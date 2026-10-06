"""The session-start hook: a few safe lines of context so the first session in a topic repo starts the interview.

Claude Code runs ``bin/onto-session-start`` when a session starts (``hooks/hooks.json``); what it prints becomes
session context, so the output is short and holds no untrusted text (no node names, summaries or quotes). The
launcher first hands off (quietly) to the kit vendored in a topic repo whose ``ontology.json`` passes its schema
(``handoff``), so the kit that serves the topic describes it, and the version line shows no kit skew that the CLI
would not show.

- Inside a topic repo (found the way ``store.discover`` finds one: ``$ONTO_REPO``, then the working directory and
  ``$CLAUDE_PROJECT_DIR``, walking up) whose ``ontology.json`` passes its schema (``checked_manifest``): at most
  ``MAX_LINES`` lines: the version line, one fixed ``Warning:`` line when a write stopped half way or the last
  recovery left files as they were (``interrupted_line``), the stage and richness, the pending proposals, and one
  ``Next:`` line naming the skill to use (``onto-review`` first when pending proposals are over the policy's
  ``max_pending``, as ``onto status`` does; else ``onto-interview`` while the quick start or a stage is open,
  ``onto-review`` when proposals are pending, otherwise ``onto``). When the change log holds a checkpoint, the
  ``Next:`` line also says to resume from it (``onto log --last``).
- In a checkout of the template itself (a folder holding ``plugins/general-ontology/ontokit/`` and no
  ``ontology.json``): two lines saying no ontology exists yet and to use the ``onto-interview`` skill, which runs
  ``onto setup``; in a clone still on the template branch or with the template as ``origin`` (``template_lines``),
  three lines naming the ``onto-interview`` skill, ``onto setup`` (or ``./new-topic``) and the README's step 1 it
  does, since ``onto init`` refuses there.
- In either place, one more line (after the first) when a cheap ``onto doctor`` check fails (``doctor_line``: git
  missing, conflict copies in ``.git/refs``, an evicted ``ontology.json`` or git index), within ``MAX_LINES``.
- Anywhere else: nothing. That includes a folder whose ``ontology.json`` fails the schema (another tool's file, a
  hand edit, or planted text): nothing from it reaches the session.

Every value printed from the repo matches its grammar first (``safe_stamp``): the ns, the release version, and each
import's ns, ref and commit. A value that does not match is left out, never printed.

It never fails the session: every error is caught, the exit code is always 0 (a closed stdout included), and the
output stays under ``MAX_CHARS`` characters.

Formats (``--format text|json``, ``format_arg``): ``text``, the default, prints the lines (Claude Code and Codex
add plain stdout to the session). ``json`` prints one line of ASCII JSON,
``{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "<the same lines>"}}``, for a
harness that reads context only from that field (the Devin CLI); the lines keep the same limits. Where ``text``
prints nothing, ``json`` prints nothing too. An unknown or missing value means ``text``. When the topic vendors
another kit, ``json`` does not hand off with ``os.execv``, since an older kit ignores ``--format`` and would print
plain lines the harness drops: the launcher runs the vendored hook as a child in ``text`` (``relay``) and wraps its
lines, bounded the same way, in the same envelope. A child that fails or runs past ``RELAY_TIMEOUT`` seconds leaves
the running kit to describe the topic.
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

MAX_LINES = 5
FORMATS = ("text", "json")
EVENT = "SessionStart"
MAX_CHARS = 600
RELAY_TIMEOUT = 20  # seconds the vendored kit's hook may take under --format json
LINE_WIDTH = 150
KIT_REL = ("plugins", "general-ontology", "ontokit")
QUICK = ("q.frame.you", "q.frame.goal", "q.frame.deliverable", "q.people.key", "q.data.where")
CLOSED = ("answered", "skipped", "na")
STAGE_NAMES = {0: "frame", 1: "people", 2: "data", 3: "vocabulary", 4: "process", 5: "constraints",
               6: "deliverables", 7: "questions", 8: "compose", 9: "deepen"}
RESUME_HINT = "resume from the last checkpoint (onto log --last)"
CHECKPOINT_MARK = b'"type":"checkpoint"'
MANIFEST_MAX_BYTES = 1000000  # a real ontology.json is a few hundred bytes
SAFE_VERSION = re.compile(r"^(unreleased|v[0-9]{1,9})\Z")
SAFE_REF = re.compile(r"^(v[0-9]{1,9}|[0-9a-f]{7,40})\Z")
SAFE_COMMIT = re.compile(r"^[0-9a-f]{1,40}\Z")
SAFE_KIT = re.compile(r"^[0-9]{1,4}\.[0-9]{1,5}\.[0-9]{1,6}\Z")
UNKNOWN_VERSION = "v?"
TEMPLATE_LINES = (
    "No ontology exists here yet: this folder is the general-ontology template.",
    "Use the onto-interview skill: it asks the setup questions, runs onto setup (or ./new-topic in a terminal), "
    "then starts the interview.",
)
STEP_ONE_LINES = {
    "branch": "This clone is still on the template branch general-ontology: a topic made here would be committed "
              "into the template.",
    "origin": "origin is still the template (origin/general-ontology, no kit remote): release --push would publish "
              "a topic made here to it.",
}
STEP_ONE_NEXT = ("Use the onto-interview skill; onto setup (or ./new-topic) makes a new folder or does step 1 "
                 "(git checkout -b main && git remote rename origin kit).")
DOCTOR_LINE = "Setup problem: the %s %s; run onto doctor for the fix."


_MANIFESTS: Dict[str, Tuple[Tuple[int, int], Optional[Dict[str, Any]]]] = {}


def checked_manifest(root: Optional[str]) -> Optional[Dict[str, Any]]:
    """A copy of ``<root>/ontology.json`` when it passes ``records.check(manifest, "ontology_manifest")``, else
    None (missing, unreadable, too big, not an object, or failing the schema: a bad ns, a missing format, and so
    on). Only a folder whose manifest passes counts as a topic for session context and the MCP instructions and
    profile. Cached by the file's ``st_mtime_ns`` and size."""
    if not root:
        return None
    path = os.path.join(str(root), "ontology.json")
    try:
        st = os.stat(path)
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return None
    key = (st.st_mtime_ns, st.st_size)
    cached = _MANIFESTS.get(real)
    if cached is None or cached[0] != key:
        manifest: Optional[Dict[str, Any]] = None
        try:
            from . import records

            if st.st_size <= MANIFEST_MAX_BYTES:
                with open(path, "rb") as fh:
                    value = json.loads(fh.read(MANIFEST_MAX_BYTES + 1).decode("utf-8"))
                if isinstance(value, dict) and not records.check(value, "ontology_manifest"):
                    manifest = value
        except Exception:  # unreadable, not JSON, too deep: not a topic here
            manifest = None
        cached = (key, manifest)
        _MANIFESTS[real] = cached
    return copy.deepcopy(cached[1]) if cached[1] is not None else None


