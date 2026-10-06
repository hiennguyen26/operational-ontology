"""The release ladder and the secret scan.

``onto release`` is a ladder; the default is a dry run that prints the plan and writes nothing. Exit codes: 0 ok,
1 a check failed, 2 secret-like strings or denylisted terms found.

1. ``validate``: any problem fails the release.
2. ``scan``: every file the release commit could carry (``git ls-files --cached --others --exclude-standard``, or
   the whole folder outside git) for the secret patterns and the denylist (``--denylist`` or ``$ONTO_DENYLIST``, a
   file kept outside the repo); ``--notes`` too. A hit prints only the path, the kind and a 6-character prefix,
   and notes with a hit are never echoed. A file that cannot be read fails the release (exit 1). ``--notes`` then
   go through the source sanitizer under the topic's policy, as decision and checkpoint text does
   (``ledger.clean_text``): a credential it finds fails the release (exit 2), personal data is redacted, or fails
   the release (exit 1) when ``policy.personal`` refuses its kind. Refused notes are never echoed; redacted ones
   are what ``VERSIONS.md``, the ``release`` change, the commit and the tag message carry.
3. Paths: junk files (``__pycache__``, ``.DS_Store``, ``* 2.*``) in that set, topic paths outside the
   allow-list, and any ``.onto/`` or ``inbox/`` file git tracks (in the index or in ``HEAD``): the release commit
   would carry raw inbox text, and the scan looks for secrets and denylisted terms, not personal data. With
   ``--push``, also commits not yet on ``origin`` that add or change such files, and those that bring in content
   ``onto erase`` has removed since (``unpushed_erased_commits``): the push would publish either in the branch
   history. With ``--push`` and a denylist, the author, committer and message of the commits not yet on
   ``origin`` (or on ``kit``, the kit's own public history) are scanned too (exit 2 on a hit). The refusal gives
   the squash for the user to run; the kit never rewrites history. Without ``--push``, a plan notes those commits
   when the topic has an ``origin`` remote.
4. The next tag ``vN``: strictly above every existing ``v<number>`` tag.
5. ``--write``: the export (``version=vN``) and the cards, the ``vN`` row of ``VERSIONS.md`` (added or replaced)
   and ``MANIFEST.json``. ``--notes`` is required.
6. ``--commit``: refused while files other than the release outputs are dirty, unless ``--allow-dirty``. It
   appends a ``release`` change and history point, commits exactly the release outputs and creates the annotated
   tag ``vN``.
7. ``--push``: pushes the branch and the tag to ``origin``; refused on a detached HEAD. Only when the user asks.

Each step implies the ones before it (``--push`` commits, ``--commit`` writes). The writes are all or nothing: a
failure before the commit puts every output back, and the index entries the commit step staged go back to what
they were (also on a branch with no commit yet). The runtime paths ``.onto/``, ``inbox/`` and
``build/index.html`` are gitignored in a topic repo and are never part of a release: a tracked ``.onto/`` or
``inbox/`` file fails step 3, and a tracked ``build/index.html`` (a viewer someone chose to publish) is carried.

``onto scan`` exits 0 when clean, 2 on hits and 1 on a usage or IO error, including a file or folder it cannot
read (the scan would otherwise pass it unseen).

``MANIFEST.json`` lists the sha256 of every file the release commit holds except itself: after ``--commit``, the
files of the commit; after ``--write`` alone, every file ``git add -A`` would commit.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import FORMAT, __version__, build, gitutil, history, ledger, mutate, records, sanitize, secrets, sources, store
from . import util
from . import validate as validate_mod
from .commands import Context
from .errors import DataError, GitError, Refused
from .graph import EDGES, NODES, Ontology

RELEASE_FILES = ("build/export.json", "build/cards.json", "VERSIONS.md", "MANIFEST.json")
LOG_FILES = ("ledger/changes.jsonl", "metrics/history.jsonl")
OUTPUTS = RELEASE_FILES + LOG_FILES
RUNTIME = (".onto/", "inbox/", "build/index.html")
PRIVATE_RUNTIME = (".onto/", "inbox/")  # raw drop text and kit state: never in a release, even when tracked
MANIFEST = "MANIFEST.json"
VERSIONS = "VERSIONS.md"
DENYLIST_ENV = "ONTO_DENYLIST"
TAG_RE = re.compile(r"^v([0-9]+)\Z")
ROW_RE = re.compile(r"^\| (v[0-9]+) \|")
VERSIONS_HEAD = (
    "# Versions",
    "",
    "Written by `onto release --write`: one row per release tag.",
    "",
    "| Version | Date | Data | Nodes | Edges | Richness | Notes |",
    "|---|---|---|---|---|---|---|",
)
NOTES_WITHHELD = "<withheld: %d scan hit(s)>"
NOTES_REFUSED = "<withheld: holds %s>"
GIT_WRITE_TIMEOUT = 120
SHOW_LINES = 20
_URL_CREDENTIALS = re.compile(r"(://)[^/@\s]+@")
ERASE_MARK = b"[erased by "  # an erased source's stored text starts so (mutate)
BLOB_CHUNK = 64  # blobs read per git cat-file call when history is checked for erased content


# small helpers -------------------------------------------------------------------------------------------------
def _is_runtime(rel: str) -> bool:
    return any(rel == r or (r.endswith("/") and rel.startswith(r)) for r in RUNTIME)


def tracked_private(state: Optional[Dict[str, Any]]) -> List[str]:
    """The ``.onto/`` and ``inbox/`` files git tracks (in the index or in ``HEAD``), relative to the topic."""
    return sorted(p for p in (state or {}).get("tracked") or () if p.startswith(PRIVATE_RUNTIME))


def unpushed_private_commits(root: str) -> List[str]:
    """Short ids of the commits reachable from ``HEAD`` but from no ``origin`` remote-tracking branch that add,
    change or remove a ``.onto/`` or ``inbox/`` file: a push would publish that text in the branch history."""
    out = _git_raw(root, "rev-list", "--abbrev-commit", "HEAD", "--not", "--remotes=origin", "--",
                   *[r.rstrip("/") for r in PRIVATE_RUNTIME])
    return [line for line in (out or "").splitlines() if line]


def unpushed_commit_hits(root: str, patterns: Sequence["re.Pattern[str]"]) -> List[Dict[str, Any]]:
    """Denylist hits in the author, the committer and the message of the commits reachable from ``HEAD`` but from
    no ``origin`` or ``kit`` remote-tracking branch: a push publishes those with the branch, and the file scan
    cannot see them. The kit's own history (on ``kit``, which ``onto setup`` makes from the template's ``origin``)
    is already public and is not the user's to rewrite, so it is left out. Each hit is ``{path: "commit <id>
    <what>", kind: "denylist", prefix, line}``; the matched text is never shown."""
    if not patterns:
        return []
    out = _git_raw(root, "log", "--no-color", "--format=%h%x1f%an <%ae>%x1f%cn <%ce>%x1f%B%x1e", "HEAD", "--not",
                   "--remotes=origin", "--remotes=kit")
    hits: List[Dict[str, Any]] = []
    for record in (out or "").split("\x1e"):
        parts = record.strip("\n").split("\x1f")
        if len(parts) != 4 or not parts[0].strip():
            continue
        commit = parts[0].strip()
        for what, text in (("author", parts[1]), ("committer", parts[2]), ("message", parts[3])):
            for rx in patterns:
                if rx.search(text):
                    hits.append(_hit("commit %s %s" % (commit, what), "denylist", rx.pattern[:6]))
    return hits


