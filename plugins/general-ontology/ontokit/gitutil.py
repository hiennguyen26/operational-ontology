"""Safe, read-only git.

Every call runs with optional locks off (``GIT_OPTIONAL_LOCKS=0``), no terminal prompt and stdin closed, so
nothing here writes to a repo (not even the index refresh of ``git status``) and nothing waits for input. Refs
from users are checked against ``REF_RE`` (a ``vN`` tag or a commit id) before git sees them, so an argument such
as ``--upload-pack=x`` never reaches git. Nothing in this module fetches or pushes.

Many files are read with a single ``git cat-file --batch`` (``read_files``, ``ref_files``, ``blobs``).
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Dict, List, Optional, Sequence, Tuple

from .errors import GitError

GIT_TIMEOUT = 30
SHA_RE = re.compile(r"^[0-9a-f]{4,40}$")  # what git gets as a commit: never an option or a ref expression
REF_RE = re.compile(r"^(v[0-9]+|[0-9a-f]{7,40})$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def _env() -> Dict[str, str]:
    return dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")


def _run(root: str, args: Sequence[str], input_bytes: Optional[bytes] = None,
         timeout: float = GIT_TIMEOUT) -> Optional[subprocess.CompletedProcess]:
    """The finished process (bytes out), or None when git could not run or timed out."""
    try:
        return subprocess.run(
            ["git", "-C", root] + list(args),
            input=input_bytes,
            stdin=None if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_env(),
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def git(root: str, *args: str, timeout: float = GIT_TIMEOUT) -> str:
    """A read-only git command's stripped output, ``''`` on any failure."""
    proc = _run(root, args, timeout=timeout)
    if proc is None or proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", "replace").strip()


def git_ok(root: str, *args: str) -> Tuple[bool, str, str]:
    """(ok, stdout, stderr), both stripped."""
    proc = _run(root, args)
    if proc is None:
        return False, "", "git is not available or timed out"
    return (
        proc.returncode == 0,
        proc.stdout.decode("utf-8", "replace").strip(),
        proc.stderr.decode("utf-8", "replace").strip(),
    )


IDENTITY_FIX = ('run git config --global user.name "Your Name" and git config --global user.email '
                '"you@example.com"')


def identity_problem(root: str) -> Optional[str]:
    """None when git can name the author and committer of a commit made in ``root`` from a name and an email the
    user set (git config, or the ``GIT_AUTHOR_*`` and ``GIT_COMMITTER_*`` variables), else git's last line
    (``fatal: no email was given and auto-detection is disabled``). An identity git would guess from the account's
    full name and the machine's host name (``user.useConfigOnly`` is forced on here) is not one the user chose, and
    every commit, pushed later, would carry it."""
    for ident in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        ok, _out, err = git_ok(root, "-c", "user.useConfigOnly=true", "var", ident)
        if not ok:
            lines = [line for line in err.splitlines() if line.strip()]
            return lines[-1] if lines else "git has no user name and email"
    return None


def resolve_ref(root: str, ref: str) -> str:
    """The 40-hex commit of a ``vN`` tag or a commit id. Anything else (a branch, ``HEAD``, an option) is refused
    with ``GitError`` before git runs."""
    if not isinstance(ref, str) or not REF_RE.fullmatch(ref):
        raise GitError("%r is not a release tag (vN) or a commit id" % (ref,))
    target = "refs/tags/%s^{commit}" % ref if ref.startswith("v") else "%s^{commit}" % ref
    commit = git(root, "rev-parse", "--verify", "--quiet", target)
    if not COMMIT_RE.fullmatch(commit):
        raise GitError("%s is not a commit in %s" % (ref, root))
    return commit


def _parse_batch(data: bytes, count: int) -> List[Optional[bytes]]:
    """Split ``cat-file --batch`` output for ``count`` requests; a missing or ambiguous object is None."""
    pos, out = 0, []  # type: int, List[Optional[bytes]]
    for _ in range(count):
        end = data.index(b"\n", pos)
        header = data[pos:end]
        pos = end + 1
        if header.endswith((b" missing", b" ambiguous")):
            out.append(None)
            continue
        parts = header.split()
        size = int(parts[2])
        body = data[pos : pos + size]
        out.append(body if parts[1] == b"blob" else None)
        pos += size + 1
    return out


def _safe_path(path: str) -> bool:
    return (
        bool(path)
        and "\n" not in path
        and not path.startswith("/")
        and ".." not in path.split("/")
    )


