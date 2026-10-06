"""Token-cost benchmark: what standard agent tasks cost through the MCP server, framing included.

``onto bench`` starts the kit's MCP server (``bin/onto-mcp``) over stdio in an empty temporary folder with a
scrubbed environment (``SCRUBBED_ENV`` removed, ``ONTO_HANDOFF=1`` so the kit under test serves), runs each task's
calls and counts characters: every ``tools/call`` request line plus its response line (``chars``), and the text an
agent reads (``text_chars``). It also records each profile's fixed overhead: the ``tools/list`` response line plus
the server instructions. Tokens are an estimate: characters / 4, rounded half up (``estimate_tokens``), never a
tokenizer count.

Tasks come from ``--tasks FILE`` (``{"tasks": [{"id", "title", "calls": [[tool, args], ...], "expect": [...]}],
"profile": "query"}``, or the bare list), else they are generated from the graph (``default_tasks``): status, a
brief, a search, a get, the neighbors and the card of the best connected node, the context for a deliverable, the
gaps and the decisions. Each task lists the ids its results must hold; a missed one fails the run (exit 1).

The data is a copy of the topic repo's data files (the benchmark writes nothing to the topic), or the kit's own
``tests/fixtures/mini`` when no topic repo is found, as on the template branch. The clock is pinned to
``$ONTO_FIXED_NOW``, else the time of the topic's last change, so equal data gives equal counts.

The kit measured is the running one, or with ``--ref`` the kit at that commit of the kit's git repo (``git archive``
into a temporary folder, never the working tree, so uncommitted changes are not measured). ``--against FILE``
compares with an earlier result (a golden file): a task or a profile overhead more than ``TOLERANCE`` above it is a
regression and fails the run.
"""

from __future__ import annotations

import io
import json
import os
import re
import select
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import commands, gitutil, ledger, mcp_server, store
from .errors import DataError, GitError, OntoError, UsageError

KIT_REL = "plugins/general-ontology"
KIT_CODE = (KIT_REL + "/ontokit", KIT_REL + "/bin")  # what the server runs; a change here dates a run
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
FIXTURE = os.path.join(PLUGIN_DIR, "tests", "fixtures", "mini")
CHARS_PER_TOKEN = 4
PROTOCOL = "2025-06-18"
CLIENT = {"name": "onto-bench", "version": "1"}
TIMEOUT_S = 120.0
TOLERANCE = 0.10
# The environment that would change what a server serves; the benchmark passes --repo and --profile instead.
SCRUBBED_ENV = ("ONTO_REPO", "ONTO_PROFILE", "CLAUDE_PROJECT_DIR", "ONTO_HANDOFF", "ONTO_MCP_REEXEC")
REF_OK = re.compile(r"^(HEAD|v[0-9]+|[0-9a-f]{7,40})$")
DATA_ROOT_FILES = ("ontology.json", "MANIFEST.json", "VERSIONS.md")

Call = Tuple[str, Dict[str, Any]]

METHOD = {
    "transport": "The kit's bin/onto-mcp over stdio (newline-delimited JSON-RPC 2.0), started with --repo and "
    "--profile in an empty folder with a scrubbed environment; one server per profile.",
    "chars": "Characters, not bytes. A call costs its tools/call request line plus its response line; text_chars "
    "is the result text alone.",
    "tokens": "Estimated: characters / 4, rounded half up. Not a tokenizer count.",
    "overhead": "Per session and profile: the tools/list response line plus the server instructions.",
    "chars_per_token": CHARS_PER_TOKEN,
}


class BenchmarkError(OntoError):
    """A server failed, or a task's call failed at the protocol level; the message says which."""

    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message, "bench", 1, **extra)


def estimate_tokens(chars: int) -> int:
    """Characters / ``CHARS_PER_TOKEN``, rounded half up (an estimate, not a tokenizer count)."""
    return (2 * int(chars) + CHARS_PER_TOKEN) // (2 * CHARS_PER_TOKEN)


