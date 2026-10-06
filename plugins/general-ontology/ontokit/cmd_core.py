"""Handlers and renderers of the core commands: ``init``, ``status``, ``validate``, ``decide``, ``decisions``, ``log``,
``pack`` and ``migrate``.

A handler is ``cmd_<name>(ctx, args) -> dict`` (it never prints; lists are returned whole and ``dispatch`` pages
them). A renderer is ``render_<name>(result, mode, ctx) -> list of lines`` for ``compact`` or ``text``; the version
line is added by ``dispatch``.

``init`` lists only the files it wrote. In a template clone that skipped the README's step 1 (still on the branch
``general-ontology``, or ``origin`` still the template) it stops and says so. Outside a template checkout it adds the
topic's git rules (``inbox/`` and ``.onto/`` ignored, byte-exact data, union merges for the logs) to the folder's
``.gitignore`` and ``.gitattributes`` when no file from the folder up to the git root holds them, and warns that the
kit is not vendored.

``status`` fills its optional sections from ``interview.progress``, ``richness.summary`` and ``compose.pin_status``
when those modules are built; a missing module leaves the section null and says so in ``notes``. Its ``Next`` names
the review when pending proposals are over ``max_pending`` or the staged interview is done (as the session-start hook
does), else the top interview question with its ``[untrusted]`` and ``(draft)`` markers and JSON flags; pending
proposals then add ``then`` (the review step and its call), printed whole on a ``Then:`` line. A stale source's
refresh tools carry their markers too.

``decide`` and ``decisions`` resolve each scope entry as ``get`` resolves an id (``resolve_scope``): a loose id is
stored and read as the record it names, and an id-shaped entry that names no record is reported as not in the
ontology. Prefixes, namespaces and area words stay as typed.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from . import (__version__, gitutil, ids, ledger, lockfile, migrate, mutate, needs, packs, pipeline, render, store,
               util, validate)
from .commands import Context, optional
from .errors import OntoError, Problem, Refused, UsageError

QUICK_FALLBACK = ("q.frame.you", "q.frame.goal", "q.frame.deliverable")
STAGE_NAMES = {0: "frame", 1: "people", 2: "data", 3: "vocabulary", 4: "process", 5: "constraints",
               6: "deliverables", 7: "questions", 8: "compose", 9: "deepen"}
DONE_TYPES = ("init", "apply", "answer", "ingest", "import", "erase", "release", "migrate", "pack")


def _lines(items: List[str]) -> List[str]:
    return [line for line in items if line is not None]


# init ----------------------------------------------------------------------------------------------------------
def _quick_questions(onto: Any, n: int = 3) -> List[Dict[str, Any]]:
    bank = [q for q in onto.registry.questions() if q.get("quick")]
    bank.sort(key=lambda q: (int(q.get("stage") or 0), -int(q.get("priority") or 0), str(q.get("id"))))
    if bank:
        return [{"id": q.get("id"), "ask": util.normalize_ws(str(q.get("ask") or "").replace(
            "{topic}", onto.title()))} for q in bank[:n]]
    return [{"id": qid, "ask": None} for qid in QUICK_FALLBACK[:n]]


# The git rules a topic folder needs (the template's root .gitignore and .gitattributes hold them): the drop folder
# (raw, unredacted input) and kit state stay local, topic data keeps LF bytes, and the append-only logs and the local
# questions (one question per line) merge by keeping both sides' lines.
GITIGNORE_RULES = ("inbox/", ".onto/", "build/index.html")
GITIGNORE_JUNK = ("__pycache__/", "*.pyc", ".DS_Store", "* [0-9].*")
GITATTRIBUTES_RULES = ("* text=auto eol=lf", "**/sources/** -text", "**/interview/log.jsonl merge=union",
                       "**/ledger/changes.jsonl merge=union", "**/metrics/history.jsonl merge=union",
                       "**/sources/index.jsonl merge=union", "**/packs/local.questions.jsonl merge=union")
KIT_REL = os.path.join("plugins", "general-ontology", "ontokit")


def _files_under(root: str) -> List[str]:
    """Every file under ``root`` (relative, ``/``-separated), leaving out ``.git`` and ``.onto``."""
    out = []
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in (".git", ".onto"))
        for entry in sorted(names):
            out.append(os.path.relpath(os.path.join(dirpath, entry), root).replace(os.sep, "/"))
    return out


def _up_to_git_root(root: str) -> List[str]:
    """``root`` and its parents up to the folder holding ``.git`` (all of them when there is none)."""
    out, here = [], os.path.abspath(root)
    while True:
        out.append(here)
        if os.path.exists(os.path.join(here, ".git")):
            return out
        parent = os.path.dirname(here)
        if parent == here:
            return out
        here = parent


def _rule_lines(folders: List[str], name: str) -> set:
    found = set()
    for folder in folders:
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            line = " ".join(line.split())
            if line and not line.startswith("#"):
                found.add(line)
                found.add(line[3:] if line.startswith("**/") else "**/" + line)  # either spelling matches anywhere
                found.add(line.lstrip("/"))
    return found


def _ensure_git_rules(root: str) -> Dict[str, Any]:
    """Make sure the topic folder's git rules exist: when no ``.gitignore`` or ``.gitattributes`` from the topic
    folder up to the git root holds them (``onto init`` outside a template checkout), the missing ones are added to
    the topic folder's own files. Returns ``{written: [paths], kit_vendored: bool}``."""
    folders = _up_to_git_root(root)
    written: List[str] = []
    for name, rules, extra, head in (
            (".gitignore", GITIGNORE_RULES, GITIGNORE_JUNK,
             "# Topic rules (onto init): raw input and kit state stay local"),
            (".gitattributes", GITATTRIBUTES_RULES, (),
             "# Topic rules (onto init): byte-exact data and union merges for the append-only logs")):
        have = _rule_lines(folders, name)
        missing = [r for r in rules if r not in have]
        if not missing:
            continue
        missing += [r for r in extra if r not in have]
        path = os.path.join(root, name)
        old = b""
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                old = fh.read()
        text = old.decode("utf-8", "replace")
        if text and not text.endswith("\n"):
            text += "\n"
        text += "\n".join([head] + missing) + "\n"
        store.write_bytes(path, text.encode("utf-8"))
        written.append(name)
    return {"written": written, "kit_vendored": any(os.path.isdir(os.path.join(f, KIT_REL)) for f in folders)}


