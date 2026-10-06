"""Test harness: puts the plugin folder on ``sys.path``, isolates the environment and builds throwaway topics.

Importing this module pops every ``ONTO_*`` variable except the test switches ``ONTO_SKIP_PERF`` and ``ONTO_DEBUG``
(and ``CLAUDE_PROJECT_DIR``) so the shell cannot steer a test,
then pins the clock with ``ONTO_FIXED_NOW``. Helpers build topics in temp folders, drive git with a local identity
and no signing or hooks, run the CLI and MCP server in process, and assemble fake secrets at run time so no
secret-like literal sits in the source.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)
sys.dont_write_bytecode = True  # keep the kit folder free of __pycache__ from test runs

FIXTURES = os.path.join(HERE, "fixtures")
FIXED_NOW = "2026-09-28T12:00:00Z"

KEEP_ENV = ("ONTO_SKIP_PERF", "ONTO_DEBUG", "ONTO_RECORD_GOLDEN")  # switches for the test run itself, not for the kit under test
for _name in [k for k in os.environ if k.startswith("ONTO_") and k not in KEEP_ENV]:
    os.environ.pop(_name, None)
os.environ.pop("CLAUDE_PROJECT_DIR", None)
os.environ["ONTO_FIXED_NOW"] = FIXED_NOW

GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_DATE": "2026-09-28T12:00:00Z",
    "GIT_COMMITTER_DATE": "2026-09-28T12:00:00Z",
}


class TempCase(unittest.TestCase):
    """A test case with a fresh temp folder in ``self.tmp``, removed afterwards."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="onto-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)


# topics --------------------------------------------------------------------------------------------------------
def bare_topic(tmp: str, ns: str = "mini", title: Optional[str] = None, name: Optional[str] = None) -> str:
    """A minimal topic repo written without ``mutate``: ``ontology.json``, the empty local pack and the topic
    folders, no records. Returns its root."""
    from ontokit import packs, store

    root = os.path.join(tmp, ns)
    os.makedirs(root, exist_ok=True)
    for folder in store.TOPIC_DIRS:
        os.makedirs(os.path.join(root, folder), exist_ok=True)
    store.write_json(os.path.join(root, store.MANIFEST),
                     store.new_manifest(name or "test-%s" % ns, ns, title or "Test %s" % ns))
    store.write_json(os.path.join(root, "packs", "local.pack.json"), packs.empty_local_pack())
    return root


def make_topic(tmp: str, fixture: str = "mini", ns: str = "mini") -> str:
    """A copy of ``fixtures/<fixture>`` under ``tmp/<ns>``; ``ontology.json`` gets ``ns`` when it differs.
    Returns its root."""
    source = os.path.join(FIXTURES, fixture)
    if not os.path.isdir(source):
        raise unittest.SkipTest("fixture %s is not built yet" % fixture)
    root = os.path.join(tmp, ns)
    shutil.copytree(source, root)
    path = os.path.join(root, "ontology.json")
    with open(path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    if manifest.get("ns") != ns:
        from ontokit import store

        manifest["ns"] = ns
        store.write_json(path, manifest)
    return root


def init_topic(tmp: str, ns: str, title: Optional[str] = None) -> str:
    """A topic created by ``mutate.init_topic`` under ``tmp/<ns>``. Returns its root."""
    from ontokit import mutate

    repo = mutate.init_topic(os.path.join(tmp, ns), "test-%s" % ns, ns, title or "Test %s" % ns)
    return repo.root


def snapshot(root: str, skip: Sequence[str] = ()) -> Dict[str, Tuple[int, str]]:
    """``{relative path: (size, sha256)}`` for every file under ``root``, to prove a call wrote nothing."""
    out: Dict[str, Tuple[int, str]] = {}
    for dirpath, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if any(rel == s or rel.startswith(s.rstrip("/") + "/") for s in skip):
                continue
            with open(full, "rb") as fh:
                data = fh.read()
            out[rel] = (len(data), hashlib.sha256(data).hexdigest())
    return out


# git -----------------------------------------------------------------------------------------------------------
def git(root: str, *args: str) -> str:
    env = dict(os.environ, **GIT_ENV)
    proc = subprocess.run(["git", "-C", root] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          stdin=subprocess.DEVNULL, env=env)
    if proc.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), proc.stderr.decode("utf-8", "replace")))
    return proc.stdout.decode("utf-8", "replace").strip()


def git_init(root: str) -> None:
    """``git init`` with branch main, a local test identity, no signing and no hooks."""
    os.makedirs(root, exist_ok=True)
    git(root, "init", "-q")
    git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    for key, value in (
        ("user.name", "Onto Test"),
        ("user.email", "test@example.invalid"),
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
        ("core.hooksPath", os.devnull),
        ("init.defaultBranch", "main"),
    ):
        git(root, "config", key, value)


