"""The topic repo on disk: discovery, paths, atomic IO, the write lock, the input path guard, the data hash and
the version stamp.

Discovery order: an explicit path (``--repo``), then ``$ONTO_REPO``, then a walk up from the working directory,
then a walk up from ``$CLAUDE_PROJECT_DIR``. A topic repo is a folder holding ``ontology.json``. An unexpanded
``$ONTO_REPO`` or ``${ONTO_REPO}`` placeholder (from an MCP or settings file) counts as unset, and a path that
is given but is not a topic repo is an error, never a fall-through. A relative ``--repo`` or ``$ONTO_REPO``
resolves against the working directory (``resolve``: the ``cwd`` a caller passes, else the process's own).

Writes are atomic (temp file in the same folder, fsync, ``os.replace``) and every write command holds
``.onto/lock`` (``write_lock``). File hashes are cached per real path, mtime and size.

A write of several files records an intent first (``begin_write``: ``.onto/intent.json`` plus a backup of every file
it rewrites under ``.onto/backup/``) and clears it when done (``end_write``). Before each line it appends to a log, the
intent records that line's bytes (``append_jsonl``). A process killed in between leaves the intent behind, and the
next ``write_lock`` holder rolls the interrupted write back (``recover``) before it does anything else, so a hard kill
never leaves half a change on disk. Recovery takes back only bytes the interrupted write itself put there: lines a
merge or a pull added since are kept, and a log whose tail mixes the write's own line with other lines is left as it
is and reported in ``.onto/recovery.json`` (``recovery_note``), which ``onto status`` and ``onto validate`` show.
"""

from __future__ import annotations

import contextlib
import copy
import errno
import glob
import hashlib
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

from . import __version__, ids, util
from .errors import DataError, LockBusy, Problem, Refused, UsageError

try:  # pragma: no cover - platform dependent
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore

MANIFEST = "ontology.json"
ENV_VAR = "ONTO_REPO"
PROJECT_ENV = "CLAUDE_PROJECT_DIR"
NEEDS = "needs ontology.json; run onto init"

TOPIC_DIRS = ("packs", "graph", "sources", "proposals", "interview", "ledger", "metrics", "imports", "build")
ROOT_DATA_FILES = ("ontology.json", "VERSIONS.md", "MANIFEST.json")
# Files hashed into data_hash (C.17), plus every ledger/decisions/*.json.
DATA_FILES = (
    "ontology.json",
    "packs/local.pack.json",
    "packs/local.questions.jsonl",
    "graph/nodes.jsonl",
    "graph/edges.jsonl",
    "sources/index.jsonl",
    "imports/lock.json",
)
DECISIONS_GLOB = "ledger/decisions/*.json"
EXPORTS_GLOB = "imports/*/export.json"

COPY_RE = re.compile(r"[^/] \d+(\.[^/]+)?$")  # "nodes 2.jsonl", "main 2": a sync or Finder copy
JUNK_RE = re.compile(r"(^|/)(__pycache__|\.pytest_cache|\.ruff_cache)/|\.py[co]$|(^|/)\.DS_Store$|[^/] \d+(\.[^/]+)?$")
_JUNK_ONLY_RE = re.compile(r"(^|/)(__pycache__|\.pytest_cache|\.ruff_cache)/|\.py[co]$|(^|/)\.DS_Store$")
_COPY_PARTS_RE = re.compile(r"^(.*[^/]) \d+((?:\.[^/]+)?)$")


def conflict_original(rel: str) -> Optional[str]:
    """The name a sync or Finder copy was made from (``graph/nodes 2.jsonl`` -> ``graph/nodes.jsonl``,
    ``main 2`` -> ``main``), or None when ``rel`` does not look like one."""
    found = _COPY_PARTS_RE.match(rel.replace(os.sep, "/"))
    return found.group(1) + found.group(2) if found else None


def is_conflict_copy(root: str, rel: str) -> bool:
    """True when ``rel`` (relative to ``root``) looks like a conflict copy and its original sits next to it. A name
    such as ``Chapter 1.md`` or ``Budget 2025.xlsx`` with no ``Chapter.md`` beside it is the user's own file."""
    original = conflict_original(rel)
    return bool(original) and os.path.lexists(os.path.join(root, *original.split("/")))


def is_junk(root: str, rel: str) -> bool:
    """A junk path: ``__pycache__`` and the like, ``.DS_Store``, or a conflict copy whose original is there."""
    return bool(_JUNK_ONLY_RE.search(rel)) or is_conflict_copy(root, rel)
ALLOWED = tuple(
    re.compile(p)
    for p in (
        r"^ontology\.json$",
        r"^VERSIONS\.md$",
        r"^MANIFEST\.json$",
        r"^packs/local\.(pack\.json|questions\.jsonl)$",
        r"^graph/(nodes|edges)\.jsonl$",
        r"^sources/index\.jsonl$",
        r"^sources/src-[0-9a-f]{12}(\.txt|\.orig\.[a-z0-9]{1,8})$",
        r"^proposals/(pending|done)/prop-[0-9]{8}-[0-9a-f]{6,10}\.json$",
        r"^interview/log\.jsonl$",
        r"^ledger/decisions/dec-[0-9]{8}-[a-z0-9-]{1,40}-[0-9a-f]{4,8}\.json$",
        r"^ledger/changes\.jsonl$",
        r"^metrics/history\.jsonl$",
        r"^imports/lock\.json$",
        r"^imports/[a-z][a-z0-9-]{0,31}/export\.json$",
        r"^build/(export|cards)\.json$",
    )
)
# Built and gitignored outputs that may sit on disk without being part of the topic data.
IGNORED_OUTPUTS = ("build/index.html",)