def read_files(root: str, commit: str, paths: Sequence[str]) -> Dict[str, bytes]:
    """``{path: bytes}`` for the files at ``commit``, read in one ``git cat-file --batch``. Missing files and
    unsafe paths are left out."""
    if not isinstance(commit, str) or not SHA_RE.fullmatch(commit):
        raise GitError("%r is not a commit id" % (commit,))
    wanted = sorted({p for p in paths if _safe_path(p)})
    if not wanted:
        return {}
    spec = "".join("%s:%s\n" % (commit, p) for p in wanted).encode("utf-8")
    proc = _run(root, ["cat-file", "--batch"], spec)
    if proc is None or proc.returncode != 0:
        raise GitError("git cat-file failed in %s" % root)
    bodies = _parse_batch(proc.stdout, len(wanted))
    return {p: b for p, b in zip(wanted, bodies) if b is not None}


def blobs(root: str, oids: Sequence[str]) -> List[Optional[bytes]]:
    """The content of each object id (None when git lacks it), read in one ``git cat-file --batch``."""
    if not oids:
        return []
    if any(not SHA_RE.fullmatch(o or "") for o in oids):
        raise GitError("not an object id in %r" % (list(oids),))
    proc = _run(root, ["cat-file", "--batch"], "".join(o + "\n" for o in oids).encode("ascii"))
    if proc is None or proc.returncode != 0:
        raise GitError("git cat-file failed in %s" % root)
    return _parse_batch(proc.stdout, len(oids))


def ref_files(root: str, commit: str, prefix: str) -> List[Tuple[str, bytes]]:
    """(path, content) of every regular file under ``prefix`` at ``commit``, read with git plumbing."""
    if not isinstance(commit, str) or not SHA_RE.fullmatch(commit):
        raise GitError("%r is not a commit id" % (commit,))
    proc = _run(root, ["ls-tree", "-r", "-z", "--full-tree", commit, "--", prefix or "."])
    if proc is None or proc.returncode != 0:
        return []
    entries = []
    for item in proc.stdout.decode("utf-8", "replace").split("\0"):
        meta, _, path = item.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob" and parts[0] in ("100644", "100755"):
            if _safe_path(path):
                entries.append((path, parts[2]))
    bodies = blobs(root, [oid for _p, oid in entries])
    return [(path, body) for (path, _oid), body in zip(entries, bodies) if body is not None]


def commits_ahead(root: str, commit: str) -> Optional[int]:
    """How many commits ``HEAD`` has that ``commit`` does not; None when it cannot be counted."""
    if not isinstance(commit, str) or not SHA_RE.fullmatch(commit):
        return None
    count = git(root, "rev-list", "--count", "%s..HEAD" % commit)
    return int(count) if count.isdigit() else None


def head(root: str) -> Optional[str]:
    """The 40-hex commit of ``HEAD``, or None (no repo, no commits)."""
    commit = git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    return commit if COMMIT_RE.fullmatch(commit) else None


def branch(root: str) -> Optional[str]:
    """The checked-out branch name, or None on a detached HEAD."""
    name = git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    return name or None


def dirty_paths(root: str) -> List[str]:
    """Paths with uncommitted changes (staged, unstaged or untracked), from ``status --porcelain -z``. A rename or
    copy lists both the new and the original path."""
    proc = _run(root, ["status", "--porcelain", "-z", "--untracked-files=all"])
    if proc is None or proc.returncode != 0:
        return []
    fields = proc.stdout.decode("utf-8", "replace").split("\0")
    out = set()
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        out.add(path)
        if "R" in status or "C" in status:
            if i < len(fields) and fields[i]:
                out.add(fields[i])
            i += 1
    return sorted(out)


def commit_set(root: str) -> List[str]:
    """Every file ``git add -A`` would leave in the next commit: tracked and untracked, minus ignored."""
    proc = _run(root, ["ls-files", "--cached", "--others", "--exclude-standard", "-z"])
    if proc is None or proc.returncode != 0:
        return []
    names = {p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p}
    return sorted(p for p in names if os.path.isfile(os.path.join(root, *p.split("/"))))


def tags(root: str, pattern: str = "v*") -> List[str]:
    """Tag names matching ``pattern``."""
    out = git(root, "tag", "--list", pattern)
    return sorted(t for t in out.splitlines() if t)