def erased_ids(repo: store.Repo) -> Tuple[Set[str], Set[str]]:
    """(the local nodes, the sources) that ``onto erase`` has erased in the work tree."""
    def gone(rel: str) -> Set[str]:
        rows, _problems = store.read_jsonl(repo.path(rel))
        return {r["id"] for r in rows if r.get("erased") is True and isinstance(r.get("id"), str)}

    return gone(NODES), gone(sources.INDEX)


def _quotes(rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    prov = rec.get("prov")
    return [p for p in prov if isinstance(p, dict) and "quote" in p] if isinstance(prov, list) else []


def _held(kind: str, rec: Dict[str, Any], nodes: Set[str], srcs: Set[str]) -> Set[str]:
    """The erased ids whose old content the record ``rec`` (a line of ``kind`` "node", "edge" or "source") holds:
    an erased node or source that is not erased in it, an edge on an erased node that keeps a quote or a note, and
    a quote citing an erased source."""
    out: Set[str] = set()
    rid = rec.get("id")
    if kind in ("node", "source") and rid in (nodes if kind == "node" else srcs) and rec.get("erased") is not True:
        out.add(str(rid))
    if kind == "edge" and (rec.get("note") or _quotes(rec)):
        out.update(end for end in (rec.get("src"), rec.get("dst")) if isinstance(end, str) and end in nodes)
    if kind != "source":
        out.update(p["src"] for p in _quotes(rec) if isinstance(p.get("src"), str) and p["src"] in srcs)
    return out


def _held_lines(data: bytes, kind: str, tokens: Sequence[bytes], nodes: Set[str],
                srcs: Set[str]) -> Dict[str, Tuple[bytes, Set[str]]]:
    """``{id: (line, erased ids it holds)}`` for the lines of a JSONL blob that name an erased id. A byte test
    comes first, so only those lines are parsed."""
    out: Dict[str, Tuple[bytes, Set[str]]] = {}
    for line in data.split(b"\n"):
        if not any(t in line for t in tokens):
            continue
        try:
            rec = util.loads_record(line.decode("utf-8"))
        except (ValueError, RecursionError, UnicodeDecodeError):
            continue  # validate reports a bad line
        if isinstance(rec, dict) and isinstance(rec.get("id"), str):
            out[rec["id"]] = (line.strip(), _held(kind, rec, nodes, srcs))
    return out


def _null_oid(oid: str) -> bool:
    return not oid.strip("0")


def _raw_log(root: str, paths: Sequence[str]) -> Optional[List[Tuple[str, List[Tuple[List[str], str, str]]]]]:
    """``[(commit, [(parent blob ids, blob id, path from the work tree top)])]`` for the commits reachable from
    ``HEAD`` but from no ``origin`` remote-tracking branch that change ``paths``, children before parents; None
    when git fails. A merge lists only the files that differ from every parent (``-c``)."""
    out = _git_bytes(root, "log", "--full-history", "--topo-order", "-c", "--raw", "--no-renames", "--no-abbrev", "-z",
                     "--no-color", "--no-show-signature", "--format=%x01%H", "HEAD", "--not", "--remotes=origin",
                     "--", *paths, timeout=GIT_WRITE_TIMEOUT)
    if out is None:
        return None
    commits: List[Tuple[str, List[Tuple[List[str], str, str]]]] = []
    for chunk in out.split(b"\x01"):
        tokens = [t.strip(b"\n") for t in chunk.split(b"\0")]
        if not tokens[0]:
            continue
        entries: List[Tuple[List[str], str, str]] = []
        rest = iter(tokens[1:])
        for token in rest:
            if not token.startswith(b":"):
                continue
            path = next(rest, b"").decode("utf-8", "replace")
            parents = len(token) - len(token.lstrip(b":"))
            fields = token.lstrip(b":").decode("ascii", "replace").split()
            oids = fields[parents + 1:2 * parents + 2]  # a mode per parent and one for the result, then the blobs
            if len(oids) == parents + 1:
                entries.append((oids[:-1], oids[-1], path))
        commits.append((tokens[0].decode("ascii", "replace"), entries))
    return commits


def unpushed_erased_commits(repo: store.Repo, state: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """The commits reachable from ``HEAD`` but from no ``origin`` remote-tracking branch that bring in content
    ``onto erase`` has removed since: a line of ``graph/nodes.jsonl``, ``graph/edges.jsonl`` or
    ``sources/index.jsonl`` that holds an erased node or source (``_held``) and differs from that line in every
    parent, the text of an erased source other than the erase marker, or its kept original. A push would publish
    that content in the branch history. Content a commit carries over from a parent is not counted again: a parent
    not yet on origin is listed itself, and one on origin has published it already. Returns ``[{"commit", "full",
    "ids"}]``, oldest first (``[]`` when nothing is erased), or None when git cannot list or read the commits."""
    nodes, srcs = erased_ids(repo)
    if not nodes and not srcs:
        return []
    root = repo.root
    prefix = state.get("prefix") or ""
    kinds = {prefix + NODES: "node", prefix + EDGES: "edge", prefix + sources.INDEX: "source"}
    folder = prefix + "sources/"
    commits = _raw_log(root, [NODES, EDGES, "sources"])
    if commits is None:
        return None
    tokens = [util.canonical_line(i).encode("utf-8") for i in sorted(nodes | srcs)]
    wanted: Dict[Tuple[str, str], None] = {}
    for _commit, entries in commits:
        for parents, oid, path in entries:
            if path in kinds:
                for each in parents + [oid]:
                    if not _null_oid(each):
                        wanted[(each, kinds[path])] = None
            elif path.startswith(folder) and path[len(folder):-4] in srcs and path.endswith(".txt"):
                if not _null_oid(oid):
                    wanted[(oid, "text")] = None
    parsed: Dict[Tuple[str, str], Any] = {}
    keys = list(wanted)
    try:
        for start in range(0, len(keys), BLOB_CHUNK):
            part = keys[start:start + BLOB_CHUNK]
            for (oid, kind), data in zip(part, gitutil.blobs(root, [oid for oid, _kind in part])):
                if data is None:
                    return None
                parsed[(oid, kind)] = data.startswith(ERASE_MARK) if kind == "text" else \
                    _held_lines(data, kind, tokens, nodes, srcs)
    except GitError:
        return None
    found: List[Dict[str, Any]] = []
    for commit, entries in commits:
        ids: Set[str] = set()
        for parents, oid, path in entries:
            name = path[len(folder):] if path.startswith(folder) else ""
            if path in kinds:
                after = {} if _null_oid(oid) else parsed[(oid, kinds[path])]
                before = [{} if _null_oid(p) else parsed[(p, kinds[path])] for p in parents]
                for rid, (line, held) in after.items():
                    if held and all((old.get(rid) or (None,))[0] != line for old in before):
                        ids |= held
            elif _null_oid(oid):
                continue  # the file was removed
            elif name.endswith(".txt") and name[:-4] in srcs and not parsed[(oid, "text")]:
                ids.add(name[:-4])
            elif ".orig." in name and name.split(".orig.", 1)[0] in srcs:
                ids.add(name.split(".orig.", 1)[0])
        if ids:
            found.append({"commit": commit[:7], "full": commit, "ids": sorted(ids)})
    found.reverse()
    return found


def _squash_base(root: str, branch: Optional[str]) -> Optional[str]:
    """What to squash the commits not yet on origin onto: ``origin/<branch>`` when they build on it, else a commit
    on origin they build on; None when nothing on origin precedes them (a first push)."""
    out = _git_raw(root, "rev-list", "--boundary", "HEAD", "--not", "--remotes=origin") or ""
    boundary = [line[1:] for line in out.splitlines() if line.startswith("-")]
    if not boundary:
        return None
    upstream = gitutil.git(root, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/%s^{commit}" % branch) \
        if branch else ""
    return "origin/%s" % branch if upstream and upstream in boundary else boundary[0][:7]


def erased_history_text(found: Sequence[Dict[str, Any]], root: str, branch: Optional[str],
                        head: Optional[str]) -> str:
    """The ``--push`` refusal for ``unpushed_erased_commits``, with the squash for the user to run."""
    ids = sorted({i for hit in found for i in hit["ids"]})
    base = _squash_base(root, branch)
    first = "git reset --soft %s" % base if base else "git update-ref -d HEAD (nothing on origin precedes them)"
    steps = "%s, then git commit -m \"<message>\"" % first
    if head and any(hit.get("full") == head for hit in found):
        steps = "commit the erase, then " + steps  # HEAD itself still holds the content
    return ("%d commit(s) not yet on origin hold content that onto erase removed since (%s%s), for example %s; "
            "--push would publish it in the branch history. Squash the commits not yet on origin into one, so the "
            "push carries only what is left after the erase (for the user to run; the files on disk stay as they "
            "are): %s; then run the release again"
            % (len(found), ", ".join(ids[:5]), " +%d more" % (len(ids) - 5) if len(ids) > 5 else "",
               ", ".join(hit["commit"] for hit in found[:3]), steps))


def untrack_advice(paths: Sequence[str]) -> str:
    """The commands that stop git tracking ``paths`` while keeping the files on disk."""
    dirs = sorted({p.split("/", 1)[0] for p in paths})
    return ("git rm -r --cached --ignore-unmatch -- %s (the files stay on disk), list %s in .gitignore, and commit"
            % (" ".join(dirs), " and ".join(d + "/" for d in dirs)))


def _read_or_none(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def _put_back(path: str, data: Optional[bytes]) -> None:
    if data is None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return
    store.write_bytes(path, data)


def _scrub(text: str) -> str:
    """A git message without credentials: user:password in URLs and secret-like strings are cut to a prefix."""
    text = _URL_CREDENTIALS.sub(r"\1***@", text)
    for _kind, match in secrets.scan_str(text):
        text = text.replace(match, match[:6] + "...")
    return text


def _git_bytes(root: str, *args: str, timeout: float = gitutil.GIT_TIMEOUT) -> Optional[bytes]:
    """Read-only git output as bytes, or None on failure."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    try:
        proc = subprocess.run(["git", "-C", root] + list(args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _git_raw(root: str, *args: str) -> Optional[str]:
    """Read-only git output unstripped (for ``-z`` lists), or None on failure."""
    out = _git_bytes(root, *args)
    return None if out is None else out.decode("utf-8", "replace")


def _git_write(root: str, *args: str) -> str:
    """A git command that writes (add, commit, tag, push); ``GitError`` with a scrubbed message on failure."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        proc = subprocess.run(["git", "-C", root] + list(args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env, timeout=GIT_WRITE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError("git %s could not run: %s" % (args[0], type(exc).__name__))
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()
        raise GitError("git %s failed: %s" % (args[0], _scrub(detail)[:400]))
    return proc.stdout.decode("utf-8", "replace").strip()


def _z_list(text: Optional[str]) -> List[str]:
    return [p for p in (text or "").split("\0") if p]


def _index_entries(root: str, rels: Sequence[str]) -> Dict[str, List[str]]:
    """The index entries of ``rels`` (paths relative to ``root``) as ``git ls-files -s`` prints them, keyed by the
    path from the work tree top: ``{path: ["<mode> <object> <stage>\\t<path>", ...]}``."""
    out: Dict[str, List[str]] = {}
    for line in _z_list(_git_raw(root, "ls-files", "-s", "-z", "--full-name", "--", *rels)):
        _info, _tab, path = line.partition("\t")
        out.setdefault(path, []).append(line)
    return out


def _restore_index(root: str, prefix: str, rels: Sequence[str], before: Dict[str, List[str]]) -> None:
    """Put the index entries of ``rels`` back as ``_index_entries`` recorded them: an entry that existed is
    rewritten, a path that had none is removed from the index. Works on a branch with no commit yet, and keeps what
    the user had staged before the release. Best effort: a failure leaves the index as it is."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    absent = [rel for rel in rels if not before.get(prefix + rel)]
    lines = [line for rel in rels for line in before.get(prefix + rel) or []]
    steps: List[Tuple[List[str], Optional[bytes]]] = []
    if absent:
        steps.append((["update-index", "--force-remove", "--"] + absent, None))
    if lines:
        steps.append((["update-index", "-z", "--index-info"], "".join(line + "\0" for line in lines).encode("utf-8")))
    for args, data in steps:
        try:
            subprocess.run(["git", "-C", root] + args, input=data if data is not None else b"",
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=GIT_WRITE_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            pass


# git state -----------------------------------------------------------------------------------------------------
def git_state(root: str) -> Optional[Dict[str, Any]]:
    """``{prefix, head, branch, tags, tracked}`` of the git work tree holding ``root``, or None outside git.
    ``prefix`` is the topic folder's path inside the work tree (``""`` at its top)."""
    if gitutil.git(root, "rev-parse", "--is-inside-work-tree") != "true":
        return None
    prefix = gitutil.git(root, "rev-parse", "--show-prefix")
    head = gitutil.head(root)
    tracked = set(_z_list(_git_raw(root, "ls-files", "--cached", "-z")))
    if head:
        tracked |= set(_z_list(_git_raw(root, "ls-tree", "-r", "-z", "--name-only", head)))
    return {"prefix": prefix, "head": head, "branch": gitutil.branch(root), "tags": gitutil.tags(root, "v*"),
            "tracked": tracked}


def next_tag(tags: Iterable[str]) -> Tuple[str, Optional[str]]:
    """(the next tag ``vN``, the highest existing one or None): N is one above every ``v<number>`` tag; other tags
    (``v2-rc``, ``version-1``) are ignored."""
    numbers = [int(m.group(1)) for m in (TAG_RE.match(t) for t in tags) if m]
    if not numbers:
        return "v1", None
    top = max(numbers)
    return "v%d" % (top + 1), "v%d" % top


def _walk(root: str) -> List[str]:
    out: List[str] = []
    for dirpath, dirs, names in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        dirs[:] = sorted(d for d in dirs if d != ".git" and not _is_runtime(rel_dir + d + "/"))
        for name in sorted(n for n in names if n != ".git"):
            rel = rel_dir + name
            if not _is_runtime(rel):
                out.append(rel)
    return out


def release_files(repo: store.Repo, state: Optional[Dict[str, Any]] = None) -> List[str]:
    """The files a release commit could carry, relative to the topic root: the git commit set (tracked plus
    untracked, minus ignored) or, outside git, every file under the root. Untracked runtime paths are left out."""
    root = repo.root
    if state is None:
        state = git_state(root)
    if state is None:
        return _walk(root)
    tracked = state.get("tracked") or set()
    return [p for p in gitutil.commit_set(root) if not _is_runtime(p) or p in tracked]


def dirty_paths(repo: store.Repo, state: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """(every dirty path relative to the topic, dirty paths other than the release outputs). Paths outside the
    topic folder are shown as ``:/<path from the work tree top>``; untracked runtime paths are not dirt."""
    prefix = state.get("prefix") or ""
    tracked = state.get("tracked") or set()
    everything: List[str] = []
    others: List[str] = []
    for path in gitutil.dirty_paths(repo.root):
        if prefix and not path.startswith(prefix):
            everything.append(":/" + path)
            others.append(":/" + path)
            continue
        rel = path[len(prefix):]
        everything.append(rel)
        if rel in OUTPUTS or (_is_runtime(rel) and rel not in tracked):
            continue
        others.append(rel)
    return everything, others


# scanning ------------------------------------------------------------------------------------------------------
def denylist_path(value: Optional[str], env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """The denylist file: ``value``, else ``$ONTO_DENYLIST``; None when neither is set (placeholders count as
    unset)."""
    if value and not store.is_placeholder(value):
        return os.path.abspath(os.path.expanduser(value))
    env = os.environ if env is None else env
    found = env.get(DENYLIST_ENV)
    if found and not store.is_placeholder(found):
        return os.path.abspath(os.path.expanduser(found))
    return None


def load_denylist(path: Optional[str], outside: Sequence[str] = ()) -> List["re.Pattern[str]"]:
    """Compiled patterns of the denylist at ``path`` ([] for None). The file must be readable and sit outside
    every folder in ``outside`` (the repo, the scanned paths): a denylist inside them would be scanned, and could
    be committed. Raises ``DataError`` (exit 1) otherwise."""
    if not path:
        return []
    for folder in outside:
        if folder and store.inside(folder, path):
            raise DataError("the denylist %s sits inside %s; keep it outside the repo and the scanned folders"
                            % (path, folder))
    try:
        return secrets.load_denylist(path)
    except (OSError, UnicodeDecodeError) as exc:
        raise DataError("cannot read the denylist %s: %s" % (path, getattr(exc, "strerror", None) or exc))


def _hit(path: str, kind: str, prefix: str, line: Optional[int] = None) -> Dict[str, Any]:
    return {"path": path, "kind": kind, "prefix": prefix, "line": line}


def _scan_bytes(rel: str, data: bytes, patterns: Sequence["re.Pattern[str]"], path: Optional[str] = None,
                cache: Optional[Dict[str, bool]] = None) -> List[Dict[str, Any]]:
    """Hits in one file: the secret patterns plus the sanitizer's credential kinds (``secrets.scan_file``, which
    skips the credential kinds inside a copy of the kit; ``path`` is the file on disk, ``rel`` by default), then
    the denylist."""
    hits = [_hit(rel, kind, prefix) for kind, prefix in secrets.scan_file(path or rel, data, cache)]
    if patterns:
        for rx in patterns:
            if rx.search(rel):
                hits.append(_hit(rel, "denylist", rx.pattern[:6], 0))
        text = data.decode("utf-8", "replace")
        for number, line in enumerate(text.splitlines(), start=1):
            for rx in patterns:
                if rx.search(line):
                    hits.append(_hit(rel, "denylist", rx.pattern[:6], number))
    return hits


def scan_text(label: str, text: str, patterns: Sequence["re.Pattern[str]"] = ()) -> List[Dict[str, Any]]:
    """Hits in a piece of text (``--notes``), reported under ``label``."""
    hits = [_hit(label, kind, match[:6]) for kind, match in secrets.scan_str(text)]
    for rx in patterns:
        if rx.search(text):
            hits.append(_hit(label, "denylist", rx.pattern[:6]))
    return hits


def clean_notes(repo: store.Repo, text: str) -> Tuple[Optional[str], Dict[str, Any]]:
    """``--notes`` through the source sanitizer under the topic's policy, the check decision and checkpoint text
    get (``ledger.clean_text``): (the notes as a release may store them, or None when refused; ``{"refused":
    kinds, "redacted": kinds, "local": bool, "message": the refusal or None}``). The notes land in ``VERSIONS.md``,
    the change log, the commit and the tag message, and ``onto erase`` cannot reach a tag message. Like every
    published text, they go through ``build.Redactor.scrub`` last: a left-out record's id or name reads
    ``[local record]`` (``local`` is then true)."""
    try:
        clean = ledger.clean_text(repo, text, "--notes")
    except Refused as exc:
        kinds = sorted({str(k) for k in exc.extra.get("kinds") or []}) or ["refused"]
        return None, {"refused": kinds, "redacted": [], "local": False, "message": exc.message}
    redacted = sanitize.check_text(text, repo.policy) if clean != text else []
    try:
        raw = build.redactor_for(Ontology.load(repo)).scrub(clean)
    except DataError:  # a graph that cannot load fails validate, and with it the release
        raw = clean
    local = raw != clean
    return (util.normalize_ws(raw) if local else clean), {"refused": [], "redacted": redacted, "local": local,
                                                          "message": None}


def _scan_repo_files(root: str, files: Sequence[str],
                     patterns: Sequence["re.Pattern[str]"]) -> Tuple[int, List[Dict[str, Any]], List[str]]:
    """(files read, hits, files that exist but cannot be read)."""
    count = 0
    hits: List[Dict[str, Any]] = []
    unreadable: List[str] = []
    cache: Dict[str, bool] = {}
    for rel in files:
        path = os.path.join(root, *rel.split("/"))
        try:
            data = _read_or_none(path)
        except OSError:
            unreadable.append(rel)
            continue
        if data is None:
            continue
        count += 1
        hits.extend(_scan_bytes(rel, data, patterns, path, cache))
    return count, hits, unreadable


def unreadable_files(paths: Sequence[str]) -> List[str]:
    """Files and folders under ``paths`` (walked as ``secrets.scan_paths`` walks them, ``.git`` skipped) that
    cannot be read: a scan would skip them silently. Only regular files are opened (a pipe would block), and a
    link to nothing holds no bytes, so it is not counted."""
    bad: List[str] = []

    def note(exc: OSError) -> None:
        bad.append(exc.filename if isinstance(exc.filename, str) else str(exc))

    for path in paths:
        if os.path.isdir(path):
            names: List[str] = []
            for folder, dirs, files in os.walk(path, onerror=note):
                dirs[:] = sorted(d for d in dirs if d != ".git")
                names.extend(os.path.join(folder, name) for name in sorted(files) if name != ".git")
        else:
            names = [path]
        for name in names:
            try:
                if not stat.S_ISREG(os.stat(name).st_mode):
                    continue
                with open(name, "rb"):
                    pass
            except FileNotFoundError:
                if not os.path.islink(name):
                    bad.append(name)
            except OSError:
                bad.append(name)
    return bad


def _scan_paths(paths: Sequence[str], patterns: Sequence["re.Pattern[str]"],
                relative_to: Optional[str] = None) -> Tuple[int, List[Dict[str, Any]], List[str]]:
    """(files read, hits, files and folders that cannot be read) for the files under ``paths``."""
    def shown(name: str) -> str:
        if relative_to and store.inside(relative_to, name):
            return os.path.relpath(name, relative_to).replace(os.sep, "/")
        return os.path.normpath(name)

    bad = unreadable_files(paths)
    count, found = secrets.scan_paths(paths)  # counts every file it walked, read or not
    count -= sum(1 for name in bad if not os.path.isdir(name))
    hits = [_hit(shown(name), kind, prefix) for name, kind, prefix in found]
    for name, line, prefix in secrets.scan_denylist(paths, patterns):
        hits.append(_hit(shown(name), "denylist", prefix, line))
    return count, hits, [shown(name) for name in bad]


def _sorted_hits(hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(hits, key=lambda h: (str(h["path"]), h["line"] or 0, str(h["kind"]), str(h["prefix"])))


def scan(repo_or_paths: Any, denylist: Any = None) -> List[Dict[str, Any]]:
    """Secret and denylist hits ``{path, kind, prefix, line}``; ``prefix`` is at most 6 characters and the match
    itself is never returned. A ``Repo`` is scanned as a release would (the files its commit could carry); a list
    of paths is walked whole (``.git`` skipped). ``denylist`` is a denylist file path, or compiled patterns.
    Raises ``DataError`` when a file or folder cannot be read, since the result would then be incomplete."""
    if isinstance(repo_or_paths, store.Repo):
        patterns = denylist if isinstance(denylist, list) else load_denylist(denylist, [repo_or_paths.root])
        _count, hits, bad = _scan_repo_files(repo_or_paths.root, release_files(repo_or_paths), patterns)
    else:
        paths = [repo_or_paths] if isinstance(repo_or_paths, str) else list(repo_or_paths)
        patterns = denylist if isinstance(denylist, list) else load_denylist(denylist, paths)
        _count, hits, bad = _scan_paths(paths, patterns)
    if bad:
        raise DataError(_unreadable_text(bad), unreadable=bad)
    return _sorted_hits(hits)


def _unreadable_text(bad: Sequence[str]) -> str:
    more = " +%d more" % (len(bad) - 5) if len(bad) > 5 else ""
    return "scan: cannot read %d file(s) or folder(s), so the scan is incomplete: %s%s" % (
        len(bad), ", ".join(bad[:5]), more)


def hit_line(hit: Dict[str, Any]) -> str:
    """``path: kind (starts "XXXXXX")``, or ``path:line: denylist (term starts "XXXXXX")``; a hit with no prefix
    (a sanitizer credential kind) is ``path: kind``."""
    where = hit["path"] if not hit.get("line") else "%s:%d" % (hit["path"], hit["line"])
    if not hit.get("prefix"):
        return "%s: %s" % (where, hit["kind"])
    what = "term starts" if hit["kind"] == "denylist" else "starts"
    return "%s: %s (%s %s)" % (where, hit["kind"], what, json.dumps(hit["prefix"], ensure_ascii=False))


# VERSIONS.md and MANIFEST.json ---------------------------------------------------------------------------------
def versions_row(tag: str, date: str, meta: Dict[str, Any], notes: str) -> str:
    counts = meta.get("counts") or {}
    rich = meta.get("richness") or {}
    richness = "%s %s" % (rich.get("richness"), rich.get("band")) if rich.get("richness") is not None else "n/a"
    cell = util.normalize_ws(notes).replace("|", "\\|")
    return "| %s | %s | %s | %d | %d | %s | %s |" % (
        tag, date, str(meta.get("data_hash") or "")[:12], counts.get("nodes", 0), counts.get("edges", 0), richness,
        cell)


def versions_text(existing: Optional[str], tag: str, row: str) -> str:
    """``VERSIONS.md`` with the ``tag`` row added or replaced; rows sorted by version number."""
    rows: Dict[str, str] = {}
    for line in (existing or "").splitlines():
        m = ROW_RE.match(line)
        if m:
            rows[m.group(1)] = line
    rows[tag] = row
    ordered = sorted(rows.items(), key=lambda kv: int(kv[0][1:]))
    return "\n".join(list(VERSIONS_HEAD) + [line for _t, line in ordered]) + "\n"


def manifest_files(repo: store.Repo, state: Optional[Dict[str, Any]], committing: bool,
                   outputs: Sequence[str]) -> Dict[str, str]:
    """``{path: sha256}`` for every file the release commit holds except ``MANIFEST.json``. When committing, that
    is the files of ``HEAD`` plus the outputs, a dirty file read as committed; otherwise every file ``git add -A``
    would commit (or, outside git, every file)."""
    root = repo.root
    present = [o for o in outputs if os.path.isfile(repo.path(o))]
    if not committing or state is None:
        files = set(release_files(repo, state)) | set(present)
        return {rel: store.file_sha256(repo.path(rel)) for rel in sorted(files - {MANIFEST})
                if os.path.isfile(repo.path(rel))}
    head = state.get("head")
    in_head = set(_z_list(_git_raw(root, "ls-tree", "-r", "-z", "--name-only", head))) if head else set()
    everything, _others = dirty_paths(repo, state)
    dirty = set(everything)
    out: Dict[str, str] = {}
    from_head: List[str] = []
    for rel in sorted((in_head | set(present)) - {MANIFEST}):
        if rel in present or (rel not in dirty and os.path.isfile(repo.path(rel))):
            out[rel] = store.file_sha256(repo.path(rel))
        else:
            from_head.append(rel)
    if from_head and head:
        prefix = state.get("prefix") or ""
        blobs = gitutil.read_files(root, head, [prefix + rel for rel in from_head])
        for rel in from_head:
            data = blobs.get(prefix + rel)
            if data is not None:
                out[rel] = util.sha256_hex(data)
    return dict(sorted(out.items()))


def build_manifest(repo: store.Repo, tag: str, meta: Dict[str, Any], files: Dict[str, str],
                   checks: Dict[str, str]) -> Dict[str, Any]:
    """``MANIFEST.json`` (C.19) from the release export's ``meta``."""
    manifest = {
        "format": FORMAT,
        "kit": __version__,
        "name": meta.get("name"),
        "ns": meta.get("ns"),
        "version": tag,
        "created": util.now_iso(),
        "data_hash": meta.get("data_hash"),
        "last_change": meta.get("last_change"),
        "files": dict(sorted(files.items())),
        "packs": {name: block.get("sha256") for name, block in sorted((meta.get("packs") or {}).items())},
        "imports": meta.get("imports") or [],
        "counts": {k: v for k, v in sorted((meta.get("counts") or {}).items())},
        "richness": meta.get("richness"),
        "checks": dict(checks),
    }
    errors = records.check(manifest, "manifest")
    if errors:
        raise DataError("MANIFEST.json fails its schema: %s" % "; ".join(errors[:3]), problems=errors[:20])
    return manifest


# the plan ------------------------------------------------------------------------------------------------------
def _mode(write: bool, commit: bool, push: bool) -> str:
    return "push" if push else "commit" if commit else "write" if write else "dry run"


def plan(repo: store.Repo, notes: Optional[str] = None, *, denylist: Optional[str] = None, write: bool = False,
         commit: bool = False, push: bool = False, allow_dirty: bool = False) -> Dict[str, Any]:
    """The dry-run ladder: validate, scan, paths, the next tag and the checks the asked-for steps need. Writes
    nothing. ``exit_code`` is 2 on scan hits, else 1 on any failure, else 0."""
    commit = commit or push
    write = write or commit
    root = repo.root
    notes_text = util.normalize_ws(notes or "")
    failures: List[str] = []
    notes_out: List[str] = []
    state = git_state(root)

    report = validate_mod.validate(repo)
    validate_block = {"ok": report.ok, "problems": len(report.problems), "warnings": len(report.warnings),
                      "lines": [p.text() for p in (report.problems or report.warnings)[:SHOW_LINES]]}
    if not report.ok:
        failures.append("validate found %d problem(s); run onto validate and fix them" % len(report.problems))

    patterns: List["re.Pattern[str]"] = []
    try:
        patterns = load_denylist(denylist, [root])
    except DataError as exc:
        failures.append(exc.message)
    files = release_files(repo, state)
    count, hits, unreadable = _scan_repo_files(root, files, patterns)
    note_hits = scan_text("--notes", notes_text, patterns) if notes_text else []
    # what a push publishes besides the files: the names, emails and messages of the commits not yet on origin.
    # Only --push publishes them, so only --push is refused; a plan notes them when the topic has an origin.
    has_origin = bool(state is not None and gitutil.git(root, "remote", "get-url", "origin"))
    commit_hits = unpushed_commit_hits(root, patterns) if state is not None and state.get("head") and (
        push or has_origin) else []
    if commit_hits and push:
        failures.append("%d denylisted term(s) in the author, committer or message of commits not yet on origin "
                        "(%s); rewrite those commits (for example git commit --amend --reset-author, or squash the "
                        "history) before a push" % (len(commit_hits), ", ".join(
                            h["path"] for h in commit_hits[:3])))
    elif commit_hits:
        notes_out.append("%d denylisted term(s) in the author, committer or message of commits not yet on origin "
                         "(%s); --push will refuse until those commits are rewritten"
                         % (len(commit_hits), ", ".join(h["path"] for h in commit_hits[:3])))
        commit_hits = []
    hits = _sorted_hits(hits + note_hits + commit_hits)
    notes_check: Dict[str, Any] = {"refused": [], "redacted": [], "message": None}
    if notes_text and not note_hits:
        cleaned, notes_check = clean_notes(repo, notes_text)
        if cleaned is None:
            failures.append(notes_check["message"])
        else:
            if notes_check["redacted"]:
                notes_out.append("--notes: redacted %s as policy.personal says; the release carries the redacted "
                                 "text" % ", ".join(notes_check["redacted"]))
            if notes_check.get("local"):
                notes_out.append("--notes: a local-only record is named; the release carries %s in its place"
                                 % build.LOCAL_MARK)
            notes_text = cleaned
    notes_secret = any(kind not in sanitize.PERSONAL_KINDS for kind in notes_check["refused"])
    scan_block = {"files": count, "hits": hits, "denylist": bool(patterns), "unreadable": unreadable}
    if unreadable:
        failures.append("cannot read %d file(s) of the commit set, so the scan is incomplete: %s"
                        % (len(unreadable), ", ".join(unreadable[:5])))

    junk = sorted(f for f in files if store.is_junk(root, f))
    allow = [p.text() for p in store.path_problems(repo)]
    private = tracked_private(state)
    history_hits = unpushed_private_commits(root) if push and state is not None and state.get("head") else []
    # content onto erase removed that commits not yet on origin still hold: --push is refused, a plan notes it
    erased_hits: List[Dict[str, Any]] = []
    erased_unknown = False
    if state is not None and state.get("head") and (push or gitutil.git(root, "remote", "get-url", "origin")):
        found = unpushed_erased_commits(repo, state)
        erased_unknown, erased_hits = found is None, found or []
    paths_block = {"junk": junk, "problems": allow, "tracked": private, "history": history_hits,
                   "erased": [{"commit": h["commit"], "ids": h["ids"]} for h in erased_hits]}
    if junk:
        failures.append("%d junk file(s) in the commit set, for example %s; delete them or ignore them"
                        % (len(junk), ", ".join(junk[:3])))
    if allow:
        failures.append("%d path(s) outside the topic allow-list; see onto validate (P19)" % len(allow))
    if private:
        failures.append("git tracks %d .onto/ or inbox/ file(s), so the release would carry raw text: %s%s; run %s"
                        % (len(private), ", ".join(private[:5]),
                           " +%d more" % (len(private) - 5) if len(private) > 5 else "", untrack_advice(private)))
    if history_hits:
        failures.append("%d commit(s) not yet on origin add or change .onto/ or inbox/ files, for example %s; "
                        "--push would publish that text in the branch history, so rewrite those commits first"
                        % (len(history_hits), ", ".join(history_hits[:3])))
    if erased_hits and push:
        failures.append(erased_history_text(erased_hits, root, (state or {}).get("branch"), (state or {}).get("head")))
    elif erased_hits:
        ids = sorted({i for h in erased_hits for i in h["ids"]})
        notes_out.append("%d commit(s) not yet on origin hold content that onto erase removed since (%s%s), for "
                         "example %s; --push will refuse until they are squashed into one"
                         % (len(erased_hits), ", ".join(ids[:5]), " +%d more" % (len(ids) - 5) if len(ids) > 5
                            else "", ", ".join(h["commit"] for h in erased_hits[:3])))
    if erased_unknown and push:
        failures.append("could not read the commits not yet on origin to check them for content onto erase "
                        "removed, so --push is refused; check that git log works in %s" % root)
    path_count = len(junk) + len(allow) + len(private) + len(history_hits) + (len(erased_hits) if push else 0)

    # tags are repo-wide (the topics of one repo share them); "previous" is this topic's own last release
    tag, _newest = next_tag((state or {}).get("tags") or [])
    previous = (store.load_release_manifest(repo) or {}).get("version")
    previous = previous if isinstance(previous, str) and TAG_RE.match(previous) else None
    stamp = store.version_stamp(repo)
    if stamp.get("matches_release"):
        notes_out.append("the data is unchanged since %s" % stamp.get("version"))

    dirty: List[str] = []
    if write and not notes_text:
        failures.append("--notes is required with --write, --commit and --push")
    if commit:
        if state is None:
            failures.append("%s is not in a git work tree; --commit needs git (run git init first)" % root)
        else:
            _all, dirty = dirty_paths(repo, state)
            if dirty and not allow_dirty:
                failures.append("--commit would leave out %d changed file(s) besides the release outputs, for "
                                "example %s; commit them first, or pass --allow-dirty"
                                % (len(dirty), ", ".join(dirty[:3])))
            elif dirty:
                notes_out.append("--allow-dirty: %d other changed file(s) stay uncommitted" % len(dirty))
    if push and state is not None:
        if not state.get("branch"):
            failures.append("HEAD is detached; --push needs a branch")
        if not gitutil.git(root, "remote", "get-url", "origin"):
            failures.append("no remote named origin; --push pushes to origin")

    checks = {
        "validate": "ok" if report.ok else "%d problem(s)" % len(report.problems),
        "scan": "ok" if not hits else "%d hit(s)" % len(hits),
        "paths": "ok" if not path_count else "%d problem(s)" % path_count,
        "tests": "skipped",
    }
    exit_code = 2 if hits or notes_secret else (1 if failures else 0)
    if hits:
        failures.append("secret-like strings or denylisted terms found; nothing may be released")
    if note_hits:
        shown_notes: Optional[str] = NOTES_WITHHELD % len(note_hits)
    elif notes_check["refused"]:
        shown_notes = NOTES_REFUSED % ", ".join(notes_check["refused"])
    else:
        shown_notes = notes_text or None
    return {
        "mode": _mode(write, commit, push),
        "tag": tag,
        "previous": previous,
        "git": state is not None,
        "branch": (state or {}).get("branch"),
        "head": ((state or {}).get("head") or "")[:7] or None,
        # notes that hold a secret, a denylisted term or data the sanitizer refuses are never echoed; the hits name
        # them by prefix only, the refusal by kind. Otherwise these are the notes after the sanitizer's redactions.
        "notes": shown_notes,
        "notes_check": {"refused": notes_check["refused"], "redacted": notes_check["redacted"]},
        "validate": validate_block,
        "scan": scan_block,
        "paths": paths_block,
        "dirty": dirty,
        "checks": checks,
        "failures": failures,
        "info": notes_out,
        "outputs": list(RELEASE_FILES) + (list(LOG_FILES) if commit else []),
        "written": [],
        "commit": None,
        "tagged": None,
        "pushed": False,
        "exit_code": exit_code,
    }


# running it ----------------------------------------------------------------------------------------------------
def _write_outputs(repo: store.Repo, report: Dict[str, Any], state: Optional[Dict[str, Any]], committing: bool,
                   patterns: Sequence["re.Pattern[str]"]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Steps 5 and the file part of 6. Returns (the manifest, hits in the written outputs)."""
    tag = report["tag"]
    notes = report["notes"] or ""
    built = build.write(repo, version=tag, validated=True)
    report["build"] = {k: built.get(k) for k in ("counts", "cards", "notes", "warnings")}
    meta = store.read_json(repo.path("build/export.json"))["meta"]
    old = _read_or_none(repo.path(VERSIONS))
    row = versions_row(tag, util.today(), meta, notes)
    store.write_text(repo.path(VERSIONS), versions_text(old.decode("utf-8") if old else None, tag, row))
    if committing:
        ledger.append_change(repo, "release", "user", [], "release %s: %s" % (tag, notes),
                             extra={"version": tag, "data_hash": meta.get("data_hash")})
        point = mutate.history_point(Ontology.load(repo), "release")
        point["label"] = tag
        history.append_point(repo, point)
    files = manifest_files(repo, state, committing, OUTPUTS)
    manifest = build_manifest(repo, tag, meta, files, report["checks"])
    store.write_json(repo.path(MANIFEST), manifest)
    written = [o for o in (RELEASE_FILES + (LOG_FILES if committing else ())) if os.path.isfile(repo.path(o))]
    _count, hits, bad = _scan_repo_files(repo.root, written, patterns)
    if bad:
        raise DataError(_unreadable_text(bad), unreadable=bad)
    report["written"] = written
    report["files_hashed"] = len(files)
    return manifest, _sorted_hits(hits)


def run(repo: store.Repo, write: bool = False, commit: bool = False, push: bool = False,
        notes: Optional[str] = None, allow_dirty: bool = False,
        denylist: Optional[str] = None) -> Tuple[Dict[str, Any], int]:
    """The whole ladder: the plan, then the writes, the commit and tag, and the push that were asked for. Returns
    (report, exit code)."""
    commit = commit or push
    write = write or commit
    report = plan(repo, notes, denylist=denylist, write=write, commit=commit, push=push, allow_dirty=allow_dirty)
    code = report["exit_code"]
    if code or not write:
        return report, code
    root = repo.root
    state = git_state(root)
    patterns = load_denylist(denylist, [root])
    tag = report["tag"]
    message = "%s: %s" % (tag, report["notes"])
    with store.write_lock(repo):
        saved = {rel: _read_or_none(repo.path(rel)) for rel in OUTPUTS}
        committed = False
        staged: List[str] = []
        index_before: Dict[str, List[str]] = {}
        try:
            _manifest, hits = _write_outputs(repo, report, state, commit, patterns)
            if hits:
                report["scan"]["hits"] = hits
                report["checks"]["scan"] = "%d hit(s)" % len(hits)
                report["failures"].append("the written outputs hold secret-like strings or denylisted terms; "
                                          "they were put back")
                for rel, data in saved.items():
                    _put_back(repo.path(rel), data)
                report["written"] = []
                report["exit_code"] = 2
                return report, 2
            if commit:
                index_before = _index_entries(root, report["written"])
                staged = list(report["written"])
                # -f: the outputs are committed by contract, even when a local ignore rule covers build/
                _git_write(root, "add", "-f", "--", *staged)
                _git_write(root, "commit", "-q", "--only", "-m", message, "--", *staged)
                committed = True
                report["commit"] = (gitutil.head(root) or "")[:7] or None
                try:
                    _git_write(root, "tag", "-a", tag, "-m", message)
                except GitError as exc:
                    raise GitError("committed %s but could not create the tag %s (%s); tag that commit by hand"
                                   % (report["commit"], tag, exc.message))
                report["tagged"] = tag
        except BaseException:
            if not committed:
                for rel, data in saved.items():
                    _put_back(repo.path(rel), data)
                if staged and state is not None:
                    # also on a branch with no commit yet, where "git reset" has no HEAD to reset to
                    _restore_index(root, state.get("prefix") or "", staged, index_before)
            raise
    if push:
        branch = report["branch"]
        try:
            _git_write(root, "push", "origin", "refs/heads/%s" % branch)
            _git_write(root, "push", "origin", "refs/tags/%s" % tag)
            report["pushed"] = True
        except GitError as exc:
            report["failures"].append("committed and tagged %s, but the push failed: %s" % (tag, exc.message))
            report["exit_code"] = 1
            return report, 1
    return report, 0


# the commands --------------------------------------------------------------------------------------------------
def cmd_release(ctx: Context, args: Dict[str, Any]) -> Dict[str, Any]:
    deny = denylist_path(args.get("denylist"), ctx.env)
    report, code = run(ctx.repo, write=bool(args.get("write")), commit=bool(args.get("commit")),
                       push=bool(args.get("push")), notes=args.get("notes"), allow_dirty=bool(args.get("allow_dirty")),
                       denylist=deny)
    return {"plan": report, "tag": report["tag"], "checks": report["checks"], "exit_code": code}


def _hit_lines(hits: Sequence[Dict[str, Any]], limit: int = 50) -> List[str]:
    lines = ["  " + hit_line(h) for h in hits[:limit]]
    if len(hits) > limit:
        lines.append("  +%d more" % (len(hits) - limit))
    return lines


def render_release(result: Dict[str, Any], mode: str, ctx: Context) -> List[str]:
    p = result.get("plan") or {}
    where = "on %s at %s" % (p.get("branch") or "a detached HEAD", p.get("head") or "no commit") if p.get("git") \
        else "outside git"
    lines = ["release %s (%s) %s%s" % (p.get("tag"), p.get("mode"), where,
                                       "; previous %s" % p["previous"] if p.get("previous") else "")]
    v = p.get("validate") or {}
    lines.append("validate: %s" % ("ok (%d warning(s))" % v.get("warnings", 0) if v.get("ok")
                                   else "%d problem(s)" % v.get("problems", 0)))
    if not v.get("ok") or mode == "text":
        lines += ["  " + line for line in v.get("lines") or []]
    s = p.get("scan") or {}
    hits = s.get("hits") or []
    lines.append("scan: %s (%d files%s)" % ("ok" if not hits else "%d hit(s)" % len(hits), s.get("files", 0),
                                           ", denylist on" if s.get("denylist") else ""))
    lines += _hit_lines(hits)
    lines += ["  %s: cannot read" % name for name in (s.get("unreadable") or [])[:SHOW_LINES]]
    paths = p.get("paths") or {}
    bad = list(paths.get("junk") or []) + list(paths.get("problems") or [])
    bad += ["%s: tracked by git (a runtime path; never released)" % rel for rel in paths.get("tracked") or []]
    bad += ["commit %s: adds or changes .onto/ or inbox/ files and is not on origin yet" % sha
            for sha in paths.get("history") or []]
    if p.get("mode") == "push":
        bad += ["commit %s: holds content onto erase removed since (%s) and is not on origin yet"
                % (hit["commit"], ", ".join(hit["ids"][:3]) + (" +%d more" % (len(hit["ids"]) - 3)
                                                                 if len(hit["ids"]) > 3 else ""))
                for hit in paths.get("erased") or []]
    lines.append("paths: %s" % ("ok" if not bad else "%d problem(s)" % len(bad)))
    lines += ["  " + b for b in bad[:SHOW_LINES]]
    if p.get("dirty"):
        lines.append("dirty besides the outputs: %s" % ", ".join(p["dirty"][:10]))
    for info in p.get("info") or []:
        lines.append("note: %s" % info)
    if p.get("failures"):
        lines.append("release FAILED:")
        lines += ["  - %s" % f for f in p["failures"]]
        return lines
    if p.get("written"):
        lines.append("wrote %s (%s files hashed in MANIFEST.json)" % (", ".join(p["written"]),
                                                                     p.get("files_hashed", 0)))
        for note in (p.get("build") or {}).get("notes") or []:
            lines.append("note: %s" % note)
    else:
        lines.append("would write: %s" % ", ".join(RELEASE_FILES))
        lines.append("then --commit: a release change and point, commit \"%s: %s\" and tag %s; --push: origin %s and %s"
                     % (p.get("tag"), p.get("notes") or "<notes>", p.get("tag"), p.get("branch") or "<branch>",
                        p.get("tag")))
    if p.get("commit"):
        lines.append("committed %s and tagged %s" % (p["commit"], p.get("tagged")))
    if p.get("pushed"):
        lines.append("pushed origin %s and %s" % (p.get("branch"), p.get("tagged")))
    if p.get("mode") == "dry run":
        lines.append("dry run ok: nothing was written")
    return lines


def cmd_scan(ctx: Context, args: Dict[str, Any]) -> Dict[str, Any]:
    given = [p for p in args.get("paths") or [] if p]
    base = ctx.cwd or os.getcwd()
    relative_to = None
    if given:
        paths = []
        for text in given:
            path = os.path.expanduser(text)
            if ctx.cwd and not os.path.isabs(path):
                path = os.path.join(ctx.cwd, path)
            paths.append(path)
    else:
        root = ctx.repo.root if ctx.has_repo() else base
        paths, relative_to = [root], root
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        raise DataError("scan: no such file or folder: %s" % ", ".join(missing))
    deny = denylist_path(args.get("denylist"), ctx.env)
    patterns = load_denylist(deny, [os.path.abspath(p) for p in paths])
    count, hits, unreadable = _scan_paths(paths, patterns, relative_to)
    hits = _sorted_hits(hits)
    # hits win (exit 2); a file that cannot be read is an IO error (exit 1): the scan could not see inside it
    code = 2 if hits else (1 if unreadable else 0)
    return {"files": count, "hits": hits, "unreadable": unreadable, "denylist": bool(patterns), "exit_code": code}


def render_scan(result: Dict[str, Any], mode: str, ctx: Context) -> List[str]:
    hits = result.get("hits") or []
    bad = result.get("unreadable") or []
    lines = [hit_line(h) for h in hits]
    lines += ["%s: cannot read" % name for name in bad[:SHOW_LINES]]
    if len(bad) > SHOW_LINES:
        lines.append("+%d more that cannot be read" % (len(bad) - SHOW_LINES))
    verdict = "%d hit(s)" % len(hits) if hits else ("no hits" if bad else "clean")
    if bad:
        verdict += ", %d unreadable: the scan is incomplete" % len(bad)
    lines.append("scan: %d file(s)%s, %s" % (result.get("files", 0), ", denylist on" if result.get("denylist") else "",
                                              verdict))
    return lines