def valid_ns(ns: Any) -> bool:
    """True when ``ns`` matches the NS grammar (C.2) and is not reserved."""
    from . import ids

    return isinstance(ns, str) and bool(ids.NS_RE.match(ns)) and ns not in ids.RESERVED_NS


def safe_stamp(stamp: Any) -> Optional[Dict[str, Any]]:
    """A copy of a version stamp that holds only values matching their grammar, so the version line built from it
    carries no free text: None unless the ns is valid; a release version other than ``unreleased`` or ``vN``
    becomes ``v?``; an import whose ns is not valid is left out, and its ref and commit are kept only when they
    look like a ref (``vN`` or hex) and hex; the kit versions only when they look like versions."""
    if not isinstance(stamp, dict) or not valid_ns(stamp.get("ns")):
        return None
    out = dict(stamp)
    if not SAFE_VERSION.match(str(out.get("version") or "unreleased")):
        out["version"] = UNKNOWN_VERSION
    pins = []
    for pin in out.get("imports") or []:
        if not isinstance(pin, dict) or not valid_ns(pin.get("ns")):
            continue
        ref, commit = str(pin.get("ref") or ""), str(pin.get("commit7") or "")
        pins.append({"ns": pin["ns"], "ref": ref if SAFE_REF.match(ref) else None,
                     "commit7": commit if SAFE_COMMIT.match(commit) else None, "ok": pin.get("ok") is True})
    out["imports"] = pins
    if not (SAFE_KIT.match(str(out.get("kit") or "")) and SAFE_KIT.match(str(out.get("repo_kit") or ""))):
        out["kit_mismatch"] = False
    rich = out.get("richness")
    if isinstance(rich, dict):
        score, band = rich.get("score"), str(rich.get("band") or "")
        change = str(rich.get("change_text") or "")
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not re.match(r"^[a-z]{0,20}\Z", band):
            out["richness"] = None
        else:
            out["richness"] = {"score": score, "band": band,
                               "change_text": change if re.match(r"^[-+0-9a-z .]{0,40}\Z", change) else None}
    elif rich is not None:
        out["richness"] = None
    return out


