"""``onto doctor``: read-only checks of the machine, the folder and the topic, each ``ok``, ``warn`` or ``fail`` with
one fix line. Exit 0 when nothing fails, else 1.

It works in a template checkout, in a topic and anywhere else (``where`` says which). The checks:

- ``python`` (3.9 or newer, naming the interpreter) and ``git`` (on ``PATH``, with its version).
- ``git_identity``: git can name the author and committer of a commit from a name and an email the user set
  (``git var`` with ``user.useConfigOnly``, so an identity git would guess from the account and the host name does
  not count); a ``warn`` with the ``git config --global`` fix when not. ``onto setup``'s preflight fails on it.
- ``where``: template checkout, topic or neither; the README's step 1 (branch and remotes); a template checkout that
  is not a git repo (a downloaded ZIP: a ``warn``, setup needs a clone); the vendored kit; kit skew between the
  running kit, the vendored kit and the kit ``ontology.json`` was written with.
- ``git_rules``: ``inbox/`` and ``.onto/`` ignored (``git check-ignore``), the ``.gitattributes`` merge rules present.
- ``cloud_sync``: the folder sits under ``~/Library/Mobile Documents`` or ``~/Library/CloudStorage``, or under
  ``~/Desktop`` or ``~/Documents`` while iCloud "Desktop and Documents" is on (a ``warn``).
- ``dataless``: a tracked file whose macOS dataless flag is set (evicted to the cloud; a ``fail``, since git and
  imports hang on it). The walk skips ``.git``, stops at the first hit and is bounded by ``MAX_WALK``. It runs in a
  topic or a template checkout only ("not checked" elsewhere, which may be ``$HOME`` itself).
- ``conflict_copies``: names such as ``main 2`` or ``nodes 2.jsonl`` inside ``.git/refs`` (a ``fail``: they break
  fetch) or in the work tree outside ``inbox/`` when the original sits next to the copy (a ``warn``; a topic or a
  template checkout only, as for ``dataless``).
- ``stale_locks``: 0-byte ``*.lock`` files under ``.git/refs``, or a ``index.lock``, ``HEAD.lock``,
  ``packed-refs.lock``, ``config.lock`` or ``shallow.lock`` in the git folder older than 10 minutes.
- ``plugin``: in a topic, ``.claude/settings.json`` names the marketplace and enables the plugin, or
  ``settings.local.json`` enables it, and the marketplace source is not a local folder; in a topic or a template
  checkout, with ``claude`` on ``PATH``, ``claude plugin marketplace list --json`` shows ``general-ontology`` and
  its source (a folder source is a ``warn``: it takes over the one marketplace of that name on the machine).
- ``agents`` (topics and template checkouts): ``.agents/skills`` equals the render of the vendored kit's skills
  (``ontokit.agents``), and every agent file ``onto setup --agent`` wires (``.devin/``, ``.codex/``, ``.cursor/``,
  ``.vscode/mcp.json``, ``.gemini/settings.json``) still holds the kit's entries as rendered (a ``warn`` otherwise).
- ``topic`` (topics): ``onto validate`` problems, pending proposals, uncommitted topic files, the last checkpoint.
  They read every graph and ledger file, so they are left out while any tracked file is evicted.

Nothing here writes or calls the network. ``$HOME`` is read from the environment, so tests can move it.
``cheap_failures`` is the subset the session-start hook runs: no tree walk, no subprocess.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import __version__, gitutil
from .commands import Context

MINIMUM = (3, 9)
MAX_WALK = 50000
LOCK_STALE_SECONDS = 600
TOP_LOCKS = ("index.lock", "HEAD.lock", "packed-refs.lock", "config.lock", "shallow.lock")
CLAUDE_TIMEOUT = 20
DATALESS = getattr(stat, "SF_DATALESS", 0x40000000)
CONFLICT_RE = re.compile(r"^.+ [0-9]+(\.[^. ]+)?$")
MARKETPLACE = "general-ontology"
PLUGIN_KEY = "general-ontology@general-ontology"
KIT_REL = ("plugins", "general-ontology")
TEMPLATE_BRANCH = "general-ontology"
VERSION_RE = re.compile(r'^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"', re.M)
PASSWORD_RE = re.compile(r"^[a-z][a-z0-9+.-]*://[^/@\s]*:[^/@\s]*@", re.I)  # git takes HTTPS:// as https://
HTTP_USER_RE = re.compile(r"^https?://[^/@\s]+@", re.I)
Q_PLUGIN = "Install the plugin for this repo (recommended), or run it without installing?"
Q_AGENTS = "Which agents will open this topic?"
MOVE_FIX = "move the repo out of the synced folder, for example to ~/Ontologies"
REPAIR = ("run /plugin marketplace remove general-ontology, then add it again from a git URL "
          "(/plugin marketplace add <repo-url>#general-ontology) and install the plugin in each topic")
KIT_OWNED = ("plugins/", ".claude-plugin/", ".github/", "examples/", "README.md", "AGENTS.md", "CLAUDE.md",
             ".gitignore", ".gitattributes", "new-topic", "New topic.command", "new-topic.cmd", ".claude/", ".agents/")

SETTINGS_REL = ".claude/settings.json"

_lstat = os.lstat  # tests swap it to fake the dataless flag


def check(cid: str, status: str, detail: str, fix: str = "") -> Dict[str, Any]:
    return {"id": cid, "status": status, "detail": detail, "fix": fix}


def home_dir(env: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if env is None else env
    return os.path.abspath(env.get("HOME") or os.path.expanduser("~"))


_CASE_FOLD = sys.platform == "darwin" or os.name == "nt"  # their file systems ignore case by default
_cmd_shell = lambda: os.name == "nt"  # noqa: E731  the printed commands are for cmd.exe there (tests patch it)
_CMD_SAFE_RE = re.compile(r"^[A-Za-z0-9_\-.:\\/=+@,]+\Z")


def shell_quote(text: str) -> str:
    """``text`` quoted for a command line the user copies: POSIX single quotes (``shlex.quote``), or on Windows
    double quotes, which cmd.exe reads (it takes single quotes as part of the word)."""
    if not _cmd_shell():
        return shlex.quote(text)
    text = str(text)
    return text if text and _CMD_SAFE_RE.match(text) else '"%s"' % text.replace('"', '\\"')


def cd_command(folder: str) -> str:
    """``cd <folder>``, with ``/d`` on Windows so cmd.exe changes the drive too."""
    return ("cd /d %s" if _cmd_shell() else "cd %s") % shell_quote(folder)


def _under(path: str, base: str) -> bool:
    """True when ``path`` is ``base`` or inside it. On macOS and Windows the names are compared without case, as
    their file systems do: ``~/desktop/x`` is inside ``~/Desktop``."""
    if _CASE_FOLD:
        path, base = path.casefold(), base.casefold()
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def _forms(path: str) -> List[str]:
    """``path`` as given (absolute) and with symlinks resolved (of its nearest existing parent when it is absent)."""
    absolute = os.path.abspath(os.path.expanduser(path))
    here, tail = absolute, ""
    while not os.path.exists(here) and os.path.dirname(here) != here:
        here, part = os.path.dirname(here), os.path.basename(here)
        tail = os.path.join(part, tail) if tail else part
    real = os.path.join(os.path.realpath(here), tail) if tail else os.path.realpath(here)
    return sorted({absolute, real})


def cloud_synced(path: str, env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Why ``path`` is inside a cloud-synced folder, or None."""
    home = home_dir(env)
    library = os.path.join(home, "Library")
    icloud = os.path.join(library, "Mobile Documents")
    for form in _forms(path):
        for base, what in ((icloud, "iCloud Drive"), (os.path.join(library, "CloudStorage"), "a cloud storage folder")):
            if _under(form, base):
                return "it is inside %s (%s)" % (what, base)
        for name in ("Desktop", "Documents"):
            if _under(form, os.path.join(home, name)) and os.path.isdir(
                    os.path.join(icloud, "com~apple~CloudDocs", name)):
                return "it is inside ~/%s, which iCloud \"Desktop and Documents\" syncs" % name
    return None