TEMPLATE_BRANCH = "general-ontology"
STEP_ONE = "git checkout -b main && git remote rename origin kit"


def template_clone_problem(path: str) -> Optional[str]:
    """Why a topic may not start at ``path`` yet, or None: the git repo holding it is a template clone (the kit
    sits at its root) that skipped the README's step 1, so it is still on the template branch, or its ``origin`` is
    still the template (it has ``origin/general-ontology`` and no ``kit`` remote). A topic made there would be
    committed on the branch every topic merges on upgrade, and ``release --push`` would publish it to the template."""
    here = os.path.abspath(path)
    while not os.path.isdir(here) and os.path.dirname(here) != here:
        here = os.path.dirname(here)
    top = gitutil.git(here, "rev-parse", "--show-toplevel")
    if not top or not os.path.isdir(os.path.join(top, KIT_REL)):
        return None
    if gitutil.branch(top) == TEMPLATE_BRANCH:
        return ("this clone is on the template branch %s: a topic here would be committed into the template, and "
                "release --push would publish it there. Run the README's step 1 first (%s), then add your own "
                "remote as origin" % (TEMPLATE_BRANCH, STEP_ONE))
    remotes = gitutil.git(top, "remote").split()
    if "kit" not in remotes and "origin" in remotes and gitutil.git(
            top, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/%s" % TEMPLATE_BRANCH):
        return ("origin is still the template (it has origin/%s and there is no kit remote): release --push would "
                "publish this topic there. Run git remote rename origin kit (the README's step 1), then add your "
                "own remote as origin" % TEMPLATE_BRANCH)
    return None


def cmd_init(ctx: Context, args: Dict[str, Any]) -> Dict[str, Any]:
    ns = args.get("ns")
    path = args.get("path") or ctx.cwd or os.getcwd()
    name = args.get("name") or ns
    target = os.path.abspath(os.path.expanduser(path))
    problem = template_clone_problem(target)
    if problem:
        raise Refused("onto init stopped: %s" % problem)
    before = set(_files_under(target)) if os.path.isdir(target) else set()
    repo = mutate.init_topic(path, name, ns, args.get("title"), personal=args.get("personal"))
    ctx.repo = repo
    rules = _ensure_git_rules(repo.root)
    warnings = []
    if rules["written"]:
        warnings.append("wrote the topic's git rules to %s (inbox/ and .onto/ stay out of git; the logs merge by "
                        "union)" % " and ".join(rules["written"]))
    if not rules["kit_vendored"]:
        warnings.append("the kit is not vendored in this folder (no %s): the topic runs on the plugin's kit; the "
                        "README's step 1 starts a topic from the template instead" % KIT_REL.replace(os.sep, "/"))
    created = [p for p in _files_under(repo.root) if p not in before]  # only what init wrote
    return {"created": sorted(created), "root": repo.root, "ns": repo.ns, "title": repo.manifest.get("title"),
            "warnings": warnings, "next": _quick_questions(ctx.onto())}


def render_init(result: Dict[str, Any], mode: str, ctx: Context) -> List[str]:
    lines = ["created topic %s (%s) at %s: %d files" % (result.get("ns"), result.get("title"), result.get("root"),
                                                        len(result.get("created") or []))]
    if mode == "text":
        lines += ["  %s" % path for path in result.get("created") or []]
    lines += ["warning: %s" % w for w in result.get("warnings") or []]
    asks = []
    for q in result.get("next") or []:
        asks.append("%s %s" % (q["id"], render.quote(q["ask"])) if q.get("ask") else str(q["id"]))
    lines.append("Next: the quick start (onto-interview skill): %s" % "; ".join(asks))
    return lines


# status --------------------------------------------------------------------------------------------------------
def _pending_block(repo: store.Repo) -> Dict[str, Any]:
    items = pipeline.pending(repo)
    ages = [pipeline.age_days(p) for p in items]
    limit = repo.policy.get("max_pending")
    return {
        "count": len(items),
        "oldest_days": max(ages) if ages else None,
        "over_max": isinstance(limit, int) and len(items) > limit,
        "top": [p.get("id") for p in items[:3]],
    }


def _stale_sources(onto: Any) -> List[Dict[str, Any]]:
    """Stale sources no later source supersedes, each with the tools that refresh it (``needs.stale_sources``:
    its own ``refresh_with`` edges, its ``via`` tool, and the tools of the datasets that cite it). ``tools`` repeats
    ``refresh_with`` as ``{id, untrusted?, draft?}``: a tool or link drafted from an untrusted source keeps its
    markers (C.7)."""
    return needs.stale_sources(onto, util.now())


def _counts(onto: Any, detail: bool) -> Dict[str, Any]:
    active = onto.local_nodes(active_only=True)
    edges = [e for e in onto.local_edge_ids if onto.active(e)]
    out: Dict[str, Any] = {
        "nodes": len(active),
        "drafts": sum(1 for n in active if onto.nodes[n].get("status") == "proposed"),
        "edges": len(edges),
        "draft_edges": sum(1 for e in edges if onto.edges[e].get("status") == "proposed"),
        "sources": len(onto.sources),
        "imports": len(onto.imports),
        "bridges": sum(1 for e in edges if e in onto.bridges),
    }
    if detail:
        by_kind: Dict[str, int] = {}
        for nid in active:
            k = onto.kind_of(nid)
            by_kind[k] = by_kind.get(k, 0) + 1
        by_rel: Dict[str, int] = {}
        for eid in edges:
            rel = str(onto.edges[eid].get("rel"))
            by_rel[rel] = by_rel.get(rel, 0) + 1
        out["nodes_by_kind"] = dict(sorted(by_kind.items()))
        out["edges_by_rel"] = dict(sorted(by_rel.items()))
    return out


def _review_step(ctx: Context, count: int, over: bool) -> Dict[str, Any]:
    why = "review %d pending proposal%s%s" % (count, "" if count == 1 else "s", " (over max_pending)" if over else "")
    if ctx.available("review"):
        return {"call": ctx.call("review"), "why": why}
    return {"call": "review them in the topic itself", "why": "%s (%s is in the full profile)"
            % (why, ctx.call("review"))}


def _next_step(ctx: Context, onto: Any, pending: Dict[str, Any], progress: Optional[Dict[str, Any]],
               notes: List[str]) -> Dict[str, Any]:
    """The one next call. Pending proposals come first when they are over ``max_pending``, and once the staged
    interview is done (stage 9, where a repeatable question is always open), as the session-start hook says;
    otherwise the top interview question, whose ``[untrusted]`` and ``(draft)`` markers and JSON flags are kept
    (C.7)."""
    count = int(pending.get("count") or 0)
    over = bool(pending.get("over_max"))
    stage = progress.get("stage") if isinstance(progress, dict) else None
    if count and (over or (isinstance(stage, int) and stage >= 9)):
        return _review_step(ctx, count, over)
    interview = optional("interview")
    if interview is not None and hasattr(interview, "next_questions"):
        try:
            top = interview.next_questions(onto, 1)
        except Exception as exc:  # status must still render
            top = []
            notes.append("next: the interview module failed (%s)" % type(exc).__name__)
        put_off = top[0].get("put_off") if top and isinstance(top[0].get("put_off"), dict) else None
        if put_off and count:  # only questions the user put off are left: the pending review comes first
            return _review_step(ctx, count, over)
        if top:
            q = top[0]
            flags = {k: True for k in ("untrusted", "draft") if q.get(k)}
            later = "; then review %d pending proposal%s" % (count, "" if count == 1 else "s") if count else ""
            if ctx.available("answer"):
                # the whole call: onto_answer needs the answer text, so the template carries its place
                out = {"call": interview.answer_call(ctx, q.get("id")),
                       "why": "ask: %s%s" % (render.mark(flags, str(q.get("ask") or q.get("id"))), later),
                       "question": q.get("id")}
            else:
                out = {"call": "ask the user: %s" % render.mark(flags, render.quote(str(q.get("ask") or q.get("id")))),
                       "why": "the topic records answers with %s, a full-profile tool%s" % (ctx.call("answer"), later),
                       "question": q.get("id")}
            out.update(flags)
            if put_off:  # next lists a put-off question only when nothing else is open
                out["put_off"] = dict(put_off)
                out["why"] = "put off (%s) until %s; ask only if the user wants to come back to it: %s" % (
                    put_off.get("status"), str(put_off.get("until") or "")[:10], out["why"])
            if count:  # the review step whole, with its call: the renderer cuts only the question
                review = _review_step(ctx, count, over)
                out["then"] = "%s: %s" % (review["why"], review["call"])
            return out
    if count:
        return _review_step(ctx, count, over)
    if not onto.local_nodes(active_only=True) or len(onto.local_nodes(active_only=True)) <= 1:
        return {"call": "the onto-interview skill", "why": "start the quick start interview"}
    if ctx.available("ingest"):
        return {"call": ctx.call("ingest", title="...", path="inbox/"),
                "why": "feed it material, or keep answering questions with the onto-interview skill"}
    return {"call": ctx.call("context", task="<what you are about to write>"),
            "why": "read the topic for a task; feeding it needs the full profile"}


def _section(notes: List[str], section: str, module: str, func: str, arg: Any) -> Any:
    """``<module>.<func>(arg)`` for an optional status section, or None with a note: when the module is not built,
    and when it fails (status is the first call of a session, so a package bug must not take it down)."""
    try:
        mod = optional(module)
    except Exception as exc:
        notes.append("%s: null (the %s module failed to load: %s)" % (section, module, type(exc).__name__))
        return None
    if mod is None or not hasattr(mod, func):
        notes.append("%s: null (the %s module is not built)" % (section, module))
        return None
    try:
        return getattr(mod, func)(arg)
    except Exception as exc:
        notes.append("%s: null (%s.%s failed: %s)" % (section, module, func, type(exc).__name__))
        return None


def cmd_status(ctx: Context, args: Dict[str, Any]) -> Dict[str, Any]:
    repo = ctx.repo
    onto = ctx.onto()
    notes: List[str] = []
    progress = _section(notes, "progress", "interview", "progress", onto)
    stage = progress.get("stage") if isinstance(progress, dict) else None
    richness = _section(notes, "richness", "richness", "summary", onto)
    pin_notes: List[str] = []
    imports = _section(pin_notes, "imports", "compose", "pin_status", repo)
    if imports is None:
        imports = (ctx.stamp() or {}).get("imports") or []
        if onto.imports:
            notes += [n.replace(": null (", ": pins only (", 1) for n in pin_notes]
    pending = _pending_block(repo)
    checkpoint = ledger.last_checkpoint(repo)
    last = None
    if checkpoint:
        last = {k: checkpoint.get(k) for k in ("id", "at", "done", "next", "open_questions")}
    repo_kit = repo.manifest.get("kit")
    result: Dict[str, Any] = {
        "title": repo.manifest.get("title"),
        "ns": repo.ns,
        "stage": stage,
        "progress": progress,
        "richness": richness,
        "pending": pending,
        "stale_sources": _stale_sources(onto),
        "imports": imports,
        "last_checkpoint": last,
        "kit": {"running": __version__, "topic": repo_kit, "mismatch": bool(repo_kit) and repo_kit != __version__},
        "profile": ctx.profile,
        "counts": _counts(onto, bool(args.get("detail"))),
        "notes": notes,
        # a write a kill stopped half way (its files read as done until it is rolled back), and what the last
        # recovery left as it was
        "interrupted": store.interrupted_lines(repo),
    }
    result["next"] = _next_step(ctx, onto, pending, progress, notes)
    if store.pending_intent(repo) is not None:
        result["next"] = {"call": "onto validate --fix",
                          "why": "a write stopped half way; roll it back first (any write command does it too), "
                                 "since the counts above include its half-written files"}
    result["load"] = {"loads": ctx.loads, "load_s": round(ctx.load_s, 3), "calls": ctx.calls}
    return result


def render_status(result: Dict[str, Any], mode: str, ctx: Context) -> List[str]:
    stage = result.get("stage")
    stage_text = "stage %s %s" % (stage, STAGE_NAMES.get(stage, "")) if stage is not None else "stage n/a"
    rich = result.get("richness")
    rich_text = "richness %s %s" % (rich.get("score"), rich.get("band") or "") if isinstance(rich, dict) \
        else "richness n/a"
    c = result.get("counts") or {}
    lines = ["%s (ns %s): %s | %s" % (render.trunc(result.get("title"), 60), result.get("ns"), stage_text,
                                      rich_text.strip()),
             "nodes %d (%d drafts), edges %d (%d drafts, %d bridges), sources %d, imports %d" % (
                 c.get("nodes", 0), c.get("drafts", 0), c.get("edges", 0), c.get("draft_edges", 0),
                 c.get("bridges", 0), c.get("sources", 0), c.get("imports", 0))]
    lines[1:1] = ["warning: %s" % line for line in result.get("interrupted") or []]
    progress = result.get("progress")
    if isinstance(progress, dict) and progress.get("stages"):
        lines.append("stages: " + ", ".join("%s %s" % (s.get("n"), "done" if s.get("done") else "%d%%" % round(
            100 * float(s.get("coverage") or 0))) for s in progress["stages"]))
    p = result.get("pending") or {}
    if p.get("count"):
        lines.append("pending: %d proposal%s, oldest %s day%s%s: %s" % (
            p["count"], "" if p["count"] == 1 else "s", p.get("oldest_days"),
            "" if p.get("oldest_days") == 1 else "s", " (over max_pending)" if p.get("over_max") else "",
            ", ".join(p.get("top") or [])))
    else:
        lines.append("pending: none")
    stale = result.get("stale_sources") or []
    if stale:
        def tools(s: Dict[str, Any]) -> str:
            items = s.get("tools") or [{"id": t} for t in s.get("refresh_with") or []]
            return ", ".join(render.mark(t) for t in items)

        lines.append("stale sources: " + "; ".join(
            "%s%s" % (s["id"], " (refresh with %s)" % tools(s) if s.get("refresh_with") else "")
            for s in stale[:5]) + ("; %s" % render.more(len(stale), 5) if len(stale) > 5 else ""))
    pins = result.get("imports") or []
    if pins:
        lines.append("imports: " + ", ".join(
            "%s %s %s%s" % (x.get("ns"), x.get("ref") or "-", "ok" if x.get("ok", True) else "mismatch",
                            " (%s)" % render.trunc(x.get("text"), 80)
                            if x.get("verdict") not in (None, "current") and x.get("text") else "")
            for x in pins))
    last = result.get("last_checkpoint")
    if last:
        nxt = "; ".join(last.get("next") or []) or "none"
        lines.append("last checkpoint %s: next: %s" % (str(last.get("at") or "")[:10], render.trunc(nxt, 80)))
    kit = result.get("kit") or {}
    if kit.get("mismatch"):
        lines.append("kit %s; the topic was written by %s (run onto migrate)" % (kit.get("running"), kit.get("topic")))
    for key in ("nodes_by_kind", "edges_by_rel"):  # present only with detail, in every mode
        if c.get(key):
            lines.append("%s: %s" % (key.replace("_", " "), ", ".join("%s %d" % kv for kv in c[key].items())))
    if mode == "text":
        load = result.get("load") or {}
        lines.append("profile %s, loads %s, calls %s" % (result.get("profile"), load.get("loads"), load.get("calls")))
    for note in result.get("notes") or []:
        lines.append("note: %s" % note)
    nxt = result.get("next") or {}
    if nxt:
        why = str(nxt.get("why") or "")
        then = nxt.get("then")
        if then:  # cut the question only; the review pointer goes on its own line, whole
            why = why.split("; then review ", 1)[0]
        lines.append("Next: %s (%s)" % (nxt.get("call"), render.trunc(why, 90)))
        if then:
            lines.append("Then: %s" % then)
    return lines


# validate ------------------------------------------------------------------------------------------------------
def cmd_validate(ctx: Context, args: Dict[str, Any]) -> Dict[str, Any]:
    report = validate.validate(ctx.repo, fix=bool(args.get("fix")))
    out = report.to_json()
    out["exit_code"] = 0 if report.ok else 1
    return out


def render_validate(result: Dict[str, Any], mode: str, ctx: Context) -> List[str]:
    problems = [Problem(p["code"], p["file"], p["line"], p["message"]) for p in result.get("problems") or []]
    warnings = [Problem(p["code"], p["file"], p["line"], p["message"]) for p in result.get("warnings") or []]
    report = validate.Report(problems, warnings, dict(result.get("summary") or {}))
    lines = report.lines()
    fixed = (result.get("summary") or {}).get("fixed")
    if fixed:
        lines.insert(0, "fixed: %s" % render.fmt(fixed))
    return lines


# erase ---------------------------------------------------------------------------------------------------------
def cmd_erase(ctx: Context, args: Dict[str, Any]) -> Dict[str, Any]:
    """``onto erase``: with ``find``, the read-only finder (``mutate.find_text``); else the erase itself
    (``ingest.erase``), followed by the finder run with the erased node's old name and aliases, so the places an
    erase by id cannot reach (an answer that names the person, a quote in another record) are listed."""
    find = args.get("find")
    scrub = args.get("scrub")
    if find not in (None, "") and scrub not in (None, ""):
        raise UsageError("give --find or --scrub, not both")
    if find not in (None, ""):
        if args.get("id") or args.get("decision"):
            raise UsageError("give --find alone (it writes nothing), or an id with --decision to erase")
        return {"find": mutate.find_text(ctx.repo, str(find))}
    if scrub not in (None, ""):
        if args.get("id"):
            raise UsageError("--scrub takes no id: it rewrites the text wherever a field holds it")
        ingest = optional("ingest")
        if ingest is None:
            raise UsageError("erase needs the ingest module, which is not built")
        return {"scrub": ingest.scrub(ctx.repo, str(scrub), args.get("decision"), by="agent" if ctx.mcp else "user")}
    if args.get("id") in (None, ""):
        raise UsageError("erase needs the id of a node or a source (or --find <text> to look first)")
    ingest = optional("ingest")
    if ingest is None:
        raise UsageError("erase needs the ingest module, which is not built")
    names: List[str] = []
    try:
        onto = ctx.onto()
        target = onto.own_local(str(args.get("id")).strip())
        if onto.is_local(target) and not (onto.nodes[target] or {}).get("erased"):
            node = onto.nodes[target]
            names = [str(v) for v in [node.get("name")] + list(node.get("aliases") or [])
                     if isinstance(v, str) and len(v.strip()) >= mutate.FIND_MIN]
    except (OntoError, KeyError):
        names = []
    result = ingest.cmd_erase(ctx, args)
    left = []
    for name in _dedupe_names(names):
        found = mutate.find_text(ctx.repo, name)
        if found["hits"]:
            left.append({"text": name, "hits": found["hits"], "erase": found["erase"], "edit": found["edit"]})
    result["names_still_in"] = left
    return result


def _dedupe_names(names: List[str]) -> List[str]:
    out: List[str] = []
    for name in names:
        if name.strip().lower() not in [n.strip().lower() for n in out]:
            out.append(name)
    return out


def _hit_text(hit: Dict[str, Any]) -> str:
    where = ", ".join(hit.get("where") or [])
    place = "%s%s" % (hit["file"], ":%d" % hit["line"] if hit.get("line") else "")
    return "%s (%s%s%s)" % (place, hit.get("what"), ", %s" % hit["id"] if hit.get("id") else "",
                            ": %s" % render.trunc(where, 80) if where else "")


def render_find(found: Dict[str, Any], mode: str, mcp: bool = False) -> List[str]:
    hits = found.get("hits") or []
    raw = found.get("raw", True)
    if not hits:
        return ["find %s: 0 places hold it (searched the graph, quotes, source texts and titles, proposals, "
                "decisions, the change log, the manifest, release and build files%s)"
                % (render.quote(found.get("text") or ""), ", imports, inbox/ and .onto/" if raw else " and imports"
                   "; inbox/, .onto/ and kept originals only on the CLI: onto erase --find")]
    lines = ["find %s: %d place%s it; nothing was written" % (
        render.quote(found.get("text") or ""), len(hits), " holds" if len(hits) == 1 else "s hold")]
    shown = hits if mode == "text" else hits[:20]
    lines += ["  %s" % _hit_text(h) for h in shown]
    if len(hits) > len(shown):
        lines.append("  %s (%s lists all)" % (render.more(len(hits), len(shown)), "format=text" if mcp else "--text"))
    if not raw:
        lines.append("  inbox/, .onto/ and kept originals (raw input) are searched on the CLI only: onto erase --find")
    erase, edit = found.get("erase") or [], found.get("edit") or []
    if erase or edit:
        steps = []
        if erase:
            steps.append("onto erase each of %s under it (an erase by id reaches what cites it)"
                         % ", ".join(erase[:8]))
        if edit:
            steps.append("onto erase --scrub %s --decision <dec> to take the text out of the fields of %s (the "
                         "records stay)" % (render.quote(found.get("text") or ""), ", ".join(edit[:8])))
        lines.append("Next: record a decision whose scope names %s, then %s" % (
            ", ".join((erase + edit)[:12]), "; and ".join(steps)))
    if found.get("root_named"):
        lines.append("  the topic's own name holds it: the topic node is never erased; rename it through a reviewed "
                     "proposal (update_node on its name)")
    return lines


def render_erase(result: Dict[str, Any], mode: str, ctx: Context) -> List[str]:
    if "find" in result:
        return render_find(result["find"], mode)
    if "scrub" in result:
        return optional("ingest").render_scrub(result["scrub"], mode)
    ingest = optional("ingest")
    lines = list(ingest.render_erase(result, mode, ctx)) if ingest is not None else []
    extra: List[str] = []
    for item in result.get("names_still_in") or []:
        for hit in item.get("hits") or []:
            extra.append("still holding data: the name %s sits in %s" % (render.quote(item["text"]), _hit_text(hit)))
        if item.get("erase"):
            extra.append("  to forget it there too, record a decision naming %s and erase %s"
                         % (", ".join(item["erase"][:6]), "it" if len(item["erase"]) == 1 else "each"))
        if item.get("edit"):
            extra.append("  to take it out of the fields of %s, record a decision naming %s and run onto erase "
                         "--scrub %s --decision <dec>" % (", ".join(item["edit"][:6]),
                                                          "it" if len(item["edit"]) == 1 else "them",
                                                          render.quote(item["text"])))
    note = lines.pop() if lines else None  # the closing note stays last
    return lines + extra + ([note] if note else [])


# decisions -----------------------------------------------------------------------------------------------------
def _id_shaped(text: str) -> bool:
    """True for a scope entry written as a record id (``kind:slug``, ``ns/kind:slug``, ``src-...``, ``e:...``), not
    as a prefix (``garden/``, ``role:``) or an area word."""
    return ":" in text.rstrip(":") or bool(ids.SOURCE_RE.match(text.lower()))


def resolve_scope(ctx: Context, scope: Any) -> Dict[str, Any]:
    """Scope entries as the records they name: ``{scope, resolved: [{given, id}], unknown: [given]}``. An entry
    ``<id>#<field>`` names one field of a record (``needs.decide_scope``): the id part resolves, and the field is
    spelled ``attrs.<field>``. A prefix
    (``garden/``, ``role:``), a namespace and a word that names no record (an area) stay as typed; an entry that
    resolves (a loose id, an alias, a unique id in one import, as ``onto get`` resolves it) becomes that record's
    id, so ``brief``, ``get`` and ``context`` find the decision; an id-shaped entry that names no record is
    ``unknown``."""
    items = [scope] if isinstance(scope, str) else list(scope or [])
    out: List[str] = []
    resolved: List[Dict[str, str]] = []
    unknown: List[str] = []
    try:
        onto = ctx.onto() if ctx.has_repo() else None
    except OntoError:  # a graph that cannot load: the scope is kept as typed
        onto = None
    namespaces = ({ctx.repo.ns} | {str(e.get("ns")) for e in onto.imports}) if onto is not None else set()
    for item in items:
        whole = str(item).strip()
        # "<id>#attrs.<field>" names one field of a record (the field a same_as conflict disagrees on)
        text, sep, field = whole.partition("#") if "#" in whole and whole.index("#") > 0 else (whole, "", "")
        text, field = text.strip(), field.strip()
        found = None
        if text and onto is not None and not text.endswith(("/", ":")) and text.lower() not in namespaces:
            try:
                found = onto.resolve(text).get("id")
            except OntoError:
                found = None
        if sep and field and not field.startswith("attrs."):
            field = "attrs." + field
        if found and found != text:
            resolved.append({"given": whole, "id": found + (sep + field if sep else "")})
        elif not found and _id_shaped(text):
            unknown.append(whole)
        value = (found or text) + (sep + field if sep and field else "")
        if value and value not in out:
            out.append(value)
    return {"scope": out, "resolved": resolved, "unknown": unknown}


def cmd_decide(ctx: Context, args: Dict[str, Any]) -> Dict[str, Any]:
    scope = resolve_scope(ctx, args.get("scope") or [])
    record = ledger.decide(
        ctx.repo, args.get("question"), args.get("options") or [], args.get("chosen"),
        chosen_text=args.get("chosen_text"), recommended=args.get("recommended"),
        decided_by=args.get("decided_by") or "user", rationale=args.get("rationale") or "",
        scope=scope["scope"], supersedes=args.get("supersedes"), narrows=args.get("narrows"),
    )
    old = record.get("supersedes")
    still = ledger.narrowed_by(ledger.all_decisions(ctx.repo)).get(old, []) if old else []
    result = {"decision": record, "scope_resolved": scope["resolved"], "scope_unknown": scope["unknown"]}
    if still:  # active decisions that narrow the one just superseded (validate W10)
        result["still_narrowing"] = still
    return result


def _decision_line(d: Dict[str, Any], width: int = render.WIDTH) -> str:
    label = next((o.get("label") for o in d.get("options") or [] if o.get("id") == d.get("chosen")), None)
    choice = d.get("chosen_text") or label or d.get("chosen")
    scope = ", ".join(d.get("scope") or []) or "no scope"
    line = "%s (%s) %s -> %s [%s]" % (d.get("id"), d.get("status"),
                                      render.quote(render.trunc(d.get("question"), width)),
                                      render.quote(render.trunc(choice, 80)), scope)
    links = []
    if d.get("narrows"):
        links.append("narrows %s" % d["narrows"])
    if d.get("narrowed_by"):
        links.append("narrowed by %s" % ", ".join(d["narrowed_by"]))
    return line + ("; " + "; ".join(links) if links else "")


def _scope_notes(result: Dict[str, Any], kept: str = "") -> List[str]:
    lines = ["scope %s -> %s" % (render.quote(r["given"]), r["id"]) for r in result.get("scope_resolved") or []]
    unknown = result.get("scope_unknown") or []
    if unknown:
        lines.append("scope %s: not in the ontology%s" % (", ".join(render.quote(u) for u in unknown), kept))
    return lines


def render_decide(result: Dict[str, Any], mode: str, ctx: Context) -> List[str]:
    d = result.get("decision") or {}
    lines = ["decided " + _decision_line(d)]
    if d.get("supersedes"):
        lines.append("supersedes %s" % d["supersedes"])
    for did in result.get("still_narrowing") or []:
        lines.append("%s still narrows %s; record a decision that supersedes it and narrows %s"
                     % (did, d["supersedes"], d.get("id")))
    return lines + _scope_notes(result, " (kept as typed)")


def _own_scope(repo: Any, scope: Any) -> Any:
    """``scope`` plus the other spelling of each local id: ``g2t/goal:x`` and ``goal:x`` name the same node in the
    topic ``g2t`` (briefs print local ids with the ns in a composed topic), unless an import uses that ns."""
    if not scope:
        return scope
    items = [scope] if isinstance(scope, str) else list(scope)
    own = repo.ns
    if not own or any(e.get("ns") == own for e in lockfile.entries(lockfile.read(repo))):
        return items
    out: List[str] = []
    for item in items:
        forms = [item]
        if isinstance(item, str) and item.startswith(own + "/") and ids.is_local(item[len(own) + 1:]):
            forms.append(item[len(own) + 1:])
        elif isinstance(item, str) and ids.is_local(item):
            forms.append("%s/%s" % (own, item))
        out.extend(f for f in forms if f not in out)
    return out


def cmd_decisions(ctx: Context, args: Dict[str, Any]) -> Dict[str, Any]:
    scope = args.get("scope")
    looked: Optional[Dict[str, Any]] = None
    wanted = scope
    if scope:
        looked = resolve_scope(ctx, scope)
        given = [scope] if isinstance(scope, str) else list(scope)
        wanted = given + [s for s in looked["scope"] if s not in given]  # the typed text and the records it names
    found = ledger.read_decisions(ctx.repo, scope=_own_scope(ctx.repo, wanted), active=bool(args.get("active")),
                                  text=args.get("text"))
    narrowers = ledger.narrowed_by(ledger.all_decisions(ctx.repo))
    found = [dict(d, narrowed_by=narrowers[d.get("id")]) if narrowers.get(d.get("id")) else d for d in found]
    return {"decisions": found, "scope": scope, "text": args.get("text"), "active": bool(args.get("active")),
            "scope_resolved": (looked or {}).get("resolved") or [],
            "scope_unknown": (looked or {}).get("unknown") or []}


def render_decisions(result: Dict[str, Any], mode: str, ctx: Context) -> List[str]:
    items = result.get("decisions") or []
    total = (result.get("totals") or {}).get("decisions", len(items))
    if not items:
        if result.get("scope") == []:
            return ["no decisions: an empty scope list matches none"]
        scope = result.get("scope")
        scope = [scope] if isinstance(scope, str) else scope
        return ["no decisions%s" % (" for %s" % ", ".join(scope) if scope else "")] + _scope_notes(result)
    width = render.WIDTH if mode == "compact" else 600
    lines = ["%d decision%s" % (total, "" if total == 1 else "s")] + _scope_notes(result)
    for d in items:
        lines.append("  " + _decision_line(d, width))
        if mode == "text" and d.get("rationale"):
            lines.append("    why: %s" % d["rationale"])
    return lines


# log -----------------------------------------------------------------------------------------------------------
def cmd_log(ctx: Context, args: Dict[str, Any]) -> Dict[str, Any]:
    repo = ctx.repo
    if args.get("checkpoint"):
        done, nxt, open_q = args.get("done") or [], args.get("next") or [], args.get("open_questions") or []
        if not (done or nxt or open_q):
            raise UsageError("a checkpoint needs --done, --next or --open-questions (comma-separated), or the same "
                             "from files: --done-file, --next-file, --open-questions-file (one item per line)")
        return {"checkpoint": ledger.checkpoint(repo, done, nxt, open_q)}
    if args.get("last"):
        found = ledger.last_checkpoint(repo)
        last = {k: found.get(k) for k in ("id", "at", "by", "done", "next", "open_questions")} if found else None
        return {"last_checkpoint": last}
    since = args.get("since")
    changes = ledger.read_changes(repo, since)
    limit = int(args.get("limit") or 0)
    offset = int(args.get("offset") or 0)
    total = len(changes)
    newest_first = list(reversed(changes))
    page = newest_first[offset: offset + limit] if limit else newest_first[offset:]
    return {"changes": list(reversed(page)), "since": since, "total": total}


def _last_checkpoint_lines(last: Optional[Dict[str, Any]], mode: str) -> List[str]:
    """The last checkpoint as a resume note: when, then Done, Next and Open, one item per line."""
    if not last:
        return ["no checkpoint yet; record one at the end of a session: onto log --checkpoint --done ... --next ..."]
    width = render.WIDTH if mode == "compact" else 600
    lines = ["last checkpoint %s (%s): resume from here" % (last.get("id"), str(last.get("at") or "")[:16])]
    for title, key in (("Done", "done"), ("Next", "next"), ("Open", "open_questions")):
        items = [str(x) for x in last.get(key) or []]
        if items:
            lines.append("  %s:" % title)
            lines += ["    - %s" % render.trunc(x, width) for x in items]
    return lines


def render_log(result: Dict[str, Any], mode: str, ctx: Context) -> List[str]:
    if result.get("checkpoint"):
        c = result["checkpoint"]
        return ["checkpoint %s: %s" % (c.get("id"), c.get("summary"))]
    if "last_checkpoint" in result:
        return _last_checkpoint_lines(result["last_checkpoint"], mode)
    changes = result.get("changes") or []
    if not changes:
        return ["no changes since %s" % result.get("since")]
    width = render.WIDTH if mode == "compact" else 600
    by_day: Dict[str, List[Dict[str, Any]]] = {}
    for c in changes:
        by_day.setdefault(str(c.get("at") or "")[:10], []).append(c)
    lines = ["%d change%s since %s%s" % (result.get("total", len(changes)), "" if result.get("total") == 1 else "s",
                                         result.get("since"), "" if len(changes) == result.get("total") else
                                         " (%d shown)" % len(changes))]
    for day in sorted(by_day):
        rows = by_day[day]
        lines.append(day)
        done = [c for c in rows if c.get("type") in DONE_TYPES]
        decided = [c for c in rows if c.get("type") == "decide"]
        checkpoints = [c for c in rows if c.get("type") == "checkpoint"]
        if done:
            lines.append("  Done:")
            lines += ["    %s %s: %s" % (c.get("id"), c.get("type"), render.trunc(c.get("summary"), width))
                      for c in done]
        if decided:
            lines.append("  Decisions:")
            lines += ["    %s: %s" % (c.get("id"), render.trunc(c.get("summary"), width)) for c in decided]
        counts: Dict[str, int] = {}
        touched = 0
        for c in rows:
            counts[str(c.get("type"))] = counts.get(str(c.get("type")), 0) + 1
            touched += len(c.get("ids") or [])
        lines.append("  Delta: %d change%s (%s), %d record%s touched" % (
            len(rows), "" if len(rows) == 1 else "s", ", ".join("%s %d" % kv for kv in sorted(counts.items())),
            touched, "" if touched == 1 else "s"))
        follow = []
        for c in checkpoints:
            follow += ["next: %s" % x for x in c.get("next") or []]
            follow += ["open: %s" % x for x in c.get("open_questions") or []]
        if follow:
            lines.append("  Follow-ups:")
            lines += ["    %s" % render.trunc(x, width) for x in follow]
    return lines


# packs ---------------------------------------------------------------------------------------------------------
def cmd_pack(ctx: Context, args: Dict[str, Any]) -> Dict[str, Any]:
    """``onto pack list`` (the built-in packs, each with ``enabled``; outside a topic none is on and ``topic`` is
    False) and ``onto pack add <name>`` (``mutate.add_pack``: one ``pack`` change; adding a pack already on writes
    nothing; it needs a topic)."""
    action = args.get("action") or "list"
    name = args.get("name")
    if action == "add":
        ctx.repo  # a missing topic reads as the error it is
        if name in (None, ""):
            raise UsageError("pack add needs the name of a built-in pack, for example: onto pack add assessment "
                             "(onto pack list shows them)")
        result = mutate.add_pack(ctx.repo, str(name).strip(), by="user")
        return dict(result, action="add")
    if action != "list":
        raise UsageError("pack takes list or add, got %r" % (action,))
    if name not in (None, ""):
        raise UsageError("pack list takes no name; to turn a pack on: onto pack add %s" % name)
    topic = ctx.has_repo()  # outside a topic the built-in packs still list, none of them on
    listed = [n for n in (ctx.repo.manifest.get("packs") or []) if isinstance(n, str)] if topic else []
    items = [dict(p, enabled=p["name"] in listed) for p in packs.available()]
    return {"action": "list", "packs": items, "listed": listed, "topic": topic}


def render_pack(result: Dict[str, Any], mode: str, ctx: Context) -> List[str]:
    if result.get("action") == "add":
        name = result.get("pack")
        if not result.get("added"):
            return ["pack %s is already on (packs: %s); nothing was written" % (name, ", ".join(result["packs"]))]
        return ["added pack %s (change %s); packs: %s" % (name, result.get("change"), ", ".join(result["packs"])),
                "Next: onto next (its questions join the interview), then onto validate"]
    items = result.get("packs") or []
    lines = ["%d built-in pack%s (packs never come off once on)" % (len(items), "" if len(items) == 1 else "s")]
    width = render.WIDTH if mode == "compact" else 600
    for p in items:
        lines.append("  %s (%s): %s" % (p["name"], "on" if p.get("enabled") else "off",
                                        render.trunc(p.get("description") or p.get("title"), width)))
        if mode == "text":
            lines.append("    kinds: %s; relations: %s; %d questions"
                         % (", ".join(p.get("kinds") or []) or "none", ", ".join(p.get("relations") or []) or "none",
                            p.get("questions") or 0))
    off = [p["name"] for p in items if not p.get("enabled")]
    if result.get("topic") is False:
        lines.append("No topic here, so none is on. Next: make a topic with onto setup (--packs <name> turns one "
                     "on), or run onto pack add <name> inside a topic")
    elif off:
        lines.append("Next: onto pack add %s" % off[0])
    return lines


# migrate -------------------------------------------------------------------------------------------------------
def cmd_migrate(ctx: Context, args: Dict[str, Any]) -> Dict[str, Any]:
    check = bool(args.get("check"))
    steps = migrate.run(ctx.repo, check=check, renames=args.get("rename_question"))
    return {"steps": steps, "check": check}


def render_migrate(result: Dict[str, Any], mode: str, ctx: Context) -> List[str]:
    steps = result.get("steps") or []
    if not steps:
        return ["up to date: nothing to migrate"]
    head = "would run" if result.get("check") else "ran"
    return ["%s %d step%s:" % (head, len(steps), "" if len(steps) == 1 else "s")] + ["  %s" % s for s in steps]
