"""The command registry, the call context and ``dispatch``: one table drives the CLI, the MCP tools and the docs.

Every command of the kit is a ``Command`` in ``COMMANDS``, including those whose module is not built yet: its
``handler`` and ``renderer`` are ``"module:function"`` strings imported lazily, so a missing module is reported as
``not built`` (exit 3) instead of failing the whole kit. Package modules reach each other the same way, through
``optional(name)``.

``dispatch(cmd, args, ctx, fmt)`` fills defaults, applies the confirm gate (over MCP a write that needs
``confirm=true`` runs as a preview without it), calls the handler (which returns full-size lists), pages each list
key by ``limit`` and ``offset``, and renders: JSON (``{"version": stamp, "command": name, ...}``) or the version line
followed by the renderer's text. It returns ``(text, is_error, json_obj)``; a domain error becomes the version line
and the message, with ``is_error`` set.

A handler result may carry ``exit_code`` (for example ``validate`` with problems, or ``scan`` with hits): the CLI
exits with it and a non-zero value marks the result as an error.

The confirm gate: for a ``confirm`` command called over MCP without ``confirm=true``, ``dispatch`` sets
``ctx.preview`` before the handler runs, and after it returns the result is a preview (``is_error``, ending with
"Preview only; nothing written...") when ``ctx.preview`` is still set. A call of such a command that needs no gate
(``import`` status or suggest; ``answer`` without ``apply``, or with ``apply`` and no destructive op) clears
``ctx.preview`` itself and writes as usual; ``ctx.preview`` is restored after every call.
"""

from __future__ import annotations

import importlib
import json
import os
import time
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import history, render, store, util
from .errors import NotBuilt, OntoError, UsageError

READ = {"readOnlyHint": True}
WRITE = {"readOnlyHint": False}
PREVIEW_TEXT = "Preview only; nothing written. Call again with confirm=true."
PREVIEW_TEXT_CLI = "Preview only; nothing written. Run it again with --confirm."  # the CLI's flag, not the MCP arg
COMMON_ARGS = ("limit", "offset")
FORMATS = ("compact", "text", "json")


@dataclass(frozen=True)
class Command:
    """One command: CLI subcommand ``name``, MCP tool ``tool`` (None when CLI-only), ``profile`` ``query``,
    ``full`` or ``cli``. ``props`` maps each argument to a schema_lite property schema with its ``default``;
    ``positional`` lists the CLI positionals in order; ``lists`` are the result keys that page; ``confirm`` means
    an MCP call writes only with ``confirm=true``; ``aliases`` maps an extra CLI name to the arguments it presets;
    ``cli_only`` lists arguments the MCP tool does not offer."""

    name: str
    tool: Optional[str]
    profile: str
    title: str
    short: str
    props: dict
    required: tuple = ()
    positional: tuple = ()
    handler: str = ""
    renderer: str = ""
    lists: tuple = ()
    default_limit: int = 20
    annotations: dict = field(default_factory=dict)
    confirm: bool = False
    needs_repo: bool = True
    aliases: dict = field(default_factory=dict)
    cli_only: tuple = ()