def commit_all(root: str, msg: str) -> str:
    """Stage everything and commit; returns the new commit id."""
    git(root, "add", "-A")
    git(root, "commit", "-q", "--no-verify", "--allow-empty", "-m", msg)
    return git(root, "rev-parse", "HEAD")


def tag(root: str, name: str) -> None:
    """An annotated tag, as a release makes."""
    git(root, "tag", "-a", name, "-m", name)


# in-process CLI and MCP ----------------------------------------------------------------------------------------
def run_cli(args: Sequence[str], repo: Optional[str] = None) -> Tuple[int, str, str]:
    """``(exit code, stdout, stderr)`` of ``onto <args>`` run in process, with ``--repo`` when given."""
    from ontokit import cli

    out, err = io.StringIO(), io.StringIO()
    argv = list(args) + (["--repo", repo] if repo else [])
    code = cli.main(argv, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


_MCP_ID = [0]


def mcp_call(server: Any, tool: str, **args: Any) -> Dict[str, Any]:
    """The JSON-RPC ``result`` of one ``tools/call`` (raises ``AssertionError`` on a protocol error)."""
    _MCP_ID[0] += 1
    request = {"jsonrpc": "2.0", "id": _MCP_ID[0], "method": "tools/call", "params": {"name": tool, "arguments": args}}
    line = json.dumps(request)
    if hasattr(server, "handle_line"):
        reply = server.handle_line(line)
        text = reply if isinstance(reply, str) else json.dumps(reply)
    else:
        out = io.StringIO()
        server.serve(io.StringIO(line + "\n"), out)
        text = out.getvalue().strip().splitlines()[-1]
    message = json.loads(text)
    if "error" in message:
        raise AssertionError("MCP error: %s" % message["error"])
    return message["result"]


# fake secrets --------------------------------------------------------------------------------------------------
def _alnum(n: int) -> str:
    base = "Ab3dEf6hJk9mNp2qRs5tUv8wXy4z"
    return (base * (n // len(base) + 1))[:n]


def fake_secret(kind: str) -> str:
    """A string the ``kind`` secret pattern matches, assembled here so no literal secret sits in the source."""
    hexs = ("0123456789abcdef" * 3)[:32]
    builders = {
        "github": lambda: "gh" + "p_" + _alnum(24),
        "anthropic": lambda: "sk-" + "ant-" + "api03-" + _alnum(24),
        "openai": lambda: "sk-" + "proj" + _alnum(24),
        "google": lambda: "AI" + "za" + "Sy" + _alnum(33),
        "google_token": lambda: "ya" + "29." + _alnum(24),
        "aws": lambda: "AK" + "IA" + "QWERTYUIOPASDFGH",
        "private_key": lambda: "-----BEGIN " + "RSA PRIVATE" + " KEY-----",
        "slack": lambda: "xo" + "xb-" + "1234567890-" + _alnum(12),
        "slack_webhook": lambda: "hooks." + "slack.com/services/" + "T0001/" + "B0002/" + _alnum(24),
        "stripe": lambda: "sk" + "_live_" + _alnum(24),
        "linear": lambda: "lin" + "_api_" + _alnum(40),
        "gitlab": lambda: "gl" + "pat-" + _alnum(20),
        "jwt": lambda: "ey" + "J" + _alnum(12) + "." + "ey" + "J" + _alnum(12) + "." + _alnum(16),
        "npm": lambda: "np" + "m_" + _alnum(36) + " ",
        "sendgrid": lambda: "SG" + "." + _alnum(22) + "." + _alnum(43),
        "twilio": lambda: "S" + "K" + hexs + " ",
        "google_oauth": lambda: "GOC" + "SPX-" + _alnum(28),
        "huggingface": lambda: "hf" + "_" + _alnum(34),
        "digitalocean": lambda: "dop" + "_v1_" + hexs + hexs,
    }
    if kind not in builders:
        raise KeyError("no fake secret for %r; add one when a pattern is added" % kind)
    return builders[kind]()


def fake_secret_kinds() -> Iterable[str]:
    from ontokit import secrets

    return secrets.KINDS


# viewer decoy --------------------------------------------------------------------------------------------------
def page_html(payload: Any) -> str:
    """A page embedding ``payload`` the way the viewer does (JSON in ``<script id="data">`` with ``</`` escaped),
    with decoy scripts around it."""
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).replace("</", "<\\/")
    return (
        "<!doctype html><html><head><title>Viewer</title></head><body>"
        '<script>var early = "</b>";</script>'
        '<script id="data" type="application/json">%s</script>'
        "<script>boot();</script></body></html>" % body
    )