def _dataless(path: str) -> bool:
    try:
        return bool(getattr(_lstat(path), "st_flags", 0) & DATALESS)
    except OSError:
        return False


def dataless_hit(root: str, limit: int = MAX_WALK) -> Optional[str]:
    """The first tracked file (relative) whose dataless flag is set, or None. Tracked files come from
    ``git ls-files``; outside git, a walk that skips ``.git``. At most ``limit`` files are looked at."""
    names: List[str] = []
    proc = gitutil._run(root, ["ls-files", "-z"])
    if proc is not None and proc.returncode == 0:
        names = [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p][:limit]
    else:
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d != ".git")
            for name in sorted(files):
                names.append(os.path.relpath(os.path.join(dirpath, name), root))
                if len(names) >= limit:
                    break
            if len(names) >= limit:
                break
    for rel in names:
        if _dataless(os.path.join(root, *rel.split("/"))):
            return rel.replace(os.sep, "/")
    return None


def _git_dirs(root: str) -> Tuple[Optional[str], Optional[str]]:
    """(git dir, common git dir) of the repo at ``root``, read from the file system (no subprocess): ``.git`` is a
    folder, or a file naming the git dir (a worktree), whose ``commondir`` names the shared one."""
    dotgit = os.path.join(root, ".git")
    if os.path.isdir(dotgit):
        return dotgit, dotgit
    if not os.path.isfile(dotgit):
        return None, None
    try:
        with open(dotgit, encoding="utf-8") as fh:
            text = fh.read(4096).strip()
    except (OSError, UnicodeDecodeError):
        return None, None
    if not text.startswith("gitdir:"):
        return None, None
    gitdir = os.path.normpath(os.path.join(root, text[len("gitdir:"):].strip()))
    common = gitdir
    try:
        with open(os.path.join(gitdir, "commondir"), encoding="utf-8") as fh:
            common = os.path.normpath(os.path.join(gitdir, fh.read(4096).strip()))
    except (OSError, UnicodeDecodeError):
        pass
    return gitdir, common