def median(values: Sequence[float]) -> Any:
    """The median; a whole number comes back as an int."""
    if not values:
        return 0
    mid = statistics.median(values)
    return int(mid) if float(mid).is_integer() else mid


# MCP over stdio --------------------------------------------------------------------------------------------------
def server_env(fixed_now: Optional[str] = None) -> Dict[str, str]:
    """The server's environment: this one without ``SCRUBBED_ENV``, with no bytecode, no handoff and the clock
    pinned when ``fixed_now`` is given."""
    env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["ONTO_HANDOFF"] = "1"  # the kit under test serves, never the kit vendored in the data
    if fixed_now:
        env["ONTO_FIXED_NOW"] = fixed_now
    return env


class McpSession(object):
    """One MCP server process spoken to over stdio. Every request and response line is kept as sent and received.

    The server runs in an empty temporary folder with ``server_env``, so neither the working directory nor the
    environment picks its data or its profile: ``--repo`` and ``--profile`` do."""

    def __init__(self, kit_dir: str, repo: str, profile: str = "query", python: Optional[str] = None,
                 fixed_now: Optional[str] = None) -> None:
        self.kit_dir = kit_dir
        self.repo = repo
        self.profile = profile
        self.python = python or sys.executable or "python3"
        self.fixed_now = fixed_now
        self.proc: Optional[subprocess.Popen] = None
        self._buf = b""
        self._next_id = 0
        self._cwd: Optional[str] = None
        self._stderr: Any = None

    def __enter__(self) -> "McpSession":
        self._cwd = tempfile.mkdtemp(prefix="onto-bench-cwd-")
        self._stderr = tempfile.TemporaryFile()
        cmd = [self.python, os.path.join(self.kit_dir, "bin", "onto-mcp"), "--repo", self.repo,
               "--profile", self.profile]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
                                     cwd=self._cwd, env=server_env(self.fixed_now))
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        if self.proc is not None:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()
                self.proc.wait()
            self.proc.stdout.close()
            self.proc = None
        if self._stderr is not None:
            self._stderr.close()
            self._stderr = None
        if self._cwd is not None:
            shutil.rmtree(self._cwd, True)
            self._cwd = None

    def stderr_tail(self, limit: int = 2000) -> str:
        if self._stderr is None:
            return ""
        self._stderr.seek(0)
        return self._stderr.read().decode("utf-8", "replace")[-limit:]

    def _send(self, message: Dict[str, Any]) -> str:
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        assert self.proc is not None
        self.proc.stdin.write((line + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        return line

    def _read_line(self, timeout: float = TIMEOUT_S) -> str:
        assert self.proc is not None
        fd = self.proc.stdout.fileno()
        deadline = time.monotonic() + timeout
        while b"\n" not in self._buf:
            left = deadline - time.monotonic()
            ready = select.select([fd], [], [], max(left, 0))[0] if left > 0 else []
            if not ready:
                raise BenchmarkError("the server sent no reply in %.0fs; its log: %s" % (timeout, self.stderr_tail()))
            chunk = os.read(fd, 65536)
            if not chunk:
                raise BenchmarkError("the server closed its output; its log: %s" % self.stderr_tail())
            self._buf += chunk
        raw, self._buf = self._buf.split(b"\n", 1)
        return raw.decode("utf-8")

    def request(self, method: str, params: Dict[str, Any]) -> Tuple[str, str, Dict[str, Any]]:
        """``(request line, response line, response)`` for one request; the lines without their newline."""
        self._next_id += 1
        sent = self._send({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})
        line = self._read_line()
        reply = json.loads(line)
        if reply.get("id") != self._next_id:
            raise BenchmarkError("reply id %r for request %d" % (reply.get("id"), self._next_id))
        return sent, line, reply

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def initialize(self) -> Dict[str, Any]:
        _sent, line, reply = self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                                         "clientInfo": CLIENT})
        if "error" in reply:
            raise BenchmarkError("initialize failed: %s" % reply["error"])
        self.notify("notifications/initialized")
        return {"line": line, "result": reply["result"]}