DEFAULT_POLICY: Dict[str, Any] = {
    "personal": {"email": "redact", "phone": "redact", "name": "keep", "address": "redact", "payment_card": "redact",
                 "government_id": "redact"},
    "max_pending": 20,
    "pending_stale_days": 30,
    "stage_done_at": 0.6,
    "weights": {"coverage": 30, "completeness": 20, "connectivity": 15, "evidence": 20, "confirmation": 15},
    "keep_original_max_bytes": 5000000,
}

CREDENTIAL_FILES = re.compile(
    r"^(?:_?netrc|_?pgpass|npmrc|pypirc|credentials.*|client_secret.*\.json"
    r"|id_(?:rsa|dsa|ecdsa|ed25519)[^/]*|.*\.(?:pem|key|p12|pfx|asc|gpg)|\.?env(?:\..*)?)$",
    re.I,
)
CREDENTIAL_DIRS = (".ssh", ".aws", ".docker", ".gnupg", ".kube")

LOCK_REL = ".onto/lock"
INTENT_REL = ".onto/intent.json"
BACKUP_REL = ".onto/backup"
RECOVERY_REL = ".onto/recovery.json"
LOCK_STALE_SECONDS = 600
LOCK_POLL_SECONDS = 0.05
_USE_FLOCK = fcntl is not None  # tests switch this off to exercise the O_EXCL fallback
_HELD: Dict[str, int] = {}  # lock path -> nesting depth held by this process
_ACTIVE: Dict[str, Dict[str, Any]] = {}  # real root -> the intent of the write this process has under way

_HASH_CACHE: Dict[str, Tuple[int, int, str]] = {}  # real path -> (mtime_ns, size, sha256)
_DATA_HASH_CACHE: Dict[str, Tuple[Tuple[Any, ...], str]] = {}  # real root -> (stamp, hash)
_MISSING = object()


# repo ----------------------------------------------------------------------------------------------------------
@dataclass
class Repo:
    """A topic repo: its absolute ``root`` and the parsed ``ontology.json``."""

    root: str
    manifest: Dict[str, Any] = field(default_factory=dict)

    def path(self, rel: str = "") -> str:
        """Absolute path of a repo-relative, ``/``-separated path."""
        if not rel:
            return self.root
        return os.path.join(self.root, *[p for p in rel.split("/") if p])

    @property
    def ns(self) -> str:
        return str(self.manifest.get("ns") or "")

    @property
    def name(self) -> str:
        return str(self.manifest.get("name") or "")

    @property
    def policy(self) -> Dict[str, Any]:
        policy = dict(DEFAULT_POLICY)
        policy.update(self.manifest.get("policy") or {})
        return policy

    @classmethod
    def open(cls, root: str) -> "Repo":
        """The repo at ``root``; raises ``DataError`` when ``ontology.json`` is missing or not a JSON object."""
        root = os.path.abspath(os.path.expanduser(root))
        path = os.path.join(root, MANIFEST)
        if not os.path.isfile(path):
            raise DataError("%s is not a topic repo (%s)" % (root, NEEDS), needs=MANIFEST)
        manifest = read_json(path)
        if not isinstance(manifest, dict):
            raise DataError("%s: not a JSON object" % path)
        return cls(root, manifest)

    def reload(self) -> "Repo":
        self.manifest = read_json(self.path(MANIFEST))
        return self


def _root(repo: Union[Repo, str]) -> str:
    return repo.root if isinstance(repo, Repo) else os.path.abspath(repo)


def is_topic_repo(path: Optional[str]) -> bool:
    return bool(path) and os.path.isfile(os.path.join(str(path), MANIFEST))


def is_placeholder(v: Optional[str]) -> bool:
    """True for an unset-looking value: None, empty, or an unexpanded ``$VAR`` / ``${VAR}`` (no real path starts
    with ``$``)."""
    return v is None or not str(v).strip() or str(v).strip().startswith("$")


def resolve(path: str, cwd: Optional[str] = None) -> str:
    """``path`` (``~`` expanded) as an absolute path; a relative one is taken from ``cwd``, else the process's
    working directory."""
    expanded = os.path.expanduser(str(path))
    return os.path.abspath(os.path.join(cwd or os.getcwd(), expanded))