def _clean(line: Any) -> str:
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in str(line)).split())
    return text if len(text) <= LINE_WIDTH else text[: LINE_WIDTH - 3].rstrip() + "..."


def _is_template(folder: str) -> bool:
    return os.path.isdir(os.path.join(folder, *KIT_REL)) and not os.path.isfile(os.path.join(folder, "ontology.json"))


def _template_above(start: Optional[str]) -> Optional[str]:
    from . import store

    if not start or store.is_placeholder(start):
        return None
    current = os.path.abspath(os.path.expanduser(str(start)))
    while True:
        if _is_template(current):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def locate(cwd: Optional[str] = None, env: Optional[Mapping[str, str]] = None) -> Tuple[Optional[str], Optional[str]]:
    """``("topic", root)``, ``("template", root)`` or ``(None, None)``."""
    from . import store

    env = os.environ if env is None else env
    try:
        root = store.find_root(None, cwd, env)
    except Exception:  # a bad $ONTO_REPO: say nothing rather than guess
        root = None
    if root:
        # discovery stops at the first ontology.json; one that fails its schema is not a topic to describe
        return ("topic", root) if checked_manifest(root) is not None else (None, None)
    for start in (cwd or os.getcwd(), env.get(store.PROJECT_ENV)):
        found = _template_above(start)
        if found:
            return "template", found
    return None, None


def template_lines(root: Optional[str]) -> List[str]:
    """The lines for a template checkout: ``TEMPLATE_LINES``, or, in a clone that has not run the README's step 1
    (``cmd_core.template_clone_problem``: still on the template branch, or ``origin`` still the template), why a
    topic may not start there yet and the step to run, since ``onto init`` refuses there."""
    try:
        from . import cmd_core

        problem = cmd_core.template_clone_problem(root) if root else None
    except Exception:
        problem = None
    if not problem:
        return list(TEMPLATE_LINES)
    why = STEP_ONE_LINES["branch" if problem.startswith("this clone is on the template branch") else "origin"]
    return [TEMPLATE_LINES[0], why, STEP_ONE_NEXT]


def _quick_answered(repo: Any) -> int:
    from . import store

    rows, _problems = store.read_jsonl(repo.path("interview/log.jsonl"))
    return len({r.get("q") for r in rows if r.get("q") in QUICK and r.get("status") in CLOSED})


def interrupted_line(repo: Any) -> Optional[str]:
    """One fixed warning line when ``store.interrupted_lines`` has any (a write stopped half way, or the last
    recovery left files as they were), else None. It names no path or reason from the repo: ``onto status`` and
    ``onto validate`` list those."""
    from . import store

    found = store.interrupted_lines(repo)
    if not found:
        return None
    if store.pending_intent(repo) is not None:
        return ("Warning: a write stopped half way; run onto validate --fix before anything else (onto status "
                "lists the files).")
    return ("Warning: the last recovery left %d file%s as %s; check git diff, then run onto validate --fix "
            "(onto status lists them)." % (len(found), "" if len(found) == 1 else "s",
                                           "it was" if len(found) == 1 else "they were"))