def result_text(result: Dict[str, Any]) -> str:
    return "".join(c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text")


def run_calls(session: McpSession, calls: Sequence[Call], expect: Sequence[str] = ()) -> Dict[str, Any]:
    """Run one task's calls in a session: its steps, totals and the expected ids its results hold or miss."""
    steps, texts = [], []
    for tool, arguments in calls:
        sent, line, reply = session.request("tools/call", {"name": tool, "arguments": arguments})
        if "error" in reply:
            raise BenchmarkError("%s %s: %s" % (tool, json.dumps(arguments, sort_keys=True),
                                                reply["error"].get("message")))
        result = reply.get("result") or {}
        text = result_text(result)
        texts.append(text)
        steps.append({"tool": tool, "arguments": arguments, "request_chars": len(sent), "result_chars": len(line),
                      "text_chars": len(text), "is_error": bool(result.get("isError"))})
    joined = "\n".join(texts)
    chars = sum(s["request_chars"] + s["result_chars"] for s in steps)
    return {
        "calls": len(steps),
        "chars": chars,
        "tokens": estimate_tokens(chars),
        "text_chars": sum(s["text_chars"] for s in steps),
        "errors": sum(1 for s in steps if s["is_error"]),
        "found": [e for e in expect if e in joined],
        "missed": [e for e in expect if e not in joined],
        "steps": steps,
    }


def overhead(profile: str, init: Dict[str, Any], tools_line: str, tools: int) -> Dict[str, Any]:
    instructions = str(init["result"].get("instructions") or "")
    tools_tokens, instructions_tokens = estimate_tokens(len(tools_line)), estimate_tokens(len(instructions))
    return {
        "profile": profile,
        "tools": tools,
        "tools_list_chars": len(tools_line),
        "instructions_chars": len(instructions),
        "tools_list_tokens": tools_tokens,
        "instructions_tokens": instructions_tokens,
        "tokens": tools_tokens + instructions_tokens,
    }


# tasks ------------------------------------------------------------------------------------------------------------
def normalize_tasks(tasks: Any) -> List[Dict[str, Any]]:
    """Tasks as ``[{id, title, calls: [(tool, args)], expect}]``; ``UsageError`` on a malformed task."""
    if isinstance(tasks, dict):
        tasks = tasks.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise UsageError("bench: tasks must be a non-empty list of {id, calls, expect}")
    out, seen = [], set()
    for i, task in enumerate(tasks):
        if not isinstance(task, dict) or not isinstance(task.get("calls"), list) or not task["calls"]:
            raise UsageError("bench: task %d needs calls: [[tool, arguments], ...]" % (i + 1))
        tid = str(task.get("id") or "task-%d" % (i + 1))
        if tid in seen:
            raise UsageError("bench: two tasks are called %s" % tid)
        seen.add(tid)
        calls = []
        for call in task["calls"]:
            if isinstance(call, dict):
                call = [call.get("tool"), call.get("arguments") or {}]
            if not (isinstance(call, (list, tuple)) and len(call) == 2 and isinstance(call[0], str)
                    and isinstance(call[1], dict)):
                raise UsageError("bench: task %s: each call is [tool, {arguments}]" % tid)
            calls.append((call[0], dict(call[1])))
        expect = task.get("expect") or []
        if not isinstance(expect, list) or not all(isinstance(e, str) for e in expect):
            raise UsageError("bench: task %s: expect must be a list of strings" % tid)
        out.append({"id": tid, "title": str(task.get("title") or tid), "calls": calls, "expect": list(expect)})
    return out


def task_profile(tasks: Sequence[Dict[str, Any]]) -> str:
    """``full`` when any call needs a write tool, else ``query``."""
    for task in tasks:
        for tool, _args in task["calls"]:
            cmd = next((c for c in commands.COMMANDS if c.tool == tool), None)
            if cmd is not None and cmd.profile == "full":
                return "full"
    return "query"


def default_tasks(onto: Any) -> List[Dict[str, Any]]:
    """Standard tasks generated from the graph (see the module docstring). Titles name ids only."""
    ns = onto.ns
    topic = "topic:%s" % ns
    local = onto.local_nodes(active_only=True)
    ranked = sorted(local, key=lambda n: (-onto.degree(n), n))
    hub = next((n for n in ranked if n != topic), topic if topic in onto.nodes else None)
    tasks: List[Dict[str, Any]] = [
        {"id": "status", "title": "Where the topic stands", "calls": [("onto_status", {})], "expect": [ns]}]
    if hub:
        name = str((onto.node(hub) or {}).get("name") or hub)
        tasks += [
            {"id": "brief", "title": "Brief on %s" % hub, "calls": [("onto_brief", {"subject": hub})],
             "expect": [hub]},
            {"id": "search", "title": "Search for the name of %s" % hub, "calls": [("onto_search", {"text": name})],
             "expect": [hub]},
            {"id": "get", "title": "Get %s" % hub, "calls": [("onto_get", {"id": hub})], "expect": [hub]},
            {"id": "neighbors", "title": "Neighbors of %s within 2 hops" % hub,
             "calls": [("onto_neighbors", {"id": hub, "depth": 2})], "expect": [hub]},
        ]
    card = next((n for n in ranked if onto.registry.has_card(onto.kind_of(n))
                 and (onto.node(n) or {}).get("visibility", "shared") == "shared"), None)
    if card:
        tasks.append({"id": "card", "title": "Card of %s" % card, "calls": [("onto_card", {"id": card})],
                      "expect": [card]})
    deliverables = onto.registry.deliverables()
    template = sorted(deliverables)[0] if deliverables else None
    goal = next((n for n in ranked if onto.kind_of(n) == "goal"), None)
    context_args: Dict[str, Any] = {"task": "write the %s" % (template or "topic brief")}
    if template:
        context_args["deliverable"] = template
    tasks.append({"id": "context", "title": "Context for writing the %s deliverable" % (template or "brief"),
                  "calls": [("onto_context", context_args)], "expect": [goal] if goal else []})
    tasks.append({"id": "gaps", "title": "What is missing", "calls": [("onto_gaps", {})], "expect": []})
    decisions = ledger.read_decisions(onto.repo, active=True)
    tasks.append({"id": "decisions", "title": "Active decisions before proposing",
                  "calls": [("onto_decisions", {})], "expect": [decisions[0]["id"]] if decisions else []})
    return tasks


# measuring --------------------------------------------------------------------------------------------------------
def run_tasks(tasks: Any, kit_dir: str, repo: str, profile: Optional[str] = None, python: Optional[str] = None,
              fixed_now: Optional[str] = None) -> Dict[str, Any]:
    """Measure ``tasks`` with the kit at ``kit_dir`` against the topic at ``repo``: each profile's overhead, then
    every task in ``profile`` (default ``task_profile``). Returns ``{method, kit, profile, overhead, tasks, totals,
    version_line}``."""
    tasks = normalize_tasks(tasks)
    profile = profile or task_profile(tasks)
    out: Dict[str, Any] = {"method": dict(METHOD), "kit": {"version": None, "ref": None, "commit": None},
                           "profile": profile, "overhead": [], "tasks": []}
    for prof in mcp_server.PROFILES:
        with McpSession(kit_dir, repo, prof, python, fixed_now) as session:
            init = session.initialize()
            out["kit"]["version"] = str((init["result"].get("serverInfo") or {}).get("version", ""))
            _sent, tools_line, reply = session.request("tools/list", {})
            listed = len((reply.get("result") or {}).get("tools") or [])
            out["overhead"].append(overhead(prof, init, tools_line, listed))
            if prof != profile:
                continue
            for task in tasks:
                row = run_calls(session, task["calls"], task["expect"])
                out["tasks"].append(dict({"id": task["id"], "title": task["title"]}, **row))
            _sent, _line, reply = session.request("tools/call", {"name": "onto_status", "arguments": {}})
            text = result_text(reply.get("result") or {})
            out["version_line"] = text.splitlines()[0] if text else ""
    rows = out["tasks"]
    out["totals"] = {
        "tasks": len(rows),
        "calls": sum(t["calls"] for t in rows),
        "chars": sum(t["chars"] for t in rows),
        "tokens": sum(t["tokens"] for t in rows),
        "text_chars": sum(t["text_chars"] for t in rows),
        "median_tokens": median([t["tokens"] for t in rows]),
        "median_calls": median([t["calls"] for t in rows]),
        "missed": sum(len(t["missed"]) for t in rows),
    }
    return out


def kit_git_root(kit_dir: str = PLUGIN_DIR) -> str:
    root = gitutil.git(kit_dir, "rev-parse", "--show-toplevel")
    if not root:
        raise GitError("the kit at %s is not inside a git repo, so --ref has nothing to read" % kit_dir)
    return root


def resolve(git_root: str, ref: str) -> str:
    """The commit of ``HEAD``, a ``vN`` tag or a commit id (anything else is refused before git runs)."""
    if not isinstance(ref, str) or not REF_OK.fullmatch(ref):
        raise UsageError("bench --ref takes HEAD, a vN tag or a commit id, not %r" % (ref,))
    if ref == "HEAD":
        commit = gitutil.git(git_root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
        if not gitutil.COMMIT_RE.fullmatch(commit):
            raise GitError("HEAD is not a commit in %s" % git_root)
        return commit
    return gitutil.resolve_ref(git_root, ref)


def kit_commit(git_root: str, commit: str) -> Tuple[str, str]:
    """``(short sha, commit time)`` of the last commit at ``commit`` that changed the kit's code."""
    line = gitutil.git(git_root, "log", "-1", "--format=%h %cI", commit, "--", *KIT_CODE)
    if not line:
        raise GitError("no commit at %s changed %s" % (commit[:12], ", ".join(KIT_CODE)))
    sha, when = line.split()
    return sha, when


def extract_kit(git_root: str, commit: str, dest: str) -> str:
    """The kit at ``commit`` (``git archive``), unpacked under ``dest``; returns its folder. An archive member that
    is absolute, climbs out with ``..`` or is a link is refused."""
    proc = gitutil._run(git_root, ["archive", "--format=tar", commit, KIT_REL], timeout=120)
    if proc is None or proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip() if proc is not None else "git did not run"
        raise GitError("git archive %s failed: %s" % (commit[:12], detail))
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
        members = tar.getmembers()
        for member in members:
            parts = member.name.replace("\\", "/").split("/")
            if member.name.startswith("/") or ".." in parts or member.issym() or member.islnk() or member.isdev():
                raise BenchmarkError("unsafe path in the archive: %s" % member.name)
        tar.extractall(dest, members=members)
    return os.path.join(dest, *KIT_REL.split("/"))


def measure_ref(git_root: str, ref: str, tasks: Any, repo: str, profile: Optional[str] = None,
                python: Optional[str] = None, fixed_now: Optional[str] = None) -> Dict[str, Any]:
    """``run_tasks`` with the kit at ``ref`` of ``git_root`` (committed files only)."""
    commit = resolve(git_root, ref)
    sha, at = kit_commit(git_root, commit)
    tmp = tempfile.mkdtemp(prefix="onto-bench-kit-")
    try:
        kit_dir = extract_kit(git_root, commit, tmp)
        result = run_tasks(tasks, kit_dir, repo, profile, python, fixed_now)
    finally:
        shutil.rmtree(tmp, True)
    result["kit"].update({"ref": ref, "commit": sha, "at": at})
    return result


# data and comparison ----------------------------------------------------------------------------------------------
def copy_data(root: str, dest: str) -> str:
    """A copy of a topic's data files (``ontology.json``, the release files and the topic folders) under ``dest``;
    returns its root. Git, the inbox and ``.onto`` are left behind."""
    target = os.path.join(dest, "topic")
    os.makedirs(target)
    for name in DATA_ROOT_FILES:
        src = os.path.join(root, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(target, name))
    for folder in store.TOPIC_DIRS:
        src = os.path.join(root, folder)
        if os.path.isdir(src) and not os.path.islink(src):
            shutil.copytree(src, os.path.join(target, folder), symlinks=True)
    return target


def pinned_clock(root: str) -> Optional[str]:
    """``$ONTO_FIXED_NOW``, else the time of the topic's last change, else None."""
    fixed = os.environ.get("ONTO_FIXED_NOW")
    if fixed and not store.is_placeholder(fixed):
        return fixed
    rows, _problems = store.read_jsonl(os.path.join(root, "ledger", "changes.jsonl"))
    times = sorted(str(r.get("at")) for r in rows if r.get("at"))
    return times[-1] if times else None


def load_json_file(path: str, what: str) -> Any:
    real = store.guard_input_path(path, None, allow_any=True)
    try:
        with open(real, encoding="utf-8") as fh:
            return json.load(fh)
    except OSError as exc:
        raise UsageError("bench: cannot read the %s file %s: %s" % (what, path, exc.strerror or exc))
    except ValueError as exc:
        raise UsageError("bench: the %s file %s is not JSON (%s)" % (what, path, exc))


def compare(result: Dict[str, Any], golden: Any, tolerance: float = TOLERANCE) -> Dict[str, Any]:
    """Tokens per task and overhead per profile against a golden result; a value more than ``tolerance`` above
    the golden one is a regression."""
    if isinstance(golden, dict) and isinstance(golden.get("result"), dict):
        golden = golden["result"]
    if not isinstance(golden, dict):
        raise UsageError("bench --against: the golden file must hold a bench result object")
    for key, kind in (("tasks", list), ("overhead", list), ("totals", dict)):
        if golden.get(key) is not None and not isinstance(golden[key], kind):
            raise UsageError("bench --against: %s in the golden file must be %s" % (
                key, "a list" if kind is list else "an object"))
    old_tasks = {t["id"]: t for t in golden.get("tasks") or [] if isinstance(t, dict) and isinstance(t.get("id"), str)}
    old_over = {o["profile"]: o for o in golden.get("overhead") or []
                if isinstance(o, dict) and isinstance(o.get("profile"), str)}
    rows, regressions = [], []

    def golden_tokens(what: str, then: Any) -> Optional[int]:
        """A golden token count: None when absent, else a whole number; anything else is a usage error."""
        if then is None:
            return None
        if isinstance(then, float) and then.is_integer():
            then = int(then)
        if isinstance(then, bool) or not isinstance(then, int) or then < 0:
            raise UsageError("bench --against: tokens for %s must be a whole number, not %s"
                             % (what, json.dumps(then)[:40]))
        return then

    def check(what: str, now: int, then: Any) -> Dict[str, Any]:
        then = golden_tokens(what, then)
        row = {"what": what, "tokens": now, "golden": then, "delta": None if then is None else now - then}
        if then is not None and now > then * (1 + tolerance):
            row["regression"] = True
            regressions.append(what)
        return row

    for task in result.get("tasks") or []:
        then = old_tasks.get(task["id"], {}).get("tokens")
        rows.append(check("task %s" % task["id"], task["tokens"], then))
    for over in result.get("overhead") or []:
        then = old_over.get(over["profile"], {}).get("tokens")
        rows.append(check("overhead %s" % over["profile"], over["tokens"], then))
    missing = sorted(str(k) for k in old_tasks if k not in {t["id"] for t in result.get("tasks") or []})
    old_total = (golden.get("totals") or {}).get("tokens")
    return {"rows": rows, "regressions": regressions, "missing": missing, "tolerance": tolerance,
            "totals": {"tokens": (result.get("totals") or {}).get("tokens"), "golden": old_total}}


# the command -------------------------------------------------------------------------------------------------------
def cmd_bench(ctx: commands.Context, args: Dict[str, Any]) -> Dict[str, Any]:
    """``onto bench [--tasks FILE] [--against FILE] [--ref REF]``: measure, compare, and exit 1 on a missed id or
    a regression."""
    tmp = tempfile.mkdtemp(prefix="onto-bench-data-")
    try:
        if ctx.has_repo():
            source, data = ctx.repo.root, "topic %s" % ctx.repo.ns
        elif os.path.isfile(os.path.join(FIXTURE, "ontology.json")):
            source, data = FIXTURE, "the kit's mini fixture (no topic repo here)"
        else:
            raise DataError("bench needs a topic repo (run it inside one, or pass --repo)")
        repo = copy_data(source, tmp)
        fixed_now = pinned_clock(repo)
        if args.get("tasks"):
            tasks = normalize_tasks(load_json_file(args["tasks"], "tasks"))
        else:
            from .graph import Ontology

            tasks = default_tasks(Ontology.load(store.Repo.open(repo)))
        if args.get("ref"):
            result = measure_ref(kit_git_root(), args["ref"], tasks, repo, fixed_now=fixed_now)
        else:
            result = run_tasks(tasks, PLUGIN_DIR, repo, fixed_now=fixed_now)
    finally:
        shutil.rmtree(tmp, True)
    result["data"] = data
    result["clock"] = fixed_now
    if args.get("against"):
        result["against"] = dict(compare(result, load_json_file(args["against"], "golden")), file=args["against"])
    failed = result["totals"]["missed"] or (result.get("against") or {}).get("regressions")
    result["exit_code"] = 1 if failed else 0
    return result


def _pct(before: Any, after: int) -> str:
    return "-" if not before else "%+d%%" % round(100.0 * (after - int(before)) / int(before))


def render_bench(result: Dict[str, Any], mode: str, ctx: Any) -> List[str]:
    kit = result.get("kit") or {}
    lines = ["bench: kit %s%s over %s; %s; tokens are characters / %d, rounded half up" % (
        kit.get("version"), " at %s (%s)" % (kit.get("ref"), kit.get("commit")) if kit.get("ref") else "",
        result.get("data"), result.get("version_line") or "", CHARS_PER_TOKEN)]
    lines.append("%-12s %5s %7s %7s  %s" % ("task", "calls", "chars", "tokens", "missed"))
    for task in result.get("tasks") or []:
        lines.append("%-12s %5d %7d %7d  %s" % (task["id"], task["calls"], task["chars"], task["tokens"],
                                                  ", ".join(task["missed"]) or "-"))
        if mode == "text":
            for step in task.get("steps") or []:
                lines.append("  %s %s: %d + %d chars%s" % (
                    step["tool"], json.dumps(step["arguments"], sort_keys=True), step["request_chars"],
                    step["result_chars"], " (error)" if step["is_error"] else ""))
    totals = result.get("totals") or {}
    lines.append("%-12s %5d %7d %7d  median %s tokens per task" % (
        "total", totals.get("calls", 0), totals.get("chars", 0), totals.get("tokens", 0),
        totals.get("median_tokens")))
    for over in result.get("overhead") or []:
        lines.append("overhead %-5s %2d tools: tools/list %d chars + instructions %d chars = %d tokens" % (
            over["profile"], over["tools"], over["tools_list_chars"], over["instructions_chars"], over["tokens"]))
    against = result.get("against")
    if against:
        lines.append("against %s (tolerance %d%%):" % (against.get("file"), round(100 * against["tolerance"])))
        for row in against["rows"]:
            if row["golden"] is None:
                lines.append("  %s: %d tokens (new)" % (row["what"], row["tokens"]))
            elif row["delta"] or row.get("regression"):
                lines.append("  %s: %d -> %d tokens %s%s" % (row["what"], row["golden"], row["tokens"],
                                                           _pct(row["golden"], row["tokens"]),
                                                           "  REGRESSION" if row.get("regression") else ""))
        if against.get("missing"):
            lines.append("  tasks in the golden file only: %s" % ", ".join(against["missing"]))
        if not against["regressions"]:
            lines.append("  no regression")
    if totals.get("missed"):
        lines.append("FAILED: %d expected id(s) missed" % totals["missed"])
    return lines