def _walk_up(start: str) -> Optional[str]:
    current = os.path.abspath(start)
    while True:
        if is_topic_repo(current):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def find_root(explicit: Optional[str] = None, cwd: Optional[str] = None,
              env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The discovered topic root, or None when nothing is found. A given path (explicit or ``$ONTO_REPO``) that
    is not a topic repo raises ``DataError``."""
    env = os.environ if env is None else env
    if explicit is not None and not is_placeholder(explicit):
        path = resolve(explicit, cwd)
        if is_topic_repo(path):
            return path
        raise DataError("--repo %s is not a topic repo (%s)" % (path, NEEDS), needs=MANIFEST)
    from_env = env.get(ENV_VAR)
    if not is_placeholder(from_env):
        path = resolve(str(from_env), cwd)
        if is_topic_repo(path):
            return path
        raise DataError("$%s=%s is not a topic repo (%s)" % (ENV_VAR, path, NEEDS), needs=MANIFEST)
    found = _walk_up(cwd or os.getcwd())
    if found:
        return found
    project = env.get(PROJECT_ENV)
    if not is_placeholder(project):
        found = _walk_up(os.path.expanduser(str(project)))
        if found:
            return found
    return None


def discover(explicit: Optional[str] = None, cwd: Optional[str] = None,
             env: Optional[Mapping[str, str]] = None) -> Repo:
    """The topic repo, or ``DataError`` naming what is needed."""
    root = find_root(explicit, cwd, env)
    if root is None:
        where = os.path.abspath(cwd or os.getcwd())
        template = _template_up(where)
        if template:
            # onto init refuses in a template clone until README step 1 is done; onto setup does that step
            raise DataError(
                "no ontology.json in %s or its parents: %s is a template checkout; run onto setup to make a topic "
                "(onto doctor says more), or pass --repo PATH or set $%s" % (where, template, ENV_VAR),
                needs=MANIFEST,
            )
        raise DataError(
            "no ontology.json in %s or its parents; run onto init, or pass --repo PATH or set $%s"
            % (where, ENV_VAR),
            needs=MANIFEST,
        )
    return Repo.open(root)


def _template_up(start: str) -> Optional[str]:
    """The nearest folder at or above ``start`` that holds the kit (``plugins/general-ontology/ontokit``), else
    None: a checkout of the template, where a topic is made with ``onto setup``."""
    current = start
    while True:
        if os.path.isdir(os.path.join(current, "plugins", "general-ontology", "ontokit")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def personal_policy(personal: Optional[str] = None) -> Dict[str, Any]:
    """The default policy, with ``personal`` (keep, redact or refuse) set for every personal kind when given."""
    policy = copy.deepcopy(DEFAULT_POLICY)
    if personal is not None:
        policy["personal"] = {kind: personal for kind in policy["personal"]}
    return policy


def new_manifest(name: str, ns: str, title: str, created: Optional[str] = None,
                 personal: Optional[str] = None) -> Dict[str, Any]:
    """A fresh ``ontology.json`` object (C.3) written by ``onto init``; ``personal`` as in ``personal_policy``."""
    from . import FORMAT

    return {
        "format": FORMAT,
        "kit": __version__,
        "name": name,
        "ns": ns,
        "title": title,
        "created": created or util.today(),
        "packs": ["core", "discovery", "local"],
        "policy": personal_policy(personal),
    }


# reading -------------------------------------------------------------------------------------------------------
def read_bytes(path: str) -> bytes:
    """A file's bytes, hashing them on the way so a later ``file_sha256`` needs no second read."""
    with open(path, "rb") as fh:
        data = fh.read()
    file_sha256(path, data)
    return data


def read_json(path: str, default: Any = _MISSING) -> Any:
    """Parsed JSON. A missing file returns ``default`` when one is given (else ``FileNotFoundError``); invalid JSON
    raises ``DataError``, and so do a key repeated in one object and a lone surrogate (``util.loads_record``)."""
    try:
        data = read_bytes(path)
    except FileNotFoundError:
        if default is _MISSING:
            raise
        return default
    try:
        return util.loads_record(data.decode("utf-8"))
    except ValueError as exc:  # NaN and Infinity are not JSON either
        raise DataError("%s: not valid JSON: %s" % (path, exc), file=path)
    except RecursionError:
        raise DataError("%s: not valid JSON: nested too deeply" % path, file=path)


def read_jsonl_lines(path: str) -> Tuple[List[Tuple[int, Dict[str, Any]]], List[Tuple[int, str]]]:
    """(``[(line, object)]``, ``[(line, message)]``) for a JSONL file. Blank lines are skipped; a line that is not
    valid UTF-8, not JSON (a key repeated in one object and a lone surrogate count: see ``util.loads_record``), or
    not an object is a problem. A missing file is empty. Never raises on bad lines."""
    rows: List[Tuple[int, Dict[str, Any]]] = []
    problems: List[Tuple[int, str]] = []
    try:
        data = read_bytes(path)
    except FileNotFoundError:
        return rows, problems
    for number, raw in enumerate(data.split(b"\n"), start=1):
        if not raw.strip():
            continue
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            problems.append((number, "not UTF-8: %s" % exc))
            continue
        try:
            value = util.loads_record(line)
        except ValueError as exc:  # NaN and Infinity are not JSON either
            problems.append((number, "not valid JSON: %s" % exc))
            continue
        except RecursionError:
            problems.append((number, "not valid JSON: nested too deeply"))
            continue
        if not isinstance(value, dict):
            problems.append((number, "not a JSON object"))
            continue
        rows.append((number, value))
    return rows, problems


def read_jsonl(path: str) -> Tuple[List[Dict[str, Any]], List[Tuple[int, str]]]:
    """(rows, problems) for a JSONL file; problems are ``(line, message)``. Never raises on bad lines."""
    numbered, problems = read_jsonl_lines(path)
    return [row for _n, row in numbered], problems


# writing -------------------------------------------------------------------------------------------------------
def _umask() -> int:
    current = os.umask(0o022)
    os.umask(current)
    return current


_DEFAULT_MODE = 0o666 & ~_umask()


def file_mode(path: str) -> int:
    """The permission bits a rewrite of ``path`` keeps: the file's own, or ``0o666`` less the umask for a new
    file (a temp file from ``mkstemp`` is ``0o600``, which a plain write would not give)."""
    try:
        return os.stat(path).st_mode & 0o777
    except OSError:
        return _DEFAULT_MODE


def write_bytes(path: str, data: bytes) -> None:
    """Atomic write: a temp file in the same folder, fsync, then ``os.replace``. The file keeps its permission
    bits (a new file gets the umask default)."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    mode = file_mode(path)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix="." + os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            if hasattr(os, "fchmod"):  # not on Windows, where mkstemp's mode is not an issue
                os.fchmod(fh.fileno(), mode)
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_text(path: str, text: str) -> None:
    write_bytes(path, text.encode("utf-8"))


def write_json(path: str, obj: Any) -> None:
    """Canonical bytes (C.1), atomically."""
    write_bytes(path, util.canonical_bytes(obj))


def _sort_key(key: Any):
    if key is None:
        return None
    if callable(key):
        return key
    if isinstance(key, (tuple, list)):
        names = tuple(key)
        return lambda row: tuple(str(row.get(n) or "") for n in names)
    return lambda row: str(row.get(key) or "")


def jsonl_bytes(rows: Sequence[Dict[str, Any]], key: Any = "id") -> bytes:
    """The canonical bytes of a JSONL file: rows sorted by ``key`` (a field name, a tuple of names, a callable, or
    None to keep the order), one canonical line each."""
    sorter = _sort_key(key)
    ordered = sorted(rows, key=sorter) if sorter else list(rows)
    return "".join(util.canonical_line(row) + "\n" for row in ordered).encode("utf-8")


def write_jsonl(path: str, rows: Sequence[Dict[str, Any]], key: Any = "id") -> None:
    """Sorted, canonical, atomic."""
    write_bytes(path, jsonl_bytes(rows, key))


def append_jsonl(path: str, row: Dict[str, Any]) -> None:
    """Append one canonical line, first repairing a missing trailing newline. Inside a write that recorded an intent
    for this file (``begin_write``), the bytes are added to the intent before they land, so ``recover`` takes back
    exactly them and nothing a merge put after them."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    prefix = b""
    if os.path.isfile(path) and os.path.getsize(path):
        with open(path, "rb") as fh:
            fh.seek(-1, os.SEEK_END)
            prefix = b"" if fh.read(1) == b"\n" else b"\n"
    data = prefix + (util.canonical_line(row) + "\n").encode("utf-8")
    _plan_append(path, data)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


# hashing -------------------------------------------------------------------------------------------------------
def file_sha256(path: str, data: Optional[bytes] = None) -> str:
    """The sha256 of a file, cached per real path, ``st_mtime_ns`` and ``st_size``. ``data`` is the content when
    the caller already read it."""
    st = os.stat(path)
    key = os.path.realpath(path)
    hit = _HASH_CACHE.get(key)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size and data is None:
        return hit[2]
    if data is None:
        with open(path, "rb") as fh:
            data = fh.read()
    digest = util.sha256_hex(data)
    _HASH_CACHE[key] = (st.st_mtime_ns, st.st_size, digest)
    return digest


def clear_cache() -> None:
    _HASH_CACHE.clear()
    _DATA_HASH_CACHE.clear()


def data_files(repo: Union[Repo, str]) -> List[str]:
    """The repo-relative paths hashed into ``data_hash``: ``DATA_FILES`` plus the decision files, sorted."""
    root = _root(repo)
    decisions = sorted(
        os.path.relpath(p, root).replace(os.sep, "/") for p in glob.glob(os.path.join(root, *DECISIONS_GLOB.split("/")))
    )
    return sorted(set(DATA_FILES) | set(decisions))


def vendored_exports(repo: Union[Repo, str]) -> List[str]:
    root = _root(repo)
    return sorted(
        os.path.relpath(p, root).replace(os.sep, "/") for p in glob.glob(os.path.join(root, *EXPORTS_GLOB.split("/")))
    )


def data_stamp(repo: Union[Repo, str]) -> Tuple[Tuple[Any, ...], ...]:
    """The cache key of the loaded data: ``(path, mtime_ns, size, inode)`` for ``data_files`` and the vendored
    exports; a missing file reads ``(path, None, None, None)``. The inode catches an atomic rewrite that keeps the
    size within the filesystem's timestamp granularity."""
    root = _root(repo)
    out: List[Tuple[Any, ...]] = []
    for rel in data_files(root) + vendored_exports(root):
        try:
            st = os.stat(os.path.join(root, *rel.split("/")))
        except OSError:
            out.append((rel, None, None, None))
            continue
        out.append((rel, st.st_mtime_ns, st.st_size, st.st_ino))
    return tuple(out)


def data_hash(repo: Union[Repo, str]) -> str:
    """sha256 over ``path + "\\0" + bytes + "\\0"`` for each data file, sorted by path; a missing file hashes as
    empty. Cached by ``data_stamp``."""
    root = _root(repo)
    stamp = data_stamp(root)
    key = os.path.realpath(root)
    hit = _DATA_HASH_CACHE.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    h = hashlib.sha256()
    for rel in data_files(root):
        try:
            with open(os.path.join(root, *rel.split("/")), "rb") as fh:
                data = fh.read()
        except FileNotFoundError:
            data = b""
        h.update(rel.encode("utf-8") + b"\0" + data + b"\0")
    digest = h.hexdigest()
    _DATA_HASH_CACHE[key] = (stamp, digest)
    return digest


# write lock ----------------------------------------------------------------------------------------------------
def _pid_of(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()[:20]
    except OSError:
        return ""


@contextlib.contextmanager
def write_lock(repo: Union[Repo, str], timeout: float = 10) -> Iterator[None]:
    """Hold ``.onto/lock`` for a write. ``fcntl.flock`` when available, else an ``O_CREAT|O_EXCL`` file holding
    the pid that counts as stale after 600 s. Re-entrant within a process. Raises ``LockBusy`` after
    ``timeout`` seconds."""
    root = _root(repo)
    path = os.path.join(root, *LOCK_REL.split("/"))
    key = os.path.realpath(os.path.dirname(path)) + os.sep + "lock"
    if _HELD.get(key):
        _HELD[key] += 1
        try:
            yield
        finally:
            _HELD[key] -= 1
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    deadline = time.monotonic() + max(0.0, float(timeout))
    if _USE_FLOCK and fcntl is not None:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                        raise
                    if time.monotonic() >= deadline:
                        raise LockBusy(
                            "another onto command is writing (%s held by pid %s); try again"
                            % (LOCK_REL, _pid_of(path) or "?"),
                            lock=LOCK_REL,
                        )
                    time.sleep(LOCK_POLL_SECONDS)
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode("ascii"))
            _HELD[key] = 1
            try:
                recover(root)
                yield
            finally:
                _HELD.pop(key, None)
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        return
    mine = str(os.getpid())
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                age = time.time() - os.stat(path).st_mtime
            except OSError:
                continue
            if age > LOCK_STALE_SECONDS:
                with contextlib.suppress(OSError):
                    os.unlink(path)
                continue
            if time.monotonic() >= deadline:
                raise LockBusy(
                    "another onto command is writing (%s held by pid %s); try again" % (LOCK_REL, _pid_of(path) or "?"),
                    lock=LOCK_REL,
                )
            time.sleep(LOCK_POLL_SECONDS)
            continue
        try:
            os.write(fd, mine.encode("ascii"))
        finally:
            os.close(fd)
        break
    _HELD[key] = 1
    try:
        recover(root)
        yield
    finally:
        _HELD.pop(key, None)
        if _pid_of(path) == mine:
            with contextlib.suppress(OSError):
                os.unlink(path)


# the write intent (crash recovery) -----------------------------------------------------------------------------
def _sha_or_none(data: Optional[bytes]) -> Optional[str]:
    return None if data is None else util.sha256_hex(data)


def _read_or_none(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def begin_write(repo: Union[Repo, str], rewrites: Sequence[Tuple[str, Optional[bytes], Optional[bytes]]],
                appends: Sequence[Tuple[str, Optional[bytes]]] = ()) -> None:
    """Record the intent of a multi-file write before its first byte lands. ``rewrites`` are ``(rel, old bytes or
    None when absent, new bytes or None for a delete)``; ``appends`` are ``(rel, old bytes or None)`` for files the
    write appends to. The old bytes of each rewrite are backed up under ``.onto/backup/``; the intent itself is
    written last, so a crash while backing up leaves the data untouched and no intent. Each append of this write
    then records the bytes it adds (``add``) before they land (``append_jsonl``)."""
    root = _root(repo)
    folder = os.path.join(root, *BACKUP_REL.split("/"))
    _clear_folder(folder)
    files: List[Dict[str, Any]] = []
    for n, (rel, old, new) in enumerate(rewrites, start=1):
        entry: Dict[str, Any] = {"path": rel, "old": _sha_or_none(old), "new": _sha_or_none(new), "backup": None}
        if old is not None:
            entry["backup"] = "%d.bak" % n
            write_bytes(os.path.join(folder, entry["backup"]), old)
        files.append(entry)
    grown = [{"path": rel, "size": None if old is None else len(old), "old": _sha_or_none(old), "add": []}
             for rel, old in appends]
    intent = {"at": util.now_iso(), "pid": os.getpid(), "rewrites": files, "appends": grown}
    write_bytes(os.path.join(root, *INTENT_REL.split("/")), util.canonical_bytes(intent))
    _ACTIVE[os.path.realpath(root)] = intent


def _plan_append(path: str, data: bytes) -> None:
    """Add ``data`` to the ``add`` list of the intent entry for ``path`` when this process has a write under way that
    appends to it, and save the intent before the bytes land (a kill in between leaves bytes planned, not written,
    which ``recover`` tells apart)."""
    if not _ACTIVE:
        return
    real = os.path.realpath(path)
    for root, intent in _ACTIVE.items():
        if not real.startswith(root.rstrip(os.sep) + os.sep):
            continue
        rel = os.path.relpath(real, root).replace(os.sep, "/")
        entry = next((e for e in intent.get("appends") or [] if e.get("path") == rel), None)
        intent_path = os.path.join(root, *INTENT_REL.split("/"))
        if entry is None or not os.path.isfile(intent_path):
            return
        entry["add"] = list(entry.get("add") or []) + [data.decode("utf-8")]
        write_bytes(intent_path, util.canonical_bytes(intent))
        return


def end_write(repo: Union[Repo, str]) -> None:
    """Clear the intent of a write that finished (or that was put back in process)."""
    root = _root(repo)
    _ACTIVE.pop(os.path.realpath(root), None)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(os.path.join(root, *INTENT_REL.split("/")))
    _clear_folder(os.path.join(root, *BACKUP_REL.split("/")))


def _clear_folder(folder: str) -> None:
    """Remove a folder of plain files (the backups), ignoring a missing one."""
    if not os.path.isdir(folder):
        return
    for name in os.listdir(folder):
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(folder, name))
    with contextlib.suppress(OSError):
        os.rmdir(folder)


def pending_intent(repo: Union[Repo, str]) -> Optional[Dict[str, Any]]:
    """The intent an interrupted write left behind, or None (an unreadable intent reads as ``{}``)."""
    path = os.path.join(_root(repo), *INTENT_REL.split("/"))
    if not os.path.isfile(path):
        return None
    try:
        value = read_json(path)
    except (DataError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def intent_files(intent: Optional[Dict[str, Any]]) -> List[str]:
    """The files an intent names (rewrites, then appends), each once."""
    out: List[str] = []
    for key in ("rewrites", "appends"):
        for entry in (intent or {}).get(key) or []:
            rel = str(entry.get("path") or "") if isinstance(entry, dict) else ""
            if rel and rel not in out:
                out.append(rel)
    return out


def recovery_note(repo: Union[Repo, str]) -> Optional[Dict[str, Any]]:
    """What the last recovery left as it was (``{at, intent_at, left: [{path, why}]}``), or None."""
    path = os.path.join(_root(repo), *RECOVERY_REL.split("/"))
    try:
        value = read_json(path, None)
    except (DataError, OSError):
        return {"at": None, "intent_at": None, "left": [{"path": RECOVERY_REL, "why": "unreadable"}]}
    return value if isinstance(value, dict) else None


def clear_recovery_note(repo: Union[Repo, str]) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(os.path.join(_root(repo), *RECOVERY_REL.split("/")))


def _planned_tails(entry: Dict[str, Any]) -> List[bytes]:
    """The tails the interrupted write may have left on an appended file: each planned line added in turn (the
    last one may be planned but not written)."""
    tails: List[bytes] = []
    acc = b""
    for piece in entry.get("add") or []:
        if isinstance(piece, str):
            acc += piece.encode("utf-8")
            tails.append(acc)
    return tails


def _undo_append(root: str, entry: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Take back what an interrupted write appended to one file: ``("restored", None)``, ``(None, None)`` when the
    file holds nothing of it (or only lines from elsewhere, which stay), or ``(None, why)`` when its own line sits
    among other lines, so the file is left as it is."""
    rel = str(entry.get("path") or "")
    path = os.path.join(root, *rel.split("/"))
    data = _read_or_none(path)
    if not rel or data is None:
        return None, None
    size = entry.get("size")
    tails = _planned_tails(entry)
    if size is None:
        if tails and data in tails:
            os.unlink(path)
            return "restored", None
        head_ok = True  # the file did not exist: everything in it came after the write began
    else:
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            return None, None
        head_ok = len(data) >= size and util.sha256_hex(data[:size]) == entry.get("old")
        tail = data[size:] if head_ok else b""
        if head_ok and not tail:
            return None, None
        if head_ok and tail in tails:
            write_bytes(path, data[:size])
            return "restored", None
    if "add" not in entry:  # an intent from an older kit, which did not record the line it appends
        return None, ("an older kit's interrupted write did not record its line, so the lines after byte %s were "
                      "kept" % size)
    pieces = [p.encode("utf-8") for p in entry.get("add") or [] if isinstance(p, str)]
    if not any(p.strip() and p.strip() in data for p in pieces):
        return None, None  # nothing of the interrupted write is in the file: lines from a merge or a pull stay
    if head_ok:
        return None, "the interrupted write's own line sits among lines added since (a merge or a pull?); it was kept"
    return None, "the file changed before the interrupted write's line (a merge or a hand edit since?); it was kept"


def recover(repo: Union[Repo, str]) -> List[str]:
    """Roll back a write a killed process left half done (see ``begin_write``); returns the paths put back. A rewrite
    changed since (neither its old nor its planned bytes) is left as it is. An appended file loses exactly the bytes
    the write planned for it, and only when they are its whole tail: lines a merge or a pull added since stay, and a
    file whose tail mixes the write's own line with other lines is left as it is and listed in the recovery note
    (``recovery_note``). Called by ``write_lock`` when it is first taken, so it runs before any other write."""
    intent = pending_intent(repo)
    if intent is None:
        return []
    root = _root(repo)
    if not intent:
        raise DataError("%s is unreadable: an interrupted write cannot be rolled back; check the graph files "
                        "(onto validate), then delete it" % INTENT_REL, file=INTENT_REL)
    folder = os.path.join(root, *BACKUP_REL.split("/"))
    restored: List[str] = []
    left: List[Dict[str, str]] = []
    # appends first: a write may rewrite a log and then append to it (an erase scrubs names from the change log),
    # so the append is undone before the rewrite it grew from
    for entry in intent.get("appends") or []:
        if not isinstance(entry, dict):
            continue
        done, why = _undo_append(root, entry)
        if done:
            restored.append(str(entry.get("path")))
        elif why:
            left.append({"path": str(entry.get("path")), "why": why})
    for entry in intent.get("rewrites") or []:
        rel = str(entry.get("path") or "")
        path = os.path.join(root, *rel.split("/"))
        current = _sha_or_none(_read_or_none(path))
        if not rel or current == entry.get("old") or current != entry.get("new"):
            continue
        if entry.get("old") is None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
        else:
            data = _read_or_none(os.path.join(folder, str(entry.get("backup") or "")))
            if data is None or util.sha256_hex(data) != entry.get("old"):
                raise DataError("the backup of %s is missing or damaged; an interrupted write cannot be rolled "
                                "back (see %s)" % (rel, INTENT_REL), file=rel)
            write_bytes(path, data)
        if rel not in restored:
            restored.append(rel)
    if left:
        write_bytes(os.path.join(root, *RECOVERY_REL.split("/")),
                    util.canonical_bytes({"at": util.now_iso(), "intent_at": intent.get("at"), "left": left}))
    end_write(root)
    clear_cache()
    return restored


def interrupted_lines(repo: Union[Repo, str]) -> List[str]:
    """One line for a write that stopped half way and is not rolled back yet, and one per file the last recovery
    left as it was; empty when neither (``onto status`` and the session hook print them)."""
    out: List[str] = []
    intent = pending_intent(repo)
    if intent is not None:
        files = intent_files(intent)
        out.append("a write stopped half way%s%s; nothing reads it as done until it is rolled back: run onto "
                   "validate --fix" % (" (started %s)" % intent["at"] if intent.get("at") else "",
                                       ", touching %s" % ", ".join(files[:4]) if files else ""))
    note = recovery_note(repo)
    for item in (note or {}).get("left") or []:
        if isinstance(item, dict):
            out.append("recovery left %s as it is: %s; check it (git diff), then run onto validate --fix to clear "
                       "this note" % (item.get("path"), item.get("why")))
    return out


# paths ---------------------------------------------------------------------------------------------------------
def inside(root: str, path: str) -> bool:
    """True when ``path`` is ``root`` or under it, after links are followed."""
    real, base = os.path.realpath(path), os.path.realpath(root)
    return real == base or real.startswith(base.rstrip(os.sep) + os.sep)


def _credential_like(real: str) -> bool:
    name = os.path.basename(real)
    parts = real.split(os.sep)
    return name.startswith(".") or bool(CREDENTIAL_FILES.match(name)) or any(p in CREDENTIAL_DIRS for p in parts)


def guard_input_path(path: str, repo: Optional[Union[Repo, str]], allow_any: bool = False) -> str:
    """The real path of an input file or folder, or ``Refused``. Always refused: dotfiles, credential names
    (``.netrc``, ``.pgpass``, ``id_rsa*``, ``*.pem``, ``credentials*``, ``.env*``) and anything under ``~/.ssh``,
    ``~/.aws``, ``~/.docker``, ``~/.gnupg`` or ``~/.kube``. Unless ``allow_any`` (CLI only), the path must sit
    inside the repo (``inbox/`` is the drop folder) and not inside a hidden folder of it such as ``.git``."""
    if not path or not isinstance(path, str):
        raise UsageError("no input path given")
    expanded = os.path.expanduser(path)
    if not os.path.lexists(expanded):
        raise UsageError("no such file or folder: %s" % path)
    real = os.path.realpath(expanded)
    home = os.path.realpath(os.path.expanduser("~"))
    for folder in CREDENTIAL_DIRS:
        if inside(os.path.join(home, folder), real):
            raise Refused("%s is under ~/%s, which holds credentials; nothing was read" % (path, folder))
    if _credential_like(real) or _credential_like(os.path.abspath(expanded)):
        raise Refused("%s looks like a credential or configuration file; nothing was read" % path)
    if allow_any:
        return real
    if repo is None:
        raise Refused("%s: no topic repo to read into; inputs must sit inside the repo or its inbox/" % path)
    root = os.path.realpath(_root(repo))
    if not inside(root, real):
        raise Refused(
            "%s is outside the topic repo (after links are followed); copy it into %s first, or pass "
            "--allow-any-path on the command line" % (path, os.path.join(_root(repo), "inbox"))
        )
    rel = os.path.relpath(real, root)
    if rel != "." and any(part.startswith(".") for part in rel.split(os.sep)):
        raise Refused("%s is inside a hidden folder of the repo; nothing was read" % path)
    return real


def _topic_files(root: str) -> Tuple[List[str], List[str]]:
    """(files, links) under the topic folders and the root data files, relative and ``/``-separated."""
    found: List[str] = []
    for name in sorted(os.listdir(root)):
        full = os.path.join(root, name)
        if name in TOPIC_DIRS and os.path.isdir(full) and not os.path.islink(full):
            for dirpath, dirs, names in os.walk(full):
                dirs.sort()
                entries = sorted(names) + [d for d in dirs if os.path.islink(os.path.join(dirpath, d))]
                for entry in entries:
                    found.append(os.path.relpath(os.path.join(dirpath, entry), root).replace(os.sep, "/"))
        elif name in TOPIC_DIRS or name in ROOT_DATA_FILES or is_junk(root, name):
            found.append(name)
    links = [p for p in found if os.path.islink(os.path.join(root, *p.split("/")))]
    return found, links


def path_problems(repo: Union[Repo, str]) -> List[Problem]:
    """P19: a path under the topic folders outside the allow-list, a symbolic link, or a junk file
    (``__pycache__``, ``.DS_Store``, ``* 2.*``). ``build/index.html`` (gitignored viewer) is skipped."""
    root = _root(repo)
    files, links = _topic_files(root)
    problems: List[Problem] = []
    for rel in files:
        if rel in IGNORED_OUTPUTS:
            continue
        if rel in links:
            problems.append(Problem("P19", rel, 0, "a symbolic link; topic folders hold regular files only"))
        elif COPY_RE.search(rel) and is_conflict_copy(root, rel):
            # a sync or Finder copy ("nodes 2.jsonl") may hold the newer edits, and git ignores it: never delete it
            problems.append(Problem("P19", rel, 0, "a conflict copy (a sync or Finder duplicate, which git ignores); "
                                                   "compare it with its original, keep one, and move the copy out of "
                                                   "the repo; do not delete it unread"))
        elif _JUNK_ONLY_RE.search(rel):
            problems.append(Problem("P19", rel, 0, "a junk file; delete it"))
        elif not any(rx.match(rel) for rx in ALLOWED):
            problems.append(Problem("P19", rel, 0, "not a path a topic repo holds; remove it or move it to inbox/"))
    return problems


# release and version -------------------------------------------------------------------------------------------
def load_release_manifest(repo: Union[Repo, str]) -> Optional[Dict[str, Any]]:
    """``MANIFEST.json`` as written by the last release, or None when absent or unreadable."""
    path = os.path.join(_root(repo), "MANIFEST.json")
    try:
        value = read_json(path, None)
    except DataError:
        return None
    return value if isinstance(value, dict) else None


def _changes_after(root: str, last_change: Optional[str]) -> Optional[int]:
    rows, _problems = read_jsonl(os.path.join(root, "ledger", "changes.jsonl"))
    ids = [r.get("id") for r in rows]
    if not last_change or last_change not in ids:
        return None
    later = rows[ids.index(last_change) + 1 :]
    return sum(1 for r in later if r.get("type") not in ("release", "checkpoint"))


# The version line is quoted by agents, so only text that matches its grammar is printed from the data files.
_STAMP_REF_RE = re.compile(r"^(v[0-9]+|[0-9a-f]{7,40})\Z")
_STAMP_VERSION_RE = re.compile(r"^v[0-9]+\Z")
_STAMP_KIT_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+([.+-][0-9A-Za-z.+-]{1,20})?\Z")
_HEX_RE = re.compile(r"^[0-9a-f]+\Z")


def _stamp_ns(value: Any) -> str:
    text = str(value or "")
    return text if ids.NS_RE.match(text) and text not in ids.RESERVED_NS else "?"


def _import_pins(root: str) -> List[Dict[str, Any]]:
    lock = read_json(os.path.join(root, "imports", "lock.json"), None)
    if not isinstance(lock, dict):
        return []
    pins: List[Dict[str, Any]] = []
    for entry in lock.get("imports") or []:
        if not isinstance(entry, dict):
            continue
        ns = str(entry.get("ns") or "")
        if _stamp_ns(ns) == "?":
            continue  # not a namespace: never printed (lockfile.verify reports the entry)
        vendored = os.path.join(root, "imports", ns, "export.json")
        try:
            ok = file_sha256(vendored) == entry.get("export_sha256")
        except OSError:
            ok = False
        ref = entry.get("ref")
        commit7 = str(entry.get("commit") or "")[:7]
        pins.append({"ns": ns, "ref": ref if isinstance(ref, str) and _STAMP_REF_RE.match(ref) else None,
                     "commit7": commit7 if _HEX_RE.match(commit7) else "", "ok": ok})
    return pins


def version_stamp(repo: Repo, onto: Any = None, richness: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The version object printed on every output:

    ``{ns, version, data_hash, matches_release, changes_after, imports: [{ns, ref, commit7, ok}], kit, repo_kit,
    kit_mismatch, richness}``. ``version`` is the last release's (``MANIFEST.json``) or ``unreleased``;
    ``matches_release`` is None without a release; ``changes_after`` counts data changes logged after the release's
    last change (None when unknown); ``richness`` is passed in by the caller (``{score, band, change_text}``)."""
    root = repo.root
    digest = data_hash(root)
    release = load_release_manifest(root)
    if release:
        version = str(release.get("version") or "unreleased")
        version = version if _STAMP_VERSION_RE.match(version) or version == "unreleased" else "v?"
        matches: Optional[bool] = release.get("data_hash") == digest
        after = 0 if matches else _changes_after(root, release.get("last_change"))
    else:
        version, matches, after = "unreleased", None, None
    repo_kit = repo.manifest.get("kit")
    if repo_kit is not None and not (isinstance(repo_kit, str) and _STAMP_KIT_RE.match(repo_kit)):
        repo_kit = "?"
    return {
        "ns": _stamp_ns(repo.ns),
        "version": version,
        "data_hash": digest,
        "matches_release": matches,
        "changes_after": after,
        "imports": _import_pins(root),
        "kit": __version__,
        "repo_kit": repo_kit,
        "kit_mismatch": bool(repo_kit) and repo_kit != __version__,
        "richness": richness,
    }