# property schema helpers ---------------------------------------------------------------------------------------
def _s(description: str, default: Any = None, enum: Optional[List[str]] = None, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": "string", "description": description, "default": default}
    if enum:
        out["enum"] = list(enum)
    out.update(extra)
    return out


def _b(description: str, default: bool = False) -> Dict[str, Any]:
    return {"type": "boolean", "description": description, "default": default}


def _i(description: str, default: Any = None, minimum: Optional[int] = None,
       maximum: Optional[int] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": "integer", "description": description, "default": default}
    if minimum is not None:
        out["minimum"] = minimum
    if maximum is not None:
        out["maximum"] = maximum
    return out


def _a(description: str, default: Any = None, items: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"type": "array", "items": items or {"type": "string"}, "description": description, "default": default}


def _o(description: str, default: Any = None) -> Dict[str, Any]:
    return {"type": "object", "description": description, "default": default}


FIND_HELP = "list every place that holds the text (quotes, source texts, proposals, decisions, the log)"


def _limit(default: int) -> Dict[str, Any]:
    return _i("items per page (0 = no cap on the CLI, the size cap over MCP)", default, minimum=0)


OFFSET = _i("skip this many items (paging)", 0, minimum=0)
CONFIRM = _b("write for real; without it the call only previews (MCP)", False)
# onto_answer writes without confirm unless apply=true carries a destructive or pack op (interview.confirm_ops):
# then the whole call is a preview (cmd_answer), not even the answer is stored
ANSWER_CONFIRM = _b("needed when apply=true carries merges, archives, confirmed updates, pack changes or new "
                    "questions; without it such a call only previews and writes nothing, not even the answer", False)
ID = _s("a node, edge or source id; loose ids resolve (tomato -> crop:tomato)")
NEXT_ABOUT = _s("a node id: its gap questions and the follow-ups of the questions that recorded it come first; "
                "loose ids resolve")
NARROWS = _s("an active decision this one makes more specific (both stay active)")


def _h(module: str, name: str) -> str:
    return "ontokit.%s:%s" % (module, name)


# the registry (E.2) --------------------------------------------------------------------------------------------
COMMANDS: Tuple[Command, ...] = (
    Command(
        "init", None, "cli", "Create a topic",
        "Create a topic repo here (or at --path): ontology.json, the empty local pack and the root topic node.",
        {"name": _s("topic slug, for example owner-topic (default: the ns)"),
         "ns": _s("namespace other topics import this one under"),
         "title": _s("one-line title"),
         "path": _s("folder to create the topic in (default: the working directory)"),
         "personal": _s("personal data in what you feed it: keep, redact or refuse, for every personal kind "
                        "(default: the kit's policy)", None, enum=["keep", "redact", "refuse"])},
        required=("ns", "title"), handler=_h("cmd_core", "cmd_init"), renderer=_h("cmd_core", "render_init"),
        annotations=dict(WRITE), needs_repo=False,
    ),
    Command(
        "setup", None, "cli", "Set up a topic",
        "Turn a template clone into a ready topic, or make a new topic folder from one: step 1, init, packs, "
        "answers, commit, plugin wiring, agent files and launch. Idempotent; each step says done, already or "
        "skipped.",
        {"new": _s("make DIR a new topic, cloned from this template checkout"),
         "here": _b("make this template clone the topic"),
         "title": _s("one-line title (default: from the name)"),
         "summary": _s("what the topic is about, in the user's sentence: the root node's summary"),
         "name": _s("topic slug (default: from the folder or the title)"),
         "ns": _s("namespace (default: from the name)"),
         "kit_url": _s("the kit's git URL (default: the kit remote, else the template's origin)"),
         "origin": _s("your own git remote, added as origin (never pushed)"),
         "personal": _s("personal data: keep, redact or refuse (default: the kit's policy)", None,
                        enum=["keep", "redact", "refuse"]),
         "packs": _a("extra built-in packs to add"),
         "plugin": _s("project, local, plugin-dir or skip (default: project for a new topic; an existing one "
                      "keeps its recorded or last-tried mode)", None, enum=["project", "local", "plugin-dir", "skip"]),
         "launch": _s("auto starts Claude Code in the topic when it can; none only prints the next step", "auto",
                      enum=["auto", "none"]),
         "yes": _b("no prompts; defaults everywhere"),
         "agent": _a("the agents that will open the topic: claude, devin, codex, cursor, copilot, gemini or generic "
                     "(default: claude, or the recorded choice; the agents step wires each one)"),
         "answers": _o("answers and decisions the agent collected, as @.onto/setup.json"),
         "cloud_ok": _b("the user accepts a cloud-synced target folder")},
        handler=_h("onboard", "cmd_setup"), renderer=_h("onboard", "render_setup"), annotations=dict(WRITE),
        needs_repo=False,
    ),
    Command(
        "doctor", None, "cli", "Check the setup",
        "Read-only checks of python, git, the folder (template or topic), git rules, cloud sync, evicted files, "
        "conflict copies, stale locks, the plugin wiring, the agent files and topic health; exit 1 when one fails.",
        {}, handler=_h("doctor", "cmd_doctor"), renderer=_h("doctor", "render_doctor"), annotations=dict(READ),
        needs_repo=False,
    ),
    Command(
        "agents", None, "cli", "Agent harnesses",
        "The agent harnesses the kit supports: list says what each reads and what this repo has wired, show <name> "
        "prints the files a topic gets and the paste blocks, render regenerates .agents/skills (--check: exit 1 on "
        "drift).",
        {"action": _s("list, show or render", "list", enum=["list", "show", "render"]),
         "name": _s("the agent for show: claude, devin, codex, cursor, copilot, gemini or generic"),
         "check": _b("render: compare .agents/skills with the render and write nothing")},
        positional=("action", "name"), handler=_h("agents", "cmd_agents"), renderer=_h("agents", "render_agents"),
        annotations=dict(WRITE), needs_repo=False,
    ),
    Command(
        "status", "onto_status", "query", "Topic status",
        "Where the topic stands: stage, richness, pending proposals, stale sources, imports, the last checkpoint "
        "and what to do next. Start here.",
        {"detail": _b("also list counts by kind and relation")},
        handler=_h("cmd_core", "cmd_status"), renderer=_h("cmd_core", "render_status"), annotations=dict(READ),
    ),
    Command(
        "brief", "onto_brief", "query", "Brief on a subject",
        "One call for a subject (an id or words): scope, facts, relations, bridges, evidence, decisions and open "
        "gaps within a token budget; the footer names the calls for what was left out.",
        {"subject": _s("an id or a few words"),
         "budget": _i("tokens to fill (characters / 4); 0 = no cap on the CLI, 5000 over MCP", 1500, minimum=0),
         "drafts": _b("include draft (proposed) records", True)},
        required=("subject",), positional=("subject",), handler=_h("answers", "cmd_brief"),
        renderer=_h("answers", "render_brief"), annotations=dict(READ),
    ),
    Command(
        "context", "onto_context", "query", "Context for a task",
        "One call before writing a deliverable: goals, constraints, active decisions, template headings with ids, "
        "subject nodes with linked records and open gaps, within a token budget.",
        {"task": _s("what you are about to write or do"),
         "deliverable": _s("a deliverable template name (default: the best match, else brief)"),
         "budget": _i("tokens to fill (characters / 4); 0 = no cap on the CLI, 5000 over MCP", 1000, minimum=0)},
        required=("task",), positional=("task",), handler=_h("answers", "cmd_context"),
        renderer=_h("answers", "render_context"), annotations=dict(READ),
    ),
    Command(
        "card", "onto_card", "query", "Node card",
        "A node's card in one read (at most 1,000 characters): what it is, key relations, sources, needs and the "
        "follow-up calls.",
        {"id": ID}, required=("id",), positional=("id",), handler=_h("answers", "cmd_card"),
        renderer=_h("answers", "render_card"), annotations=dict(READ),
    ),
    Command(
        "get", "onto_get", "query", "Get a record",
        "One node, edge or source: facts, provenance, relations by label (paged per label), needs and decisions. "
        "For a source, its text in untrusted fences (chunk or lines).",
        {"id": ID,
         "full": _b("do not cut long texts"),
         "chunk": _i("source chunk number (1-based)", None, minimum=1),
         "lines": _s("source line span a-b"),
         "include_archived": _b("also list archived records"),
         "limit": _limit(10), "offset": OFFSET},
        required=("id",), positional=("id",), handler=_h("queries", "cmd_get"), renderer=_h("queries", "render_get"),
        default_limit=10, annotations=dict(READ),
    ),
    Command(
        "search", "onto_search", "query", "Search",
        "Find nodes by id, name, alias or text (whole-word stems, OR across words); exact archived ids are listed, "
        "marked.",
        {"text": _s("words or an id"),
         "kinds": _a("only these kinds"),
         "ns": _s("only this namespace (self for local)"),
         "status": _s("any, confirmed or drafts", "any", enum=["any", "confirmed", "drafts"]),
         "include_archived": _b("also list archived records"),
         "find": _b(FIND_HELP),
         "limit": _limit(20), "offset": OFFSET},
        required=("text",), positional=("text",), handler=_h("queries", "cmd_search"),
        renderer=_h("queries", "render_search"), lists=("results",), annotations=dict(READ),
    ),
    Command(
        "neighbors", "onto_neighbors", "query", "Neighbors",
        "Nodes within 1 to 3 hops, with every link that reached each; hubs are not expanded.",
        {"id": ID,
         "depth": _i("hops (1 to 3)", 1, minimum=1, maximum=3),
         "rels": _a("only these relations"),
         "kinds": _a("only these kinds"),
         "ns": _s("only this namespace"),
         "include_archived": _b("also walk archived records"),
         "limit": _limit(40), "offset": OFFSET},
        required=("id",), positional=("id",), handler=_h("queries", "cmd_neighbors"),
        renderer=_h("queries", "render_neighbors"), lists=("items",), default_limit=40, annotations=dict(READ),
    ),
    Command(
        "path", "onto_path", "query", "Paths between nodes",
        "The k shortest paths between two nodes over active links; bridges are marked ~> and same_as classes "
        "count as one node.",
        {"from": _s("start id"), "to": _s("end id"),
         "max_depth": _i("longest path in hops", 4, minimum=1, maximum=8),
         "k": _i("paths to return", 3, minimum=1, maximum=10),
         "rels": _a("only these relations")},
        required=("from", "to"), positional=("from", "to"), handler=_h("queries", "cmd_path"),
        renderer=_h("queries", "render_path"), annotations=dict(READ),
    ),
    Command(
        "gaps", "onto_gaps", "query", "Gaps and richness",
        "Ranked gaps with the question or action that closes each; section summary gives richness and its parts, "
        "history the trend, calibration the review agreement.",
        {"section": _s("gaps, summary, history or calibration", "gaps",
                       enum=["gaps", "summary", "history", "calibration"]),
         "kind": _s("only nodes of this kind"),
         "node": _s("only this node"),
         "type": _s("only this gap type"),
         "suggest_sources": _b("suggest source lines that may close the gaps"),
         "limit": _limit(20), "offset": OFFSET},
        handler=_h("richness", "cmd_gaps"), renderer=_h("richness", "render_gaps"), lists=("gaps",),
        annotations=dict(READ), aliases={"richness": {"section": "summary"}},
    ),
    Command(
        "decisions", "onto_decisions", "query", "Decisions",
        "Recorded decisions, newest first. No scope and no text lists everything; an empty scope list matches "
        "none.",
        {"scope": _a("ids, namespaces or areas the decision must overlap"),
         "active": _b("only active decisions", True),
         "text": _s("words that must all appear"),
         "limit": _limit(20), "offset": OFFSET},
        positional=("text",), handler=_h("cmd_core", "cmd_decisions"), renderer=_h("cmd_core", "render_decisions"),
        lists=("decisions",), annotations=dict(READ),
    ),
    Command(
        "next", "onto_next", "full", "Next questions",
        "The next interview questions, ranked by stage, coverage and the gaps they close.",
        {"n": _i("how many questions", 3, minimum=1, maximum=20),
         "stage": _i("only this stage (0 to 9)", None, minimum=0, maximum=9),
         "about": NEXT_ABOUT},
        handler=_h("interview", "cmd_next"), renderer=_h("interview", "render_next"), annotations=dict(READ),
    ),
    Command(
        "answer", "onto_answer", "full", "Record an answer",
        "Store an interview answer as a source and its ops as a proposal; apply=true accepts stated, quoted facts "
        "at once (merges, archives, confirmed updates, pack changes and new questions need confirm=true).",
        {"q": _s("the question id"),
         "text": _s("the answer in the user's words (not needed for skipped, na or later)"),
         "text_file": _s("read the answer from this file ('-' reads stdin), so the user's words never pass through "
                         "a shell (CLI)"),
         "status": _s("answered, skipped, na, later or skip_stage", "answered",
                      enum=["answered", "skipped", "na", "later", "skip_stage"]),
         "ops": _a("proposal ops drafted from the answer", None, items={"type": "object"}),
         "apply": _b("accept stated facts now and draft the rest"),
         "confirm": ANSWER_CONFIRM},
        required=("q",), positional=("q", "text"), handler=_h("interview", "cmd_answer"),
        renderer=_h("interview", "render_answer"), annotations=dict(WRITE), confirm=True, cli_only=("text_file",),
    ),
    Command(
        "ingest", "onto_ingest", "full", "Ingest a source",
        "Store text, a file or a folder as sanitized, hashed sources (credentials are refused); returns the chunks "
        "to read and draft proposals from.",
        {"path": _s("a file or folder inside the repo or its inbox/ ('-' reads stdin on the CLI)"),
         "text": _s("the text itself (instead of path)"),
         "kind": _s("note, file, url or transcript", "note", enum=["note", "file", "url", "transcript"]),
         "title": _s("a neutral title (the file name is never stored)"),
         "url": _s("where the text was fetched from"),
         "fetched_at": _s("when it was fetched (timestamp)"),
         "original": _s("path of the original file the text was converted from"),
         "keep_original": _b("also store the original file (binary: only if policy.personal keeps all)"),
         "via": _s("the tool:<id> node that produced the text"),
         "stale_after_days": _i("mark the source stale after this many days", None, minimum=1),
         "allow_any_path": _b("read a path outside the repo (CLI only)")},
        required=("title",), positional=("path",), handler=_h("ingest", "cmd_ingest"),
        renderer=_h("ingest", "render_ingest"), annotations=dict(WRITE), cli_only=("allow_any_path",),
    ),
    Command(
        "propose", "onto_propose", "full", "Propose changes",
        "Check a proposal (ops with quotes and locations) and save it as pending; problems refuse it and say "
        "what to fix. The CLI takes the proposal as @file.",
        {"proposal": _o("the proposal object {source, summary, ops}"),
         "limit": _i("ops in the preview (0 = all); a refusal shows ops with problems", 10, minimum=0),
         "offset": OFFSET},
        required=("proposal",), handler=_h("proposals", "cmd_propose"), renderer=_h("proposals", "render_propose"),
        annotations=dict(WRITE), cli_only=("limit",),
    ),
    Command(
        "review", "onto_review", "full", "Pending proposals",
        "Without an id, the pending proposals by priority; with an id, the numbered preview of its ops with "
        "matches and conflicts.",
        {"id": _s("a proposal id"), "limit": _limit(10), "offset": OFFSET},
        positional=("id",), handler=_h("proposals", "cmd_review"), renderer=_h("proposals", "render_review"),
        lists=("pending",), default_limit=10, annotations=dict(READ),
    ),
    Command(
        "apply", "onto_apply", "full", "Apply verdicts",
        "Record verdicts (ranges such as 1-3,5) and apply the proposal.",
        {"id": _s("the proposal id"),
         "accept": _s("op numbers to accept, for example 1-3,5"),
         "draft": _s("op numbers to keep as drafts"),
         "reject": _s("op numbers to reject"),
         "edit": _o("replacement ops by op number {n: op}"),
         "all": _s("one verdict for every op", None, enum=["accept", "draft", "reject"]),
         "by": _s("who decided", "user", enum=["user", "agent"]),
         "reason": _s("why (kept with the review)"),
         "confirm": CONFIRM},
        required=("id",), positional=("id",), handler=_h("proposals", "cmd_apply"),
        renderer=_h("proposals", "render_apply"),
        annotations=dict(WRITE, destructiveHint=True, idempotentHint=True), confirm=True,
    ),
    Command(
        "decide", "onto_decide", "full", "Record a decision",
        "Record a decision in the user's words (append-only; a reversal supersedes the old one).",
        {"question": _s("the question decided"),
         "options": _a("options as id=label"),
         "recommended": _s("the option recommended"),
         "chosen": _s("the option chosen, or other with chosen_text"),
         "chosen_text": _s("the choice in the user's words"),
         "chosen_text_file": _s("read chosen_text from this file, so the user's words never pass through a shell "
                                "(CLI)"),
         "rationale": _s("why"),
         "rationale_file": _s("read the rationale from this file, so the user's words never pass through a shell "
                              "(CLI)"),
         "scope": _a("ids, namespaces or areas it covers"),
         "decided_by": _s("user, owner, team or agent", "user", enum=["user", "owner", "team", "agent"]),
         "supersedes": _s("the decision this one replaces"),
         "narrows": NARROWS},
        required=("question", "chosen"), handler=_h("cmd_core", "cmd_decide"),
        renderer=_h("cmd_core", "render_decide"), annotations=dict(WRITE),
        cli_only=("chosen_text_file", "rationale_file"),
    ),
    Command(
        "import", "onto_import", "full", "Imports",
        "Pin, update or remove a released topic under a namespace, show pins, or suggest bridges with "
        "another import (status and suggest only read).",
        {"action": _s("add, update, remove, status or suggest", None,
                      enum=["add", "update", "remove", "status", "suggest"]),
         "ns": _s("the namespace"),
         "from": _s("path of the released topic repo"),
         "ref": _s("release tag (vN) or a release commit"),
         "override": _s("an active decision that allows a pin conflict"),
         "keep": _s("ns=sha of the export to keep in a pin conflict"),
         "with": _s("the other namespace (suggest)"),
         "confirm": CONFIRM},
        required=("action",), positional=("action",), handler=_h("compose", "cmd_import"),
        renderer=_h("compose", "render_import"), annotations=dict(WRITE, destructiveHint=True), confirm=True,
    ),
    Command(
        "validate", None, "cli", "Validate",
        "Every problem (exit 1) and warning of the topic; --fix rewrites only sort order and duplicate lines.",
        {"fix": _b("fix P04 and P05 only")},
        handler=_h("cmd_core", "cmd_validate"), renderer=_h("cmd_core", "render_validate"),
        annotations=dict(READ),
    ),
    Command(
        "log", None, "cli", "Change log",
        "Dated sections from the change log (Done, Decisions, Delta, Follow-ups); --checkpoint records what was "
        "done, what comes next and the open questions; --last shows the last checkpoint.",
        {"since": _s("changes since a date, a timestamp or a span such as 7d", "7d"),
         "limit": _limit(0), "offset": OFFSET,
         "checkpoint": _b("record a checkpoint instead of reading the log"),
         "last": _b("show only the last checkpoint: what was done, what comes next, what is open"),
         "done": _a("checkpoint: what was done"),
         "next": _a("checkpoint: what comes next"),
         "open_questions": _a("checkpoint: questions still open"),
         "done_file": _s("checkpoint: read what was done from this file, one item per line, so the user's words "
                         "never pass through a shell"),
         "next_file": _s("checkpoint: read what comes next from this file, one item per line"),
         "open_questions_file": _s("checkpoint: read the open questions from this file, one item per line")},
        handler=_h("cmd_core", "cmd_log"), renderer=_h("cmd_core", "render_log"), default_limit=0,
        annotations=dict(WRITE), cli_only=("done_file", "next_file", "open_questions_file"),
    ),
    Command(
        "migrate", None, "cli", "Migrate the data format",
        "Bring the topic's data format up to this kit; --check only lists the steps. --rename-question OLD=NEW "
        "first moves a local question to a new id, with its answers and every reference to it.",
        {"check": _b("list the steps and write nothing"),
         "rename_question": _a("move a local question to a new local id first, as OLD=NEW: its interview log lines "
                               "and every reference to it move too (repeatable)")},
        handler=_h("cmd_core", "cmd_migrate"), renderer=_h("cmd_core", "render_migrate"), annotations=dict(WRITE),
    ),
    Command(
        "dupes", None, "cli", "Duplicates",
        "Likely duplicate nodes; --propose writes a pending proposal of merge ops.",
        {"kind": _s("only this kind"), "limit": _limit(50), "offset": OFFSET,
         "propose": _b("write a pending merge proposal")},
        handler=_h("proposals", "cmd_dupes"), renderer=_h("proposals", "render_dupes"), lists=("pairs",),
        default_limit=50, annotations=dict(WRITE),
    ),
    Command(
        "eval", None, "cli", "Evaluate extraction",
        "Precision and recall of a proposal against a gold extraction.",
        {"gold": _s("gold extraction file"), "proposal": _s("proposal id or file")},
        required=("gold", "proposal"), handler=_h("evaluate", "cmd_eval"), renderer=_h("evaluate", "render_eval"),
        annotations=dict(READ),
    ),
    Command(
        "erase", None, "cli", "Erase (privacy)",
        "Erase a node's or a source's content under an active decision whose scope covers it; with --find, list "
        "every place that holds a text (quotes, source texts, proposals, decisions, the log) and write nothing; "
        "with --scrub, take a text out of the fields that hold it and keep the records.",
        {"id": _s("the node or source id"), "decision": _s("the decision that allows it"),
         "find": _s("a text (a name) to look for everywhere first; writes nothing"),
         "scrub": _s("a text to take out of the fields that hold it (summaries, attrs, notes, quotes, proposals, "
                     "decisions, the log) under --decision; the records stay")},
        positional=("id",), handler=_h("cmd_core", "cmd_erase"),
        renderer=_h("cmd_core", "render_erase"), annotations=dict(WRITE, destructiveHint=True),
    ),
    Command(
        "pack", None, "cli", "Packs",
        "List the built-in packs and which are on, or turn one on (pack add assessment): it is listed in "
        "ontology.json as one logged change. Packs are never removed.",
        {"action": _s("list or add", "list", enum=["list", "add"]),
         "name": _s("the built-in pack to add, for example assessment")},
        positional=("action", "name"), handler=_h("cmd_core", "cmd_pack"), renderer=_h("cmd_core", "render_pack"),
        annotations=dict(WRITE), needs_repo=False,
    ),
    Command(
        "tools", None, "cli", "Tools check",
        "Check the tool nodes: cli tools on PATH, others unchecked. Writes .onto/tools-check.json only.",
        {"action": _s("check", "check", enum=["check"])},
        positional=("action",), handler=_h("richness", "cmd_tools_check"),
        renderer=_h("richness", "render_tools_check"), annotations=dict(READ),
    ),
    Command(
        "build", None, "cli", "Build",
        "Write build/export.json and build/cards.json (and the viewer with --html); --check builds twice and "
        "compares the bytes.",
        {"out": _s("output folder (default build/)"), "html": _b("also write build/index.html"),
         "check": _b("build twice and compare")},
        handler=_h("build", "cmd_build"), renderer=_h("build", "render_build"), annotations=dict(WRITE),
    ),
    Command(
        "release", None, "cli", "Release",
        "The release ladder: validate, scan, next tag; --write writes the release files, --commit commits and "
        "tags, --push pushes (only when asked).",
        {"write": _b("write the export, cards, MANIFEST.json and VERSIONS.md"),
         "commit": _b("commit the release files and tag vN"),
         "push": _b("push the branch and the tag"),
         "notes": _s("release notes (needed with --write)"),
         "allow_dirty": _b("commit even when other files are dirty"),
         "denylist": _s("denylist file kept outside the repo")},
        handler=_h("release", "cmd_release"), renderer=_h("release", "render_release"), annotations=dict(WRITE),
    ),
    Command(
        "scan", None, "cli", "Secret scan",
        "Scan paths for secrets and denylisted terms (exit 2 on hits; only path, kind and a 6-character prefix "
        "are printed).",
        {"paths": _a("files or folders (default: the repo)"), "denylist": _s("denylist file kept outside the repo")},
        positional=("paths",), handler=_h("release", "cmd_scan"), renderer=_h("release", "render_scan"),
        annotations=dict(READ), needs_repo=False,
    ),
    Command(
        "bench", None, "cli", "Token benchmark",
        "Measure the token cost of tasks over MCP (characters / 4, an estimate).",
        {"tasks": _s("tasks file"), "against": _s("golden result to compare with"), "ref": _s("kit ref to measure")},
        handler=_h("bench", "cmd_bench"), renderer=_h("bench", "render_bench"), annotations=dict(READ),
        needs_repo=False,
    ),
)

_BY_NAME: Dict[str, Command] = {}
for _cmd in COMMANDS:
    _BY_NAME[_cmd.name] = _cmd
    if _cmd.tool:
        _BY_NAME[_cmd.tool] = _cmd
    for _alias in _cmd.aliases:
        _BY_NAME.setdefault(_alias, _cmd)


def get(name_or_tool: str) -> Command:
    """The command for a CLI name, an alias or an MCP tool name; ``UsageError`` otherwise."""
    found = _BY_NAME.get(name_or_tool)
    if found is None:
        raise UsageError("unknown command %r" % (name_or_tool,), commands=sorted(c.name for c in COMMANDS))
    return found


def tools(profile: str = "full") -> List[Command]:
    """The MCP tools of a profile: ``query`` has the read tools, ``full`` adds the write tools."""
    wanted = ("query",) if profile == "query" else ("query", "full")
    return [c for c in COMMANDS if c.tool and c.profile in wanted]


def optional(module: str) -> Optional[ModuleType]:
    """``ontokit.<module>`` when it is built, else None. A built module whose own import fails raises, so a
    broken package reads as the bug it is, not as "not built"."""
    return util.optional_module(module)


def _import(target: str) -> Callable[..., Any]:
    """The handler or renderer a ``"module:function"`` string names; ``NotBuilt`` (via ``LookupError``) when the
    module or the function does not exist yet."""
    module, _sep, attr = target.partition(":")
    try:
        mod = importlib.import_module(module)
    except ImportError as exc:
        if util.module_missing(exc, module):
            raise LookupError(target)
        raise
    try:
        return getattr(mod, attr)
    except AttributeError:
        raise LookupError(target)


# the context ---------------------------------------------------------------------------------------------------
class Context(object):
    """What a handler needs: the repo (discovered lazily), the transport (``mcp``), the profile, the preview flag
    and the environment. ``onto()`` loads the graph (cached by ``store.data_stamp``, so it reloads after a write)."""

    def __init__(self, repo: Optional[store.Repo] = None, mcp: bool = False, profile: str = "cli",
                 preview: bool = False, env: Optional[Dict[str, str]] = None, explicit: Optional[str] = None,
                 cwd: Optional[str] = None) -> None:
        self._repo = repo
        self.mcp = mcp
        self.profile = profile
        self.preview = preview
        self.env = dict(os.environ if env is None else env)
        self.explicit = explicit
        self.cwd = cwd
        self.loads = 0
        self.load_s = 0.0
        self.calls = 0
        self._onto: Any = None

    @property
    def repo(self) -> store.Repo:
        """The topic repo; discovered on first use (``DataError`` when there is none)."""
        if self._repo is None:
            self._repo = store.discover(self.explicit, self.cwd, self.env)
        return self._repo

    @repo.setter
    def repo(self, value: Optional[store.Repo]) -> None:
        self._repo = value
        self._onto = None

    def has_repo(self) -> bool:
        if self._repo is not None:
            return True
        try:
            self.repo
        except OntoError:
            return False
        return True

    def onto(self) -> Any:
        from .graph import Ontology

        started = time.perf_counter()
        loaded = Ontology.load(self.repo)
        if loaded is not self._onto:
            self.loads += 1
            self.load_s += time.perf_counter() - started
            self._onto = loaded
        return loaded

    def call(self, tool: str, **args: Any) -> str:
        """A follow-up call in the caller's words (``onto_get id=X`` over MCP, ``onto get X`` on the CLI)."""
        name = tool[5:] if tool.startswith("onto_") else tool
        return render.call(self.mcp, name, **args)

    def available(self, tool: str) -> bool:
        """True when this caller can make the call: every command on the CLI; over MCP, only the tools of the
        active profile (``query`` has no write tools), so a follow-up never names a tool the caller lacks."""
        if not self.mcp:
            return True
        name = tool if tool.startswith("onto_") else "onto_" + tool
        return any(c.tool == name for c in tools(self.profile))

    def richness(self) -> Optional[Dict[str, Any]]:
        """``{score, band, change_text}`` from ``richness.summary`` when that module is built."""
        rich = optional("richness")
        if rich is None or not hasattr(rich, "summary"):
            return None
        try:
            summary = rich.summary(self.onto())
        except Exception:  # the version line must never fail because of a measure
            return None
        if not isinstance(summary, dict) or summary.get("score") is None:
            return None
        return {"score": summary.get("score"), "band": summary.get("band"),
                "change_text": _change_text(summary.get("change7"))}

    def stamp(self) -> Optional[Dict[str, Any]]:
        """The version stamp of the repo, with richness when available; None without a repo."""
        if not self.has_repo():
            return None
        try:
            return store.version_stamp(self.repo, None, self.richness())
        except OntoError:
            return None


def _change_text(change: Any) -> Optional[str]:
    if change is None:
        return None
    if isinstance(change, str):
        return change or None
    if isinstance(change, (list, tuple)) and len(change) == 2:
        return history.fmt_change(change[0], change[1])
    if isinstance(change, dict) and "delta" in change:
        return history.fmt_change(change.get("delta"), change.get("at"))
    return None


# dispatch ------------------------------------------------------------------------------------------------------
def fill_defaults(cmd: Command, args: Dict[str, Any]) -> Dict[str, Any]:
    """``args`` with every missing (or None) argument set to its default, and unknown arguments refused."""
    known = set(cmd.props) | set(COMMON_ARGS)
    unknown = sorted(k for k in args if k not in known)
    if unknown:
        raise UsageError("%s: unknown argument%s %s; accepted: %s" % (
            cmd.name, "" if len(unknown) == 1 else "s", ", ".join(unknown), ", ".join(sorted(cmd.props))))
    out: Dict[str, Any] = {}
    for key, schema in cmd.props.items():
        value = args.get(key)
        out[key] = schema.get("default") if value is None else value
    for key in COMMON_ARGS:
        if key not in cmd.props and args.get(key) is not None:
            out[key] = args[key]
    missing = [k for k in cmd.required if out.get(k) in (None, "")]
    if missing:
        raise UsageError("%s needs %s" % (cmd.name, ", ".join(missing)), accepted=sorted(cmd.props))
    return out


def _paging(cmd: Command, args: Dict[str, Any], result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Page every list key in place; returns the paging object of the first list key present."""
    limit = args.get("limit")
    limit = cmd.default_limit if limit is None else int(limit)
    offset = max(0, int(args.get("offset") or 0))
    first: Optional[Dict[str, Any]] = None
    totals = dict(result.get("totals") or {})
    for key in cmd.lists:
        items = result.get(key)
        if not isinstance(items, list):
            continue
        shown, total = render.page(items, limit, offset)
        result[key] = shown
        totals[key] = total
        remaining = max(0, total - offset - len(shown))
        info = {"key": key, "offset": offset, "limit": limit, "more": remaining > 0, "remaining": remaining,
                "next_offset": offset + len(shown) if remaining > 0 else None}
        if first is None:
            first = info
    if totals:
        result["totals"] = totals
    if first is not None:
        result["paging"] = first
    return first


def _page_line(cmd: Command, args: Dict[str, Any], paging: Optional[Dict[str, Any]], ctx: Context) -> List[str]:
    if not paging or not paging.get("more"):
        return []
    follow = {k: v for k, v in args.items()
              if k in cmd.props and v is not None and v != cmd.props[k].get("default") and k != "offset"}
    follow["offset"] = paging["next_offset"]
    ordered = {k: follow[k] for k in cmd.positional if k in follow}
    ordered.update({k: v for k, v in follow.items() if k not in ordered})
    shown = paging["next_offset"] - paging["offset"]
    return ["[page] %s %d-%d of %d; next: %s" % (
        paging["key"], paging["offset"] + 1, paging["offset"] + shown,
        paging["offset"] + shown + paging["remaining"], ctx.call(cmd.tool or cmd.name, **ordered))]


def generic_render(result: Dict[str, Any], mode: str, ctx: Context) -> List[str]:
    """A plain renderer for commands whose own renderer is missing: one ``key: value`` line per result key."""
    lines = []
    for key in sorted(result):
        if key in ("totals", "paging", "exit_code"):
            continue
        lines.append("%s: %s" % (key, render.trunc(render.fmt(result[key]), 300)))
    return lines


def _as_text(rendered: Any) -> str:
    if rendered is None:
        return ""
    if isinstance(rendered, (list, tuple)):
        return "\n".join(str(line) for line in rendered)
    return str(rendered)


def _json_text(obj: Any, ctx: Context) -> str:
    if ctx.mcp:
        return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=1, default=str)


def error_text(exc: OntoError) -> str:
    """The message of an error, with the candidates of a miss on a second line."""
    text = exc.message
    candidates = getattr(exc, "candidates", None)
    if candidates:
        text += "\n%s: %s" % ("matches" if getattr(exc, "ambiguous", False) else "did you mean",
                              ", ".join(str(c) for c in list(candidates)[:12]))
    return text


def dispatch(cmd: Command, args: Dict[str, Any], ctx: Context,
             fmt: str = "compact") -> Tuple[str, bool, Dict[str, Any]]:
    """Run ``cmd`` (see the module docstring). Returns ``(text, is_error, json_obj)``."""
    fmt = fmt if fmt in FORMATS else "compact"
    ctx.calls += 1
    saved_preview = ctx.preview
    stamp: Optional[Dict[str, Any]] = None
    try:
        try:
            handler = _import(cmd.handler)
        except LookupError:
            raise NotBuilt(cmd.name, handler=cmd.handler)
        full = fill_defaults(cmd, dict(args or {}))
        for name in sorted(full):
            half = util.surrogate_problem(full[name], name)
            if half:  # no file can hold it: say which argument, never echo it
                raise UsageError("argument %s; retype it (or pass UTF-8 text)" % half)
        if cmd.confirm and ctx.mcp and not full.get("confirm"):
            ctx.preview = True
        if cmd.needs_repo:
            ctx.repo  # discover now, so a missing repo reads as the error it is
        result = handler(ctx, full)
        if not isinstance(result, dict):
            result = {"result": result}
        paging = _paging(cmd, full, result)
        stamp = ctx.stamp()
        exit_code = int(result.get("exit_code") or 0)
        is_error = exit_code != 0
        preview = bool(ctx.preview)
        if fmt == "json":
            obj = {"version": stamp, "command": cmd.name}
            obj.update(result)
            if preview:
                if not result.get("preview"):  # keep a handler's own preview details (answer, apply, import)
                    obj["preview"] = True
                obj["message"] = PREVIEW_TEXT
            return _json_text(obj, ctx), is_error or preview, obj
        try:
            renderer = _import(cmd.renderer) if cmd.renderer else generic_render
        except LookupError:
            renderer = generic_render
        body = _as_text(renderer(result, "compact" if fmt == "compact" else "text", ctx))
        parts = [render.version_line(stamp)]
        if body:
            parts.append(body)
        parts.extend(_page_line(cmd, full, paging, ctx))
        if preview:
            parts.append(PREVIEW_TEXT if ctx.mcp else PREVIEW_TEXT_CLI)
        obj = {"version": stamp, "command": cmd.name}
        obj.update(result)
        if preview and not result.get("preview"):
            obj["preview"] = True
        return "\n".join(parts), is_error or preview, obj
    except OntoError as exc:
        if stamp is None:
            try:
                stamp = ctx.stamp()
            except Exception:
                stamp = None
        obj = exc.to_json()
        obj["exit_code"] = exc.code
        obj["command"] = cmd.name
        if fmt == "json":
            return _json_text(dict(obj, version=stamp), ctx), True, dict(obj, version=stamp)
        return render.version_line(stamp) + "\n" + error_text(exc), True, obj
    finally:
        ctx.preview = saved_preview