def refs_conflicts(root: str, limit: int = MAX_WALK) -> List[str]:
    """Conflict copies (``<stem> <digit>`` or ``<stem> <digit>.<ext>``) under the repo's ``refs`` folder."""
    _gitdir, common = _git_dirs(root)
    hits: List[str] = []
    if not common:
        return hits
    refs = os.path.join(common, "refs")
    seen = 0
    for dirpath, dirs, files in os.walk(refs):
        for name in sorted(dirs) + sorted(files):
            seen += 1
            if CONFLICT_RE.match(name):
                hits.append(os.path.relpath(os.path.join(dirpath, name), common).replace(os.sep, "/"))
        if seen >= limit or len(hits) >= 5:
            break
    return hits[:5]


def tree_conflicts(root: str, limit: int = MAX_WALK) -> List[str]:
    """Conflict copies in the work tree outside ``.git`` and ``inbox/`` (at most 5, bounded walk): a name such as
    ``nodes 2.jsonl`` counts only when its original (``nodes.jsonl``) sits next to it, so the user's own
    ``Chapter 1.md`` or ``Budget 2025.xlsx`` is never one."""
    from . import store

    hits: List[str] = []
    seen = 0
    for dirpath, dirs, files in os.walk(root):
        top = os.path.normpath(dirpath) == os.path.normpath(root)
        dirs[:] = sorted(d for d in dirs if d != ".git" and not (top and d == "inbox"))
        for name in dirs + sorted(files):
            seen += 1
            if CONFLICT_RE.match(name) and store.is_conflict_copy(dirpath, name):
                hits.append(os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/"))
        if seen >= limit or len(hits) >= 5:
            break
    return hits[:5]


def stale_locks(root: str, now: Optional[float] = None) -> List[str]:
    gitdir, common = _git_dirs(root)
    found: List[str] = []
    if not gitdir or not common:
        return found
    for dirpath, _dirs, files in os.walk(os.path.join(common, "refs")):
        for name in sorted(files):
            path = os.path.join(dirpath, name)
            try:
                if name.endswith(".lock") and os.path.getsize(path) == 0:
                    found.append(os.path.relpath(path, common).replace(os.sep, "/"))
            except OSError:
                continue
    # the locks git keeps at the top of the git folder (and of the shared one, for a worktree): a stale HEAD.lock
    # makes every commit fail with "cannot lock ref HEAD"
    folders = [gitdir] + ([common] if os.path.normpath(common) != os.path.normpath(gitdir) else [])
    for folder in folders:
        for name in TOP_LOCKS:
            path = os.path.join(folder, name)
            try:
                age = (time.time() if now is None else now) - os.path.getmtime(path)
            except OSError:
                continue
            rel = name if folder == gitdir else os.path.relpath(path, gitdir).replace(os.sep, "/")
            if age > LOCK_STALE_SECONDS and rel not in found:
                found.append(rel)
    return found


def kit_version(root: str) -> Optional[str]:
    """The ``__version__`` of the kit vendored in ``root``, or None."""
    path = os.path.join(root, *KIT_REL, "ontokit", "__init__.py")
    try:
        with open(path, encoding="utf-8") as fh:
            found = VERSION_RE.search(fh.read(20000))
    except (OSError, UnicodeDecodeError):
        return None
    return found.group(1) if found else None


def _read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def local_source(url: Any) -> bool:
    """True for a kit URL that is a local folder (a path, ``file://``, ``~``), which must never become the
    marketplace: it would take over the one ``general-ontology`` marketplace per user."""
    if not isinstance(url, str) or not url.strip():
        return True
    text = url.strip()
    if text.startswith(("/", "./", "../", "~", "file:", ".")) or re.match(r"^[A-Za-z]:[\\/]", text):
        return True
    if re.match(r"^[a-z][a-z0-9+.-]*://", text) or re.match(r"^[^/@\s]+@[^:/\s]+:", text):
        return False
    return not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", text)


def _source_is_folder(source: Any) -> bool:
    if isinstance(source, dict):
        kind = str(source.get("source") or source.get("type") or "")
        if kind in ("directory", "file", "local", "path"):
            return True
        if kind in ("github", "git", "url", "npm"):
            return False
        inner = source.get("url") or source.get("repo") or source.get("path")
        return local_source(inner) if inner is not None else False
    if isinstance(source, str):
        return local_source(source.split("#", 1)[0])
    return False


def settings_wiring(root: str) -> Dict[str, Any]:
    """What ``.claude/settings.json`` and ``settings.local.json`` say about the plugin."""
    out: Dict[str, Any] = {"project": False, "local": False, "source": None, "folder_source": False}
    for name, key in (("settings.json", "project"), ("settings.local.json", "local")):
        data = _read_json(os.path.join(root, ".claude", name))
        if not isinstance(data, dict):
            continue
        enabled = data.get("enabledPlugins")
        markets = data.get("extraKnownMarketplaces")
        entry = markets.get(MARKETPLACE) if isinstance(markets, dict) else None
        on = isinstance(enabled, dict) and enabled.get(PLUGIN_KEY) is True
        if key == "project":
            out["project"] = on and isinstance(entry, dict)
        else:
            out["local"] = on
        if isinstance(entry, dict) and out["source"] is None:
            out["source"] = entry.get("source")
            out["folder_source"] = _source_is_folder(entry.get("source"))
    return out


def claude_marketplace(claude: str) -> Optional[Dict[str, Any]]:
    """``{found, folder, source}`` from ``claude plugin marketplace list --json``, or None when it cannot run."""
    try:
        proc = subprocess.run([claude, "plugin", "marketplace", "list", "--json"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=CLAUDE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace") or "null")
    except ValueError:
        return None
    items: List[Any] = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("marketplaces", "items", "data"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
        else:
            items = [dict(v, name=k) if isinstance(v, dict) else {"name": k, "source": v} for k, v in data.items()]
    for item in items:
        if isinstance(item, dict) and item.get("name") == MARKETPLACE:
            source = item.get("source")
            return {"found": True, "folder": _source_is_folder(source), "source": source}
    return {"found": False, "folder": False, "source": None}


# where ---------------------------------------------------------------------------------------------------------
def locate(start: str, env: Mapping[str, str], explicit: Optional[str] = None) -> Tuple[str, str]:
    """``(where, root)``: ``topic``, ``template`` or ``neither`` (with the git top or the folder itself). Only
    ``explicit`` (``--repo``) and ``start`` count: ``$ONTO_REPO`` and ``$CLAUDE_PROJECT_DIR`` never send doctor or
    setup to a topic elsewhere."""
    from . import hook, store

    try:
        root = store.find_root(explicit, start, {k: v for k, v in env.items()
                                                 if k not in (store.PROJECT_ENV, store.ENV_VAR)})
    except Exception:
        root = None
    if root and hook.checked_manifest(root) is not None:
        return "topic", os.path.abspath(root)
    if root and manifest_problem(root):
        return "topic", os.path.abspath(root)  # a topic whose ontology.json is broken: run_checks says so
    found = hook._template_above(start)
    if found:
        return "template", found
    top = gitutil.git(start, "rev-parse", "--show-toplevel") if os.path.isdir(start) else ""
    return "neither", os.path.abspath(top or start)


def _remotes(root: str) -> Dict[str, str]:
    out = {}
    for name in gitutil.git(root, "remote").split():
        out[name] = gitutil.git(root, "remote", "get-url", name)
    return out


HIDDEN_URL = "<a URL holding a credential, not shown>"


def safe_url(url: str) -> str:
    """A remote URL as it may be printed: without any user or password part (``https://user:token@host`` ->
    ``https://host``), and without its query and fragment when they hold a credential (``?access_token=...`` ->
    ``?<hidden>``); ``HIDDEN_URL`` when what is left still holds one (a token in the path)."""
    text = re.sub(r"^([a-z][a-z0-9+.-]*://)[^/@\s]*@", r"\1", str(url or ""), flags=re.I)
    if not url_credential(text):
        return text
    text = re.sub(r"[?#].*\Z", "", text, flags=re.S) + "?<hidden>"
    return HIDDEN_URL if url_credential(text) else text


def url_credential(url: Any) -> Optional[str]:
    """What credential a remote URL holds, or None: a password (``scheme://user:pass@``), a user part on an http(s)
    URL (a token given as the user name counts), or a string that looks like a token anywhere in it (the secret
    patterns, and the credential kinds the source sanitizer refuses, such as ``?token=...``). The scheme is matched
    in any case. ``git@host:x`` and ``ssh://git@host/x`` name a login, not a secret."""
    from . import secrets

    text = str(url or "").strip()
    if not text:
        return None
    if PASSWORD_RE.match(text):
        return "a password"
    if HTTP_USER_RE.match(text):
        return "a user name or token"
    if secrets.scan_str(text) or secrets.credential_kinds(text.encode("utf-8")):
        return "a token"  # the second catches what the source sanitizer refuses, such as ?token=... in the query
    return None


def strip_credentials(url: str) -> str:
    """``url`` without the credential ``url_credential`` names: the user part of an http(s) URL, the password of
    any other scheme (``ssh://git:pw@host`` -> ``ssh://git@host``)."""
    text = str(url or "").strip()
    text = re.sub(r"^(https?://)[^/@\s]*@", r"\1", text, flags=re.I)
    return re.sub(r"^([a-z][a-z0-9+.-]*://[^/@:\s]*):[^/@\s]*@", r"\1@", text, flags=re.I)


def manifest_problem(root: Optional[str]) -> Optional[str]:
    """Why ``<root>/ontology.json`` does not make a topic (not JSON, a merge conflict, failing its schema), or None
    when it is readable and valid, or absent."""
    from . import records

    if not root:
        return None
    path = os.path.join(root, "ontology.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        return "it cannot be read: %s" % (exc.strerror or exc)
    if data.startswith(b"<<<<<<<") or b"\n<<<<<<< " in data or b"\n>>>>>>> " in data:
        return "it holds merge conflict markers"
    try:
        value = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return "not valid JSON: %s" % exc
    if not isinstance(value, dict):
        return "not a JSON object"
    errors = records.check(value, "ontology_manifest")
    return "it fails its schema: %s" % errors[0] if errors else None


# the checks ----------------------------------------------------------------------------------------------------
def run_checks(start: str, env: Mapping[str, str], explicit: Optional[str] = None,
               claude: Optional[str] = None) -> Dict[str, Any]:
    from . import cmd_core

    checks: List[Dict[str, Any]] = []
    version = ".".join(str(n) for n in sys.version_info[:3])
    exe = sys.executable or "python3"
    if tuple(sys.version_info[:2]) >= MINIMUM:
        checks.append(check("python", "ok", "python %s (%s)" % (version, exe)))
    else:
        checks.append(check("python", "fail", "python %s (%s) is older than 3.9" % (version, exe),
                            "install python 3.9 or newer, or put one first on PATH"))
    git = shutil.which("git")
    git_version = ""
    if git:
        ok, out, _err = gitutil.git_ok(start if os.path.isdir(start) else os.getcwd(), "--version")
        git_version = out if ok else ""
        checks.append(check("git", "ok", git_version or "git at %s" % git))
    else:
        checks.append(check("git", "fail", "git is not on PATH", "install git (on macOS: xcode-select --install)"))
    where, root = locate(start, env, explicit)
    # an evicted ontology.json or git index (what the session-start hook checks) is found before any git command
    # reads the index: those commands would hang on it
    evicted = evicted_core(root)
    is_git = bool(git) and bool(gitutil.git(root, "rev-parse", "--show-toplevel"))
    branch = gitutil.branch(root) if is_git else None
    remotes = _remotes(root) if is_git else {}
    remote_text = ", ".join("%s %s" % (k, safe_url(v)) for k, v in sorted(remotes.items())) or "no remotes"
    if git:
        # setup's commit and every session commit need it; a new machine often has none
        ident = gitutil.identity_problem(root if is_git else os.path.abspath(os.sep))
        checks.append(check("git_identity", "warn", "git cannot name the author of a commit: %s" % ident,
                            gitutil.IDENTITY_FIX) if ident
                      else check("git_identity", "ok", "git has a user name and email"))
    broken = manifest_problem(root) if where == "topic" else None
    if broken:
        checks.append(check("where", "fail", "topic at %s, but ontology.json %s" % (root, broken),
                            "resolve the merge conflict or the edit in ontology.json (git diff ontology.json shows "
                            "it), then run onto validate"))
    elif where == "topic":
        checks.append(check("where", "ok", "topic at %s (branch %s; %s)" % (root, branch or "none", remote_text)))
    elif where == "template" and not is_git and git:
        checks.append(check("where", "warn", "template files at %s, but not a git repo (a downloaded ZIP?): onto "
                                             "setup clones new topics from a git checkout" % root,
                            "clone the template instead: git clone -b %s --single-branch <repo-url> "
                            "~/general-ontology-kit, then run ./new-topic there" % TEMPLATE_BRANCH))
    elif where == "template":
        problem = cmd_core.template_clone_problem(root) if is_git else None
        detail = "template checkout at %s (branch %s; %s)" % (root, branch or "none", remote_text)
        make = missing_branch_fix(root) if is_git else None
        if make:
            checks.append(check("where", "warn", "%s%s: no local %s branch, which onto setup clones new topics from"
                                % (detail, ": step 1 not done" if problem else "", TEMPLATE_BRANCH),
                                "create it with %s, then run onto setup" % make))
        else:
            checks.append(check("where", "warn" if problem else "ok",
                                detail + (": step 1 not done" if problem else ""),
                                "run onto setup (or ./new-topic) to make a topic" if problem else
                                "run onto setup to make the topic"))
    else:
        checks.append(check("where", "warn", "neither a topic nor a template checkout: %s" % root,
                            "run onto setup --new DIR from a template checkout"))
    vendored = kit_version(root)
    if where in ("topic", "template"):
        if vendored is None:
            checks.append(check("kit", "warn", "no kit vendored in this folder (plugins/general-ontology)",
                                "start topics from the template (onto setup), so the topic carries its kit"))
        else:
            skew = []
            if vendored != __version__:
                skew.append("running kit %s, vendored kit %s" % (__version__, vendored))
            if where == "topic":
                manifest = _read_json(os.path.join(root, "ontology.json")) or {}
                if isinstance(manifest, dict) and manifest.get("kit") and manifest.get("kit") != vendored:
                    skew.append("ontology.json written by kit %s" % manifest.get("kit"))
            if skew:
                checks.append(check("kit", "warn", "kit %s vendored; %s" % (vendored, "; ".join(skew)),
                                    "run onto migrate --check in the topic; refresh the plugin with "
                                    "/plugin marketplace update general-ontology"))
            else:
                checks.append(check("kit", "ok", "kit %s vendored" % vendored))
    if is_git and where in ("topic", "template") and evicted:
        checks.append(check("git_rules", "warn", "not checked: %s is evicted, and git would hang reading it"
                            % evicted[0], MOVE_FIX))
    elif is_git and where in ("topic", "template"):
        unignored = [rel for rel in ("inbox/", ".onto/") if not gitutil.git_ok(root, "check-ignore", "-q", rel)[0]]
        have = cmd_core._rule_lines(cmd_core._up_to_git_root(root), ".gitattributes")
        missing = [r for r in cmd_core.GITATTRIBUTES_RULES if r not in have]
        if unignored:
            checks.append(check("git_rules", "fail", "not ignored: %s" % ", ".join(unignored),
                                "add %s to .gitignore (the template's .gitignore has them)" % ", ".join(unignored)))
        elif missing:
            checks.append(check("git_rules", "warn", "%d .gitattributes merge rule%s missing" % (
                len(missing), "" if len(missing) == 1 else "s"), "copy the template's .gitattributes"))
        else:
            checks.append(check("git_rules", "ok", "inbox/ and .onto/ ignored; merge rules present"))
    elif where in ("topic", "template"):
        checks.append(check("git_rules", "warn", "not a git repo", "git init, or start from the template"))
    reason = cloud_synced(root, env)
    checks.append(check("cloud_sync", "warn", "%s: %s" % (root, reason), MOVE_FIX) if reason
                  else check("cloud_sync", "ok", "not in a cloud-synced folder"))
    # outside a topic or a template checkout the folder can be HOME itself: no walk there, since it would reach
    # ~/Desktop, ~/Documents and ~/Library (privacy prompts, evicted folders) for a folder that is not a topic
    ours = where in ("topic", "template")
    hit = evicted[0] if evicted else (dataless_hit(root) if ours else None)  # it lists files through the index
    checks.append(check("dataless", "fail", "%s is evicted (dataless)" % hit,
                        "files are evicted; git and imports will hang. " + MOVE_FIX) if hit
                  else check("dataless", "ok", "no evicted files" if ours else
                             "not checked: neither a topic nor a template checkout"))
    refs = refs_conflicts(root) if is_git else []
    tree = tree_conflicts(root) if ours else []
    if refs:
        checks.append(check("conflict_copies", "fail", "in .git: %s%s" % (
            ", ".join(refs), "; in the work tree: %s" % ", ".join(tree) if tree else ""),
            "they break fetch; move them out of .git (do not delete them)%s" % (
                "; compare each work-tree copy with its original, keep one, move the copy out" if tree else "")))
    elif tree:
        checks.append(check("conflict_copies", "warn", "in the work tree: %s" % ", ".join(tree),
                            "compare each with its original, keep one, move the copy out"))
    else:
        checks.append(check("conflict_copies", "ok", "no conflict copies"))
    locks = stale_locks(root) if is_git else []
    checks.append(check("stale_locks", "warn", "stale: %s" % ", ".join(locks),
                        "make sure no git command runs, then move the lock files out of .git") if locks
                  else check("stale_locks", "ok", "no stale locks"))
    if where in ("topic", "template") and not evicted and not hit:
        from . import agents

        checks.append(agents.skills_check(root))
    if where == "topic":
        checks.append(_plugin_check(root, claude))
        if not broken and not evicted and not hit:  # the topic checks read every graph and ledger file
            checks.extend(_topic_checks(root))
    elif where == "template" and claude:
        checks.append(_machine_plugin_check(claude))
    fails = sum(1 for c in checks if c["status"] == "fail")
    warns = sum(1 for c in checks if c["status"] == "warn")
    return {"where": where, "root": root, "checks": checks, "fails": fails, "warns": warns,
            "exit_code": 1 if fails else 0}


def source_text(source: Any) -> str:
    """A short, credential-free description of a marketplace source (``github owner/repo``, ``git URL``, ...)."""
    if isinstance(source, dict):
        kind = str(source.get("source") or source.get("type") or "source")
        inner = source.get("repo") or source.get("url") or source.get("path") or ""
        text = ("%s %s" % (kind, safe_url(str(inner)))).strip()
        return text + (" (ref %s)" % source["ref"] if source.get("ref") else "")
    if isinstance(source, str) and source:
        return safe_url(source)
    return "an unknown source"


def _machine_plugin_check(claude: str) -> Dict[str, Any]:
    """In a template checkout: what ``claude plugin marketplace list`` says about the machine's marketplace."""
    listed = claude_marketplace(claude)
    if listed is None:
        return check("plugin", "ok", "claude plugin marketplace list did not answer; nothing to check")
    if listed.get("folder"):
        return check("plugin", "warn", "claude lists general-ontology from a local folder (%s)" % source_text(
            listed.get("source")), REPAIR)
    if listed.get("found"):
        return check("plugin", "ok", "claude lists general-ontology from %s" % source_text(listed.get("source")))
    return check("plugin", "ok", "no general-ontology marketplace on this machine yet; onto setup wires it per topic")


def recorded_plugin(root: str) -> Optional[str]:
    """The plugin mode the topic's active setup decision names (project, local, plugin-dir or skip), or None."""
    chosen = _active_choice(root, Q_PLUGIN)
    return chosen if chosen in ("project", "local", "plugin-dir", "skip") else None


def _active_choice(root: str, question: str) -> Optional[str]:
    """The ``chosen`` text of the topic's active decision on ``question``, or None."""
    folder = os.path.join(root, "ledger", "decisions")
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return None
    want = " ".join(question.split())
    for name in names:
        rec = _read_json(os.path.join(folder, name)) if name.endswith(".json") else None
        if isinstance(rec, dict) and rec.get("status") == "active" and " ".join(
                str(rec.get("question") or "").split()) == want:
            chosen = rec.get("chosen")
            return chosen if isinstance(chosen, str) else None
    return None


def topic_agents(root: str) -> Tuple[List[str], str]:
    """The agents this topic is for, and where that came from: the active setup decision on question 7
    (``decision``), else the harnesses whose files are in the repo (``wired``), else nothing (``none``)."""
    from . import agents

    chosen = _active_choice(root, Q_AGENTS)
    if chosen:
        try:
            names = agents.parse_agents(chosen)
        except Exception:
            names = []
        if names:
            return names, "decision"
    names = [n for n in agents.NAMES if n not in ("claude", "generic")
             and agents.wired(root, n) not in ("not wired", "no files")]
    return (names, "wired") if names else ([], "none")


def _source_credential(source: Any) -> Optional[str]:
    if isinstance(source, dict):
        return url_credential(source.get("url") or source.get("repo"))
    return url_credential(source) if isinstance(source, str) else None


def _plugin_check(root: str, claude: Optional[str]) -> Dict[str, Any]:
    wiring = settings_wiring(root)
    leaked = _source_credential(wiring["source"])
    if leaked:
        return check("plugin", "fail", "the marketplace URL in .claude/settings.json holds %s, and the file is "
                                       "shared with everyone who clones the repo" % leaked,
                     "revoke that credential, take it out of .claude/settings.json (use a git credential helper), "
                     "and commit the file again")
    if wiring["folder_source"]:
        return check("plugin", "warn", "the marketplace source in .claude/settings is a local folder", REPAIR)
    listed = claude_marketplace(claude) if claude else None
    if listed is not None and listed.get("folder"):
        return check("plugin", "warn", "claude lists general-ontology from a local folder (%s)" % source_text(
            listed.get("source")), REPAIR)
    if not (wiring["project"] or wiring["local"]):
        chosen = recorded_plugin(root)
        if chosen == "skip":
            return check("plugin", "ok", "not wired, as the recorded setup decision says (do not wire it)")
        if chosen == "plugin-dir":
            return check("plugin", "ok", "not installed, as the recorded setup decision says (run it without "
                                         "installing)", "start claude --plugin-dir ./plugins/general-ontology")
        names, came_from = topic_agents(root)
        if names and "claude" not in names:
            # the plugin is for Claude Code only: a topic for other agents never needs it wired
            return check("plugin", "ok", "not wired: Claude Code is not one of the agents (%s: %s)" % (
                "the recorded setup decision" if came_from == "decision" else "wired", ", ".join(names)))
        kit = _remotes(root).get("kit") if gitutil.git(root, "rev-parse", "--show-toplevel") else None
        if kit and local_source(kit):
            # setup cannot install from a folder (it would take over the machine's one marketplace), so running
            # setup --plugin project again changes nothing: plugin-dir is how this topic runs
            folder = re.split(r"[/\\]", kit.strip().rstrip("/\\"))[-1] or "a folder"
            return check("plugin", "ok", "not installed: the topic's kit remote is a local folder (%s), so the plugin "
                                         "runs without installing" % folder,
                         "start claude --plugin-dir ./plugins/general-ontology; to install it later, point kit at a "
                         "git URL (git remote set-url kit <repo-url>), then run onto setup --plugin project")
        return check("plugin", "warn", "the plugin is not wired in .claude/settings.json or settings.local.json",
                     "ask the user, then run onto setup --plugin project (or start claude --plugin-dir "
                     "./plugins/general-ontology)")
    where = "settings.json (project)" if wiring["project"] else "settings.local.json (local)"
    if listed is not None and not listed.get("found"):
        return check("plugin", "warn", "wired in %s; claude does not list the marketplace yet" % where,
                     "trust the folder when Claude Code asks, or run onto setup again")
    if listed is not None:
        return check("plugin", "ok", "wired in %s; claude lists it from %s" % (where, source_text(
            listed.get("source"))))
    return check("plugin", "ok", "wired in %s (%s)" % (where, source_text(wiring["source"])) if wiring["source"]
                 else "wired in %s" % where)


def _topic_checks(root: str) -> List[Dict[str, Any]]:
    from . import ledger, pipeline, store, util, validate

    out: List[Dict[str, Any]] = []
    try:
        repo = store.Repo.open(root)
    except Exception as exc:
        return [check("topic", "fail", "ontology.json cannot be read: %s" % exc, "run onto validate")]
    try:
        report = validate.validate(repo)
        count = len(report.problems)
        out.append(check("validate", "fail" if count else "ok", "%d validate problem%s" % (
            count, "" if count == 1 else "s"), "run onto validate" if count else ""))
    except Exception as exc:
        out.append(check("validate", "fail", "validate failed: %s" % exc, "run onto validate"))
    try:
        pending = len(pipeline.pending(repo))
    except Exception:
        pending = 0
    limit = repo.policy.get("max_pending")
    over = isinstance(limit, int) and pending > limit
    out.append(check("pending", "warn" if over else "ok", "%d pending proposal%s%s" % (
        pending, "" if pending == 1 else "s", " (over max_pending)" if over else ""),
        "review them (onto-review skill)" if over else ""))
    every = gitutil.dirty_paths(root)
    dirty = [p for p in every if not p.startswith(KIT_OWNED)]
    out.append(check("uncommitted", "warn" if dirty else "ok", "%d uncommitted topic file%s" % (
        len(dirty), "" if len(dirty) == 1 else "s"), "commit at the end of the session" if dirty else ""))
    if SETTINGS_REL in every:
        # the project's plugin wiring: uncommitted, it works on this machine only, and a clone never gets it
        out.append(check("wiring", "warn", "%s is uncommitted: the plugin is wired on this machine only" %
                         SETTINGS_REL, "run onto setup again (it commits the file when it holds only setup's "
                         "entries), or commit it with the user's yes: git add -- %s && git commit -m \"Wire the "
                         "general-ontology plugin\"" % SETTINGS_REL))
    try:
        last = ledger.last_checkpoint(repo)
    except Exception:
        last = None
    if last and last.get("at"):
        try:
            days = max(0, (util.now() - util.parse_ts(str(last["at"]))).days)
        except Exception:
            days = None
        out.append(check("checkpoint", "ok", "last checkpoint %s%s" % (
            str(last["at"])[:10], "" if days is None else " (%d day%s ago)" % (days, "" if days == 1 else "s"))))
    else:
        out.append(check("checkpoint", "ok", "no checkpoint yet", "onto log --checkpoint at the end of a session"))
    return out


def missing_branch_fix(root: str) -> Optional[str]:
    """The command that creates the local general-ontology branch ``onto setup --new`` clones from, or None when it
    exists: from a remote-tracking copy (a plain ``git clone`` checked out on another branch has only that), else
    from HEAD."""
    if gitutil.git(root, "rev-parse", "--verify", "--quiet", "refs/heads/%s" % TEMPLATE_BRANCH):
        return None
    tracking = [ref for ref in gitutil.git(root, "for-each-ref", "--format=%(refname:short)",
                                           "refs/remotes/*/%s" % TEMPLATE_BRANCH).split() if ref]
    if tracking:
        ref = sorted(tracking)[0]
        have, head = _kit_at(root, ref), _kit_at(root, "HEAD")
        if not (have and head and _version_key(have) < _version_key(head)):
            return "git -C %s branch %s %s" % (shell_quote(root), TEMPLATE_BRANCH, ref)
        # the remote copy holds an older kit than this checkout: setup would refuse a branch made from it
        return "git -C %s branch %s HEAD (%s holds kit %s, older than this checkout's kit %s)" % (
            shell_quote(root), TEMPLATE_BRANCH, ref, have, head)
    return "git -C %s branch %s HEAD (when HEAD holds the kit to start topics from)" % (shell_quote(root),
                                                                                        TEMPLATE_BRANCH)


def _version_key(version: str) -> Tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"[0-9]+", str(version or ""))[:3])


def _kit_at(root: str, ref: str) -> Optional[str]:
    """The kit version a commit (``ref``) of the checkout at ``root`` holds, or None."""
    found = VERSION_RE.search(gitutil.git(root, "show", "%s:%s" % (ref, "/".join(
        KIT_REL + ("ontokit", "__init__.py")))) or "")
    return found.group(1) if found else None


def evicted_core(root: Optional[str]) -> List[str]:
    """``ontology.json`` and the git index of ``root`` when they are evicted (dataless), read from the file system
    only: the files the session-start hook checks, and every git command reads the index."""
    if not root:
        return []
    gitdir, _common = _git_dirs(root)
    paths = [os.path.join(root, "ontology.json")] + ([os.path.join(gitdir, "index")] if gitdir else [])
    return [p for p in paths if _dataless(p)]


def cheap_failures(root: Optional[str]) -> List[str]:
    """The ids of failing checks that need no tree walk and no subprocess, for the session-start hook: git missing,
    conflict copies in ``.git/refs`` (a small folder), and an evicted ``ontology.json`` or git index."""
    out: List[str] = []
    if shutil.which("git") is None:
        out.append("git")
    if not root:
        return out
    try:
        if refs_conflicts(root, limit=2000):
            out.append("conflict_copies")
    except Exception:
        pass
    if evicted_core(root):
        out.append("dataless")
    return out


# the command ---------------------------------------------------------------------------------------------------
def cmd_doctor(ctx: Context, args: Dict[str, Any]) -> Dict[str, Any]:
    start = os.path.abspath(os.path.expanduser(ctx.explicit or ctx.cwd or os.getcwd()))
    claude = shutil.which("claude", path=ctx.env.get("PATH"))
    result = run_checks(start, ctx.env, ctx.explicit, claude)
    from . import store
    from .errors import OntoError

    # the version line names the folder these checks are about, never the topic $ONTO_REPO points at
    ctx.env.pop("ONTO_REPO", None)
    found = None
    if result.get("where") == "topic" and result.get("root"):
        try:
            found = store.Repo.open(result["root"])
        except OntoError:
            found = None
    ctx.repo = found
    if found is None:
        ctx.explicit, ctx.cwd = None, os.path.abspath(os.sep)  # no topic here: no version line from elsewhere
    return result


def render_doctor(result: Dict[str, Any], mode: str, ctx: Context) -> List[str]:
    lines = ["doctor: %s at %s: %d fail, %d warn" % (result.get("where"), result.get("root"),
                                                       result.get("fails") or 0, result.get("warns") or 0)]
    from .onboard import one_line

    for c in result.get("checks") or []:
        lines.append("%-4s %-15s %s" % (c["status"], c["id"], one_line(c["detail"])))
        if c.get("fix") and (c["status"] != "ok" or mode == "text"):
            lines.append("     %-15s fix: %s" % ("", one_line(c["fix"])))
    return lines