def topic_lines(root: str, env: Optional[Mapping[str, str]] = None) -> List[str]:
    """The lines for a topic repo (see the module docstring)."""
    from . import commands, pipeline, render, store

    manifest = checked_manifest(root)
    if manifest is None:
        return []
    ns = str(manifest["ns"])
    repo = store.Repo(os.path.abspath(root), manifest)
    ctx = commands.Context(repo=repo, mcp=False, profile="cli", env=dict(os.environ if env is None else env))
    lines: List[str] = []
    stamp = None
    try:
        stamp = safe_stamp(ctx.stamp())
    except Exception:  # a broken richness module: the stamp without richness
        try:
            stamp = safe_stamp(store.version_stamp(repo))
        except Exception:
            stamp = None
    lines.append(render.version_line(stamp) if stamp else "%s (topic ontology)" % ns)
    try:  # a half-finished write or a recovery note, as one fixed line (no paths or reasons from the repo)
        warning = interrupted_line(repo)
    except Exception:
        warning = None
    if warning:
        lines.append(warning)
    stage: Optional[int] = None
    stage_text = "stage unknown"
    quick = None
    try:
        interview = commands.optional("interview")
    except Exception:
        interview = None
    if interview is not None and hasattr(interview, "progress"):
        try:
            progress = interview.progress(ctx.onto())
            stage = int(progress.get("stage")) if progress.get("stage") is not None else None
        except Exception:
            stage = None
    if stage is None:
        try:
            quick = _quick_answered(repo)
        except Exception:
            quick = None
        if quick is not None and quick < len(QUICK):
            stage = 0
    if stage is not None:
        stage_text = "stage %d %s %s" % (stage, STAGE_NAMES.get(stage, ""), "(open)" if stage < 9 else "(deepen)")
        if stage == 0 and quick is not None:
            stage_text += ", quick start %d of %d answered" % (quick, len(QUICK))
    rich = stamp.get("richness") if isinstance(stamp, dict) else None
    rich_text = "richness %s %s" % (rich.get("score"), rich.get("band") or "") if isinstance(rich, dict) \
        else "richness not measured yet"
    lines.append("Topic ontology here (ns %s): %s | %s" % (ns, stage_text, rich_text.strip()))
    try:
        pending = len(pipeline.pending(repo))
    except Exception:
        pending = 0
    try:
        limit = repo.policy.get("max_pending")
    except Exception:
        limit = None
    over = isinstance(limit, int) and not isinstance(limit, bool) and pending > limit
    lines.append("Pending proposals: %d%s" % (pending, " (over max_pending)" if over else ""))
    if over:
        nxt = "Next: use the onto-review skill to review the %d pending proposals first" % pending
    elif stage is not None and stage < 9:
        nxt = "Next: use the onto-interview skill to continue the interview (onto_next, then onto_answer)"
    elif pending:
        nxt = "Next: use the onto-review skill to review the %d pending proposal%s" % (
            pending, "" if pending == 1 else "s")
    else:
        nxt = "Next: use the onto skill (onto_status, then onto_brief or onto_context)"
    try:
        resume = has_checkpoint(repo)
    except Exception:
        resume = False
    lines.append(nxt + ("; " + RESUME_HINT if resume else "") + ".")
    return lines


def has_checkpoint(repo: Any) -> bool:
    """True when the change log holds a checkpoint line. A byte search, no parse: the kit writes canonical lines
    (sorted keys, no spaces), so ``"type":"checkpoint"`` appears exactly in a checkpoint line."""
    try:
        with open(repo.path("ledger/changes.jsonl"), "rb") as fh:
            return CHECKPOINT_MARK in fh.read()
    except OSError:
        return False


def doctor_line(root: Optional[str]) -> Optional[str]:
    """One fixed line naming the cheap ``onto doctor`` checks that fail (no tree walk, no subprocess), else None."""
    try:
        from . import doctor

        failed = doctor.cheap_failures(root)
    except Exception:
        return None
    if not failed:
        return None
    return DOCTOR_LINE % (" and ".join(failed[:3]), "check fails" if len(failed) == 1 else "checks fail")


def bounded(lines: Sequence[Any]) -> List[str]:
    """At most ``MAX_LINES`` non-blank lines of at most ``LINE_WIDTH`` characters, under ``MAX_CHARS`` in all."""
    cleaned = [_clean(line) for line in list(lines)[:MAX_LINES] if str(line).strip()]
    while cleaned and len("\n".join(cleaned)) + 1 > MAX_CHARS:
        cleaned.pop()
    return cleaned


def lines_for(cwd: Optional[str] = None, env: Optional[Mapping[str, str]] = None) -> List[str]:
    """What the hook prints: topic lines, template lines or nothing; never raises. A failing cheap doctor check
    adds one line after the first only when the lines stay within ``MAX_LINES`` and ``MAX_CHARS`` (it never
    pushes out a line of its own)."""
    try:
        where, root = locate(cwd, env)
        if where == "template":
            out = template_lines(root)
        elif where == "topic" and root:
            try:
                out = topic_lines(root, env)
            except Exception:
                out = ["Topic ontology here; use the onto skill (onto_status) to see where it stands."]
        else:
            return []
        extra = doctor_line(root)
    except Exception:
        return []
    cleaned = bounded(out)
    if extra and cleaned and len(cleaned) < MAX_LINES:
        joined = cleaned[:1] + [_clean(extra)] + cleaned[1:]
        if len("\n".join(joined)) + 1 <= MAX_CHARS:
            cleaned = joined
    return cleaned


def quiet_stdout(out: Any = None) -> None:
    """After a failed write to the real stdout (a closed pipe), point its file descriptor at the null device, so
    the flush at interpreter exit has somewhere to go instead of failing again (which would make the exit code
    120). Another stream (a test's) is left alone."""
    out = sys.stdout if out is None else out
    if out is not sys.stdout and out is not sys.__stdout__:
        return
    try:
        fd = out.fileno()
        null = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(null, fd)
        finally:
            os.close(null)
    except Exception:
        pass


def format_arg(argv: Optional[Sequence[str]]) -> str:
    """The ``--format`` value (``--format json`` or ``--format=json``, any case) when it names one of ``FORMATS``,
    else ``text``. Other arguments are ignored: the hook never fails over its command line."""
    args = [str(a) for a in (argv or ())]
    value = None
    for i, arg in enumerate(args):
        if arg == "--format":
            value = args[i + 1] if i + 1 < len(args) else None
        elif arg.startswith("--format="):
            value = arg[len("--format="):]
    value = (value or "").strip().lower()
    return value if value in FORMATS else "text"


def render_output(text: str, fmt: str) -> str:
    """What the hook writes for ``text`` (the joined lines; empty prints nothing) in ``fmt``."""
    if not text:
        return ""
    if fmt == "json":
        payload = {"hookSpecificOutput": {"hookEventName": EVENT, "additionalContext": text}}
        return json.dumps(payload, ensure_ascii=True, sort_keys=False) + "\n"
    return text + "\n"


def relay(argv: Optional[Sequence[str]] = None, cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None,
          stdout: Any = None, run: Any = None) -> bool:
    """``--format json`` in a topic that vendors another kit (``handoff.target``): run that kit's hook as a child in
    ``text`` (no arguments, ``ONTO_HANDOFF=1``, no stdin, stderr dropped) and write its lines, ``bounded``, in the
    JSON envelope. True when that is done (an empty reply prints nothing); False when there is no other kit, the
    format is ``text``, or the child fails or runs past ``RELAY_TIMEOUT``, so the caller carries on with this kit.
    ``run`` is a seam for ``subprocess.run``."""
    if format_arg(argv) != "json":
        return False
    from . import handoff

    environ = dict(os.environ if env is None else env)
    try:
        launcher = handoff.target([], handoff.HOOK_ENTRY, cwd, environ)
    except Exception:
        return False
    if not launcher:
        return False
    environ[handoff.GUARD] = "1"
    try:
        proc = (run or subprocess.run)([sys.executable or "python3", launcher], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environ, cwd=cwd,
                                       timeout=RELAY_TIMEOUT)
        if proc.returncode != 0:
            return False
        text = proc.stdout.decode("utf-8", "replace") if isinstance(proc.stdout, bytes) else str(proc.stdout or "")
    except Exception:
        return False
    out = stdout if stdout is not None else sys.stdout
    try:
        data = render_output("\n".join(bounded(text.splitlines())), "json")
        if data:
            out.write(data)
            out.flush()
    except Exception:
        quiet_stdout(out)
    return True


def main(argv: Optional[Sequence[str]] = None, cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None,
         stdout: Any = None) -> int:
    """Print the hook lines in the ``--format`` asked for (``format_arg``); always exit 0."""
    out = stdout if stdout is not None else sys.stdout
    try:
        data = render_output("\n".join(lines_for(cwd, env)), format_arg(argv))
        if data:
            out.write(data)
            out.flush()
    except Exception:
        quiet_stdout(out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
