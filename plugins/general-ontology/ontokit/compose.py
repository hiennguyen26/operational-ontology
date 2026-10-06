"""Composition: import released topics under namespaces, keep their pins honest, and suggest bridges.

An import is read-only, pinned and vendored:

- ``add`` reads ``build/export.json`` and ``MANIFEST.json`` of a released topic at a tag or commit (read-only git,
  never a fetch; for a topic made with ``onto init --path`` both sit under the topic folder's prefix in the repo, and
  the topics of one repo share the ``vN`` tags, so a tag counts as a topic's release only when its own manifest
  names that version: ``release_tags``). A commit must be a release commit: between releases both files still hold
  the last release, so a later commit is refused with the release it carries, and a release commit is pinned under
  its ``vN`` tag. ``add`` checks the export against its manifest, refuses a newer export format ("upgrade the
  kit"), and pins it in ``imports/lock.json`` with the export vendored under ``imports/<ns>/export.json``. The
  parents the export bundles are pinned too, as ``via`` entries whose vendored file is the canonical bytes of the
  bundle object, so a composed topic can be imported without clone paths for its parents.
- ``update`` moves a direct pin to another tag or commit of the same ``from`` path; ``remove`` drops a direct pin
  and the parents only it bundled (with ``override`` and ``keep`` when the parents other imports bundle are left at
  two releases). Both show what moves first: the lock diff, the node diff (added, removed, changed) and the
  bridges that would dangle or point at archived nodes: local ones, and the ones an import brings (a composed
  topic's bridges, flagged ``inherited`` with their ``owner`` ns), which a pin switch can cut too.
- The lock is recomputed from the direct pins every time, so dedup and conflicts follow one rule per namespace:
  another topic ``name`` under the same ns is a collision (an error; a bundled parent keeps the ns its bundler's
  release gave it, so the refusal names the options that exist); the same name at the same commit is one
  entry (a direct pin wins over a bundled one); the same name at another commit is a pin conflict, refused unless
  ``override`` names an active decision and ``keep`` names the export sha to keep. The choice is recorded in the
  entry's ``override`` block, and a later recompute keeps it while it still covers every sha. The refusal says
  which bridges keeping each side would cut.
- ``pin_status`` says per pin whether the vendored file still matches the lock and whether a newer release exists.
- ``suggest`` pairs the nodes of two namespaces (``entities.cross``: names, aliases, the local ``kind_map``, shared
  terms and sources) and saves a pending proposal of ``same_as`` bridges whose provenance cites the pinned exports.
- Open proposals follow the pins (``_sync_proposals``): every import write, ``suggest``, and an ``update`` that
  changes nothing re-cite ``imp:<ns>@<commit>`` provenance of another pin to the current one when the cited id is
  still active there, and mark an op that no longer applies ``annot.stale`` with a ``stale`` warning. A stale pair
  is neither offered nor, once rejected, a rejected identity; ``status`` lists both kinds.
- ``bundle`` gives the ``bundled`` object of this topic's own export: every lock entry, flattened.

Writes hold the write lock and are all or nothing: the vendored files, the lock, the re-cited proposals, the
``import`` change line and the history point go together. A write intent names them first (``store.begin_write``),
so a process killed half way is rolled back by the next writer, and a failure in process puts every file back. A
change that would add P15 problems is refused, and so is one that would write or remove a file through a symbolic
link under ``imports/`` (P19).

Refusals and notes name their follow-up calls in the caller's words (``_Hints``): MCP tools and arguments over MCP,
``onto`` commands and ``--flags`` on the CLI; a pin conflict names the decision to record and this call again with
``override`` and ``keep``.
"""

from __future__ import annotations

import copy
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import FORMAT, entities, gitutil, history, ids, ledger, lockfile, mutate, pipeline, records, render, store
from . import needs, util, validate
from .errors import Conflict, DataError, GitError, NotFound, Problem, Refused, UsageError
from .graph import Ontology
from .graph import clear_cache as clear_graph_cache

EXPORT_FILE = "build/export.json"
MANIFEST_FILE = "MANIFEST.json"
ACTIONS = ("add", "update", "remove", "status", "suggest")
MAX_SUGGEST = 50  # same_as candidates per proposal; the rest are counted
SHOW = 8  # ids per list in compact output
SHOW_TEXT = 50
MAX_ROUNDS = 10
SHA_PREFIX_MIN = 7
_TAG_RE = re.compile(r"^v([0-9]+)$")
_SHA_RE = re.compile(r"^[0-9a-f]+$")


# small helpers -------------------------------------------------------------------------------------------------
def _entries(lock: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {str(e.get("ns")): e for e in lockfile.entries(lock) if isinstance(e.get("ns"), str)}


def _short(sha: Any, n: int = 12) -> str:
    return str(sha or "")[:n]


def _pin_text(entry: Optional[Dict[str, Any]]) -> str:
    """``v1 a1b2c3d`` (plus ``via g2t``) for a lock entry or a lock diff side."""
    if not entry:
        return "-"
    text = "%s %s" % (entry.get("ref") or "-", entry.get("commit7") or _short(entry.get("commit"), 7))
    if entry.get("via"):
        text += " via %s" % entry["via"]
    return text


def _tag_number(tag: str) -> Optional[int]:
    m = _TAG_RE.match(tag or "")
    return int(m.group(1)) if m else None


class _Hints(object):
    """How this call's messages write a follow-up call: MCP tools and arguments over MCP, ``onto`` commands and
    ``--flags`` on the CLI (``render.call``). ``again`` holds the arguments that run this import call again."""

    def __init__(self, mcp: bool = False, again: Optional[Dict[str, Any]] = None) -> None:
        self.mcp = bool(mcp)
        self.again = dict(again or {})

    def call(self, tool: str, **args: Any) -> str:
        """A follow-up call. ``from`` is never a CLI positional of ``onto import`` (only the action is): where
        ``render.call`` would write it as one (only positionals before it), it is written as ``--from`` right after
        the action."""
        given = args.get("from")
        keys = [k for k, v in args.items() if v is not None]
        if self.mcp or given is None or any(k not in render.POSITIONAL or isinstance(args[k], bool)
                                            for k in keys[:keys.index("from")]):
            return render.call(self.mcp, tool, **args)
        rest = {k: v for k, v in args.items() if k != "from"}
        head = render.call(False, tool, **({"action": rest.pop("action")} if "action" in rest else {}))
        tail = render.call(False, tool, **rest)[len(render.call(False, tool)):]
        return "%s --from %s%s" % (head, render.quote(str(given)), tail)

    def rerun(self, **more: Any) -> str:
        """This import call again with ``more`` (an override and keep), which writes at once: ``confirm=true`` over
        MCP."""
        args = dict(self.again)
        args.update(more)
        args["confirm"] = True if self.mcp else None
        return self.call("import", **args)


_CLI = _Hints()


def topic_prefix(root: str) -> str:
    """The topic folder's path inside its git work tree, ending in ``/`` (``""`` at the top), as
    ``release.git_state`` computes it: a topic made with ``onto init --path`` releases ``<prefix>build/export.json``
    and ``<prefix>MANIFEST.json``."""
    return gitutil.git(root, "rev-parse", "--show-prefix")


def _manifest_at(root: str, commit: str, prefix: str) -> Optional[Dict[str, Any]]:
    """The topic's ``MANIFEST.json`` at ``commit`` as a dict, or None when it is absent or unreadable."""
    try:
        data = gitutil.read_files(root, commit, [prefix + MANIFEST_FILE]).get(prefix + MANIFEST_FILE)
    except GitError:
        return None
    try:
        value = util.loads_record(data.decode("utf-8")) if data is not None else None
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _other_release(manifest: Dict[str, Any], tag: str) -> Optional[str]:
    """The version the topic's manifest names when the tag ``vN`` is not its release (another topic of the same
    git repo made that tag: every topic in one repo shares the ``vN`` sequence), else None."""
    version = manifest.get("version")
    return version if isinstance(version, str) and _tag_number(tag) is not None and version != tag else None


def release_tags(root: str) -> List[Tuple[str, str]]:
    """``(tag, commit)`` of each ``vN`` tag that releases the topic at ``root``, newest first. A tag counts when the
    topic's own ``MANIFEST.json`` at that commit names it as its version: the topics of one git repo (``onto init
    --path``) share the ``vN`` sequence, so the tags of the other topics are left out."""
    prefix = topic_prefix(root)
    return [found for found in (_newest_of(root, prefix, [tag]) for tag in _tags_newest_first(root)) if found]


def _tags_newest_first(root: str) -> List[str]:
    return [t for _n, t in sorted(((_tag_number(t), t) for t in gitutil.tags(root) if _tag_number(t) is not None),
                                  reverse=True)]


def _newest_of(root: str, prefix: str, tags_: Sequence[str]) -> Optional[Tuple[str, str]]:
    """The first of ``tags_`` (newest first) that releases the topic at ``prefix``, as ``(tag, commit)``."""
    for tag in tags_:
        try:
            commit = gitutil.resolve_ref(root, tag)
        except GitError:
            continue
        manifest = _manifest_at(root, commit, prefix)
        if manifest is not None and _other_release(manifest, tag) is None:
            return tag, commit
    return None


def newest_release(root: str) -> Optional[Tuple[str, str]]:
    """``(tag, commit)`` of the newest ``vN`` tag that releases the topic at ``root`` (see ``release_tags``), or
    None."""
    return _newest_of(root, topic_prefix(root), _tags_newest_first(root))


def newest_tag(root: str) -> Optional[str]:
    """The newest release tag ``vN`` of the topic at ``root`` (see ``release_tags``), or None."""
    found = newest_release(root)
    return found[0] if found else None


def _released_folders(root: str, commit: str) -> List[str]:
    """Folders under ``root`` (relative to it) that hold a released topic at ``commit``: both ``MANIFEST.json`` and
    ``build/export.json``. Used to say where the release is when ``from`` names the repo, not the topic."""
    ok, out, _err = gitutil.git_ok(root, "ls-tree", "-r", "-z", "--name-only", commit, "--", ".")
    if not ok:
        return []
    names = {p for p in out.split("\0") if p}  # relative to root, as the pathspec is
    return sorted(name[: -len("/" + EXPORT_FILE)] for name in names
                  if name.endswith("/" + EXPORT_FILE) and name[: -len(EXPORT_FILE)] + MANIFEST_FILE in names)


def _from_path(repo: store.Repo, from_: str) -> Tuple[str, str]:
    """(real path, the path stored in the lock) for a ``from`` argument. A relative path is taken from the topic
    root, and the lock stores it relative to the root with ``/`` (no machine-specific prefix)."""
    if not isinstance(from_, str) or not from_.strip():
        raise UsageError("import needs from: the path of a released topic repo")
    given = os.path.expanduser(from_.strip())
    full = given if os.path.isabs(given) else os.path.join(repo.root, given)
    full = os.path.normpath(os.path.abspath(full))
    if not os.path.isdir(full):
        raise NotFound("no topic repo at %s (a relative from is read from the topic root)" % from_,
                       searched=from_)
    real = store.guard_input_path(full, repo, allow_any=True)
    if os.path.realpath(real) == os.path.realpath(repo.root):
        raise Refused("%s is this topic; a topic cannot import itself" % from_)
    stored = os.path.relpath(full, repo.root).replace(os.sep, "/")
    return real, stored


def _stored_root(repo: store.Repo, stored: Optional[str]) -> Optional[str]:
    """The folder a lock entry's ``from`` names, or None when it is absent here."""
    if not stored:
        return None
    full = stored if os.path.isabs(stored) else os.path.join(repo.root, *stored.split("/"))
    full = os.path.normpath(full)
    return full if os.path.isdir(full) else None


def _parse_json(data: bytes, what: str) -> Any:
    try:
        return util.loads_record(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise Refused("%s is not valid JSON (%s)" % (what, exc))


def _check_format(value: Any, what: str) -> None:
    if isinstance(value, int) and not isinstance(value, bool) and value > FORMAT:
        raise Refused("%s uses export format %d; this kit reads format %d: upgrade the kit" % (what, value, FORMAT))


def _packs_of(export: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for name, block in sorted(((export.get("meta") or {}).get("packs") or {}).items()):
        if isinstance(block, dict) and isinstance(block.get("sha256"), str):
            out[name] = block["sha256"]
    return out


# reading a release ---------------------------------------------------------------------------------------------
def read_release(root: str, ref: str, label: str = "", hints: Optional[_Hints] = None) -> Dict[str, Any]:
    """The release of the topic repo at ``root`` at ``ref`` (a ``vN`` tag or a commit):
    ``{ref, commit, data, sha, export, manifest_sha}``. The export must be listed with its sha256 in the
    ``MANIFEST.json`` of the same commit, be canonical, pass the export schema and use a format this kit reads;
    every bundle must match its sha. A commit must be a release commit (``_commit_release``); ``ref`` is then its
    ``vN`` tag when that tag releases the topic there. Anything else is ``Refused``; nothing is written. ``hints``
    words the follow-up calls the refusals name."""
    hints = hints or _CLI
    what = label or root
    prefix = topic_prefix(root)  # a topic made with onto init --path keeps its files in a folder of the repo
    commit = gitutil.resolve_ref(root, ref)
    files = gitutil.read_files(root, commit, [prefix + EXPORT_FILE, prefix + MANIFEST_FILE])
    data = files.get(prefix + EXPORT_FILE)
    if data is None:
        inside = _released_folders(root, commit)
        if inside:  # from names the repo that holds topic folders (onto init --path), not a topic
            calls = [hints.call("import", action="add", ns=hints.again.get("ns"),
                                **{"from": "%s/%s" % (what.rstrip("/"), f)}) for f in inside[:3]]
            raise Refused("%s has no %s at %s: it holds topic folders. At %s it has the release%s of %s; import "
                          "one with %s" % (what, EXPORT_FILE, ref, ref, "s" if len(inside) > 1 else "",
                                           _and(inside[:3]), " or ".join(calls)))
        raise Refused("%s has no %s at %s; release it first (onto release --write --commit)" % (
            what, EXPORT_FILE, ref))
    mdata = files.get(prefix + MANIFEST_FILE)
    if mdata is None:
        raise Refused("%s has no %s at %s; only released topics can be imported" % (what, MANIFEST_FILE, ref))
    manifest = _parse_json(mdata, "%s at %s" % (MANIFEST_FILE, ref))
    if not isinstance(manifest, dict):
        raise Refused("%s at %s is not a JSON object" % (MANIFEST_FILE, ref))
    other = _other_release(manifest, ref)
    if other is not None:
        own = newest_tag(root)
        raise Refused("%s at %s is not a release of this topic: another topic of the same git repo made the tag %s "
                      "(the topics of one repo share the vN sequence), and this topic's %s there is still %s. Import "
                      "a tag of its own (%s), or leave out ref for its newest release" % (
                          what, ref, ref, MANIFEST_FILE, other, own or "it has none yet"),
                      version=other, newest=own)
    _check_format(manifest.get("format"), "%s %s" % (what, ref))
    sha = util.sha256_hex(data)
    listed = (manifest.get("files") or {}).get(EXPORT_FILE) if isinstance(manifest.get("files"), dict) else None
    if listed != sha:
        raise Refused("%s at %s does not match its %s (sha256 %s, the manifest lists %s); the release was edited "
                      "after it was made" % (EXPORT_FILE, ref, MANIFEST_FILE, _short(sha), _short(listed) or "none"))
    export = _parse_json(data, "%s at %s" % (EXPORT_FILE, ref))
    if not isinstance(export, dict) or not isinstance(export.get("meta"), dict):
        raise Refused("%s at %s is not an export (no meta)" % (EXPORT_FILE, ref))
    _check_format(export["meta"].get("format"), "%s %s" % (what, ref))
    for bns, bundle in sorted((export.get("bundled") or {}).items()):
        if isinstance(bundle, dict) and isinstance(bundle.get("export"), dict):
            _check_format((bundle["export"].get("meta") or {}).get("format"), "%s %s (bundled %s)" % (what, ref, bns))
    if data != util.canonical_bytes(export):
        raise Refused("%s at %s is not in canonical form; rebuild it with onto build" % (EXPORT_FILE, ref))
    errors = records.check(export, "export")
    if errors:
        raise Refused("%s at %s fails the export schema: %s" % (EXPORT_FILE, ref, "; ".join(errors[:3])),
                      problems=errors[:20])
    for key in ("name", "ns"):
        if manifest.get(key) not in (None, export["meta"].get(key)):
            raise Refused("%s at %s names %s %r but its export says %r" % (MANIFEST_FILE, ref, key, manifest.get(key),
                                                                          export["meta"].get(key)))
    for bns, bundle in sorted(export["bundled"].items()):
        if lockfile.bundle_sha(bundle["export"]) != bundle["sha256"]:
            raise Refused("%s at %s: bundled %s does not match its sha256; the release was edited" % (
                EXPORT_FILE, ref, bns))
    if _tag_number(ref) is None:  # a commit: between releases its export is still the last release
        ref = _commit_release(root, prefix, ref, commit, manifest, what, hints)
    return {"ref": ref, "commit": commit, "data": data, "sha": sha, "export": export,
            "manifest_sha": util.sha256_hex(mdata)}


def _manifest_commit(root: str, prefix: str, commit: str) -> Optional[str]:
    """The newest commit at or before ``commit`` that wrote the topic's ``MANIFEST.json``: the commit of the
    release ``commit`` carries. None when git cannot say."""
    ok, out, _err = gitutil.git_ok(root, "log", "-1", "--format=%H", commit, "--", ":(top)" + prefix + MANIFEST_FILE)
    return out if ok and gitutil.COMMIT_RE.fullmatch(out) else None


def _commit_release(root: str, prefix: str, ref: str, commit: str, manifest: Dict[str, Any], what: str,
                    hints: _Hints) -> str:
    """The ref a commit ``ref`` is pinned under. A topic's ``build/export.json`` and ``MANIFEST.json`` keep the last
    release until the next one, so a commit after a release (not the commit that wrote its manifest) holds that
    older release, not the topic as it is there: refused, naming the release to import instead. The release commit
    itself is recorded under its ``vN`` tag when that tag releases this topic there, else under the commit."""
    version = manifest.get("version") if isinstance(manifest.get("version"), str) else None
    tagged = _newest_of(root, prefix, [version]) if version and _tag_number(version) is not None else None
    made = _manifest_commit(root, prefix, commit) or commit
    if made != commit:
        use = tagged[0] if tagged and tagged[1] == made else made[:12]
        raise Refused("%s at %s is not a release: its %s there is still release %s, made at %s, so its export is "
                      "that release, not the topic as it is at %s. Import %s (%s), or release the topic first (onto "
                      "release --write --commit) and import that release" % (
                          what, ref, MANIFEST_FILE, version or "-", made[:7], ref, use,
                          hints.call("import", **dict(hints.again, ref=use))),
                      version=version, release_commit=made)
    return tagged[0] if tagged and tagged[1] == commit else ref


# candidates ----------------------------------------------------------------------------------------------------
class _Cand(object):
    """One way a namespace could be pinned: a direct import (from the lock or a release) or a parent bundled by a
    direct import."""

    def __init__(self, entry: Dict[str, Any], data: Optional[bytes], export: Optional[Dict[str, Any]],
                 fresh: bool = False, tampered: bool = False) -> None:
        self.entry = entry
        self.data = data
        self.export = export
        self.fresh = fresh  # named by this call (add or update)
        self.tampered = tampered

    @property
    def ns(self) -> str:
        return str(self.entry["ns"])

    @property
    def via(self) -> Optional[str]:
        return self.entry.get("via")

    @property
    def sha(self) -> str:
        return str(self.entry.get("export_sha256") or "")

    @property
    def commit(self) -> str:
        return str(self.entry.get("commit") or "")

    @property
    def name(self) -> str:
        return str(self.entry.get("name") or "")

    def describe(self) -> Dict[str, Any]:
        return {"ns": self.ns, "name": self.name, "ref": self.entry.get("ref"), "commit7": _short(self.commit, 7),
                "sha256": self.sha, "via": self.via, "from": self.entry.get("from")}


def _direct_from_release(ns: str, release: Dict[str, Any], stored_from: str) -> _Cand:
    export = release["export"]
    meta = export["meta"]
    entry = {
        "ns": ns, "name": meta["name"], "from": stored_from, "ref": release["ref"], "commit": release["commit"],
        "export_sha256": release["sha"], "manifest_sha256": release["manifest_sha"], "kit": meta["kit"],
        "format": meta["format"], "packs": _packs_of(export), "nodes": len(export.get("nodes") or []),
        "edges": len(export.get("edges") or []), "via": None, "override": None, "locked_at": util.today(),
    }
    return _Cand(entry, release["data"], export, fresh=True)


def _existing_directs(repo: store.Repo, lock: Dict[str, Any], skip: Iterable[str] = ()) -> List[_Cand]:
    """The direct pins of the lock as candidates, their vendored files read and checked against the lock."""
    left_out = set(skip)
    out: List[_Cand] = []
    for ns, e in sorted(_entries(lock).items()):
        if e.get("via") or ns in left_out:
            continue
        path = lockfile.export_path(repo, ns)
        data: Optional[bytes] = None
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                data = fh.read()
        tampered = data is None or util.sha256_hex(data) != e.get("export_sha256")
        export = None
        if not tampered:
            try:
                export = util.loads_record(data.decode("utf-8"))  # type: ignore[union-attr]
            except (UnicodeDecodeError, ValueError):
                tampered = True
        out.append(_Cand(dict(e), data, export, tampered=tampered))
    return out


def _vias(direct: _Cand) -> List[_Cand]:
    """The parents a direct candidate bundles, as ``via`` candidates."""
    export = direct.export or {}
    meta_entries = {str(e.get("ns")): e for e in (export.get("meta") or {}).get("imports") or [] if isinstance(e, dict)}
    out: List[_Cand] = []
    for bns, bundle in sorted((export.get("bundled") or {}).items()):
        if not isinstance(bundle, dict) or not isinstance(bundle.get("export"), dict):
            raise Refused("%s: bundled %s is not an object with an export; re-import %s" % (direct.ns, bns, direct.ns))
        bexp = bundle["export"]
        bmeta = bexp.get("meta") or {}
        src = meta_entries.get(bns)
        if src is None:
            raise Refused("%s bundles %s but its meta.imports has no lock entry for it; the release is broken"
                          % (direct.ns, bns))
        data = util.canonical_bytes(bexp)
        if util.sha256_hex(data) != bundle.get("sha256"):
            raise Refused("%s: bundled %s does not match its sha256" % (direct.ns, bns))
        if src.get("name") != bmeta.get("name"):
            raise Refused("%s: bundled %s is topic %r but its lock entry names %r" % (
                direct.ns, bns, bmeta.get("name"), src.get("name")))
        entry = {
            "ns": bns, "name": bmeta.get("name"), "from": None, "ref": src.get("ref"), "commit": src.get("commit"),
            "export_sha256": bundle["sha256"], "manifest_sha256": None, "kit": bmeta.get("kit"),
            "format": bmeta.get("format"), "packs": _packs_of(bexp), "nodes": len(bexp.get("nodes") or []),
            "edges": len(bexp.get("edges") or []), "via": direct.ns, "override": None, "locked_at": util.today(),
        }
        out.append(_Cand(entry, data, bexp))
    return out


# planning ------------------------------------------------------------------------------------------------------
def parse_keep(keep: Any) -> Dict[Optional[str], str]:
    """``{ns: sha prefix}`` from ``garden=<sha>`` (several joined by commas or spaces, or a dict); a bare sha maps
    from None and applies when exactly one namespace conflicts, so it stands alone: mixed with ``ns=<sha>`` pairs,
    given twice, or one ns given two shas is a ``UsageError``."""
    if keep in (None, "", {}):
        return {}
    pairs: List[Tuple[Optional[str], str]] = []
    if isinstance(keep, dict):
        pairs = [(str(k), str(v)) for k, v in keep.items()]
    else:
        items = keep if isinstance(keep, (list, tuple)) else re.split(r"[,\s]+", str(keep).strip())
        for item in items:
            item = str(item).strip()
            if not item:
                continue
            if "=" in item:
                ns, _sep, sha = item.partition("=")
                pairs.append((ns.strip(), sha.strip()))
            else:
                pairs.append((None, item))
    out: Dict[Optional[str], str] = {}
    for ns, sha in pairs:
        sha = sha.lower()
        if not _SHA_RE.match(sha) or len(sha) < SHA_PREFIX_MIN or len(sha) > 64:
            raise UsageError("keep: %r is not an export sha256 (at least %d hex characters)" % (sha, SHA_PREFIX_MIN))
        if ns is not None and not ids.NS_RE.match(ns):
            raise UsageError("keep: %r is not a namespace" % ns)
        if ns in out and out[ns] != sha:
            if ns is None:
                raise UsageError("keep: give one bare sha, or ns=<sha> pairs")
            raise UsageError("keep: %s is given two shas (%s and %s); give one" % (ns, out[ns], sha))
        out[ns] = sha
    if None in out and len(out) > 1:
        raise UsageError("keep: a bare sha stands alone (it applies when one namespace conflicts); with several, "
                         "write each as ns=<sha>")
    return out


def _active_decision(repo: store.Repo, override: Optional[str], hints: _Hints = _CLI) -> Optional[str]:
    if not override:
        return None
    found = ledger.all_decisions(repo).get(override)
    if found is None:
        raise NotFound("override %s: no such decision; record one with %s" % (override, hints.call("decide")),
                       searched=override)
    if found.get("status") != "active":
        raise Refused("override %s is %s (superseded by %s); name an active decision" % (
            override, found.get("status"), found.get("superseded_by")))
    return override


def _pick(cands: Sequence[_Cand], old: Optional[Dict[str, Any]]) -> _Cand:
    """The candidate that stands for a namespace when all name one version: the direct pin named by this call,
    then any direct pin, then the bundled one already in the lock, then the bundled one whose ``via`` sorts first."""
    for test in (lambda c: c.fresh and not c.via, lambda c: not c.via,
                 lambda c: old is not None and c.via == old.get("via") and c.sha == old.get("export_sha256")):
        found = [c for c in cands if test(c)]
        if found:
            return found[0]
    return sorted(cands, key=lambda c: (str(c.via or ""), c.sha))[0]


def _entry_for(winner: _Cand, old: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The lock entry for a winning candidate; the old entry verbatim when it pins the same thing the same way."""
    if old is not None and all(old.get(k) == winner.entry.get(k) for k in ("name", "commit", "export_sha256", "via",
                                                                           "from")):
        return dict(old)
    return dict(winner.entry)


class _Plan(object):
    def __init__(self) -> None:
        self.lock: Dict[str, Any] = lockfile.empty()
        self.data: Dict[str, bytes] = {}  # ns -> vendored bytes
        self.exports: Dict[str, Dict[str, Any]] = {}  # ns -> parsed export (bundled kept)
        self.notes: List[str] = []
        self.same_commit: List[Dict[str, Any]] = []  # one release pinned directly and bundled flattened


def _and(items: Sequence[str], word: str = "and") -> str:
    items = [str(i) for i in items]
    return items[0] if len(items) == 1 else "%s %s %s" % (", ".join(items[:-1]), word, items[-1])


_FIXED = "a bundled parent keeps the ns its bundling topic gave it"


def _elsewhere(cand: _Cand, hints: _Hints, ns: str = "<other>", ref: bool = True) -> str:
    """The call that imports ``cand``'s topic under ``ns`` (with its release, unless ``ref`` is False)."""
    return hints.call("import", **{"action": "add", "ns": ns, "from": cand.entry.get("from") or "<its repo>",
                                   "ref": cand.entry.get("ref") if ref else None})


def _collision(ns: str, group: Sequence[_Cand], fresh: Set[str], hints: _Hints = _CLI) -> Refused:
    """The refusal for two topics under one ns, with advice that can be followed: a bundled parent keeps the ns its
    bundler's release gave it, so another ``--ns`` for the bundler does not help, and a direct pin of a topic that
    is also bundled under this ns cannot move away from it."""
    who = "; ".join("%s (%s)" % (c.name, "via %s" % c.via if c.via else "direct") for c in group)
    text = "namespace collision: %s names different topics: %s. " % (ns, who)
    bundlers = sorted({str(c.via) for c in group if c.via})
    bundled = {c.name for c in group if c.via}
    movable = [c for c in group if not c.via and c.name not in bundled]
    if not bundlers:
        fresh_direct = [c for c in movable if c.fresh]
        if not fresh_direct:
            return Refused(text + "Import one of them under another ns", ns=ns)
        return Refused(text + "Import %s under another ns (%s)" % (fresh_direct[0].name, _elsewhere(fresh_direct[0],
                                                                                                    hints)), ns=ns)
    text += "%s is fixed by the release%s of %s: %s" % (ns, "s" if len(bundlers) > 1 else "", _and(bundlers), _FIXED)
    moved = [b for b in bundlers if b in fresh]
    if moved:
        text += ", so importing %s under another ns does not change it" % _and(moved)
    options = ["import %s under another ns (%s)" % (c.name, _elsewhere(c, hints)) for c in movable if c.fresh]
    options += ["move your direct pin of %s to another ns (%s, then %s)" % (
                    c.name, hints.call("import", action="remove", ns=ns), _elsewhere(c, hints, ref=False))
                for c in movable if not c.fresh]
    if len(bundlers) > 1:
        options.append("import only one of %s" % _and(bundlers))
    options.append("have %s import its %s parent under another ns and release again, then import that release"
                   % (_and(bundlers, "or"), ns))
    return Refused("%s. Options: %s" % (text, "; or ".join(options)), ns=ns, fixed_by=bundlers)


def _own_ns(ns: str, entry: Dict[str, Any], bundlers: Sequence[str], fresh: Set[str],
            chosen: Dict[str, Dict[str, Any]], hints: _Hints = _CLI) -> Refused:
    """The refusal for an import under this topic's own ns, with advice that can be followed: a topic's own ns is
    fixed (``onto init``), and a bundled parent keeps the ns its bundler's release gave it, so when a release bundles
    the clashing topic, another ``--ns`` for that bundler does not help (see ``_collision``)."""
    text = "namespace %s is this topic's own ns" % ns
    if not bundlers:
        return Refused(text + "; import %s under another ns (%s)" % (entry.get("name") or "it", hints.call(
            "import", **{"action": "add", "ns": "<other>", "from": entry.get("from") or "<its repo>",
                         "ref": entry.get("ref")})), ns=ns)
    text += ", and the release%s of %s bundle%s topic %s as %s: %s" % (
        "s" if len(bundlers) > 1 else "", _and(bundlers), "" if len(bundlers) > 1 else "s", entry.get("name"), ns,
        _FIXED)
    moved = [b for b in bundlers if b in fresh]
    if moved:
        text += ", so importing %s under another ns does not change it" % _and(moved)
    where = ["%s (from %s)" % (b, chosen[b]["from"]) if (chosen.get(b) or {}).get("from") else b for b in bundlers]
    return Refused("%s. Options: have %s import %s %s parent under another ns and release again, then import that "
                   "release; or leave %s out" % (text, _and(where), "their" if len(where) > 1 else "its", ns,
                                                 _and(bundlers)),
                   ns=ns, fixed_by=list(bundlers))


def _twice(name: str, sides: Sequence[Tuple[str, Dict[str, Any], Sequence[str]]], fresh: Set[str],
           hints: _Hints = _CLI) -> Refused:
    """The refusal for one topic under two ns, with advice that can be followed (see ``_collision``). Each side is
    ``(ns, lock entry, the imports whose releases bundle the topic under that ns)``."""
    text = "topic %s would be imported twice, as %s and as %s" % (name, sides[0][0], sides[1][0])
    fixed = [(ns, sorted(by)) for ns, _e, by in sides if by]
    free = [(ns, e) for ns, e, by in sides if not by]
    if not fixed:
        return Refused(text + "; keep one ns")
    bundlers = sorted({b for _ns, by in fixed for b in by})
    text += ": %s, and %s" % ("; ".join("the release%s of %s bundle%s it as %s" % (
        "s" if len(by) > 1 else "", _and(by), "" if len(by) > 1 else "s", ns) for ns, by in fixed), _FIXED)
    if len(fixed) > 1:
        return Refused("%s. Options: import only one of %s; or have %s import %s under the other's ns and release "
                       "again" % (text, _and(bundlers), _and(bundlers, "or"), name), fixed_by=bundlers)
    (fns, fby), (dns, de) = fixed[0], free[0]
    if dns in fresh:
        return Refused("%s. Import it as %s instead (the same release is one pin; another release is a pin conflict "
                       "you settle with an override)" % (text, fns), fixed_by=bundlers)
    return Refused("%s. Options: move your direct pin from %s to %s (%s, then %s); or have %s import it as %s and "
                   "release again" % (
                       text, dns, fns, hints.call("import", action="remove", ns=dns),
                       hints.call("import", action="add", ns=fns, **{"from": de.get("from") or "<its repo>"}),
                       _and(fby, "or"), dns),
                   fixed_by=bundlers)


def _resolve(ns: str, group: List[_Cand], old: Optional[Dict[str, Any]],
             keep: Dict[Optional[str], str], override: Optional[str], active: Set[str],
             single_conflict: bool, fresh: Set[str] = frozenset(),  # type: ignore[assignment]
             hints: _Hints = _CLI) -> Tuple[Optional[_Cand], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """(winner, entry, conflict) for one namespace. ``fresh``: the direct namespaces this call names."""
    names = sorted({c.name for c in group})
    if len(names) > 1:
        raise _collision(ns, group, fresh, hints)
    versions = sorted({c.commit or c.sha for c in group})
    shas = sorted({c.sha for c in group})
    if len(shas) == 1:
        winner = _pick(group, old)
        entry = _entry_for(winner, old)
        entry["override"] = None
        return winner, entry, None
    choice = keep.get(ns) or (keep.get(None) if single_conflict and len(versions) > 1 else None)
    if choice:
        matching = [c for c in group if c.sha.startswith(choice)]
        if not matching:
            raise UsageError("keep %s=%s matches none of the conflicting exports: %s" % (ns, choice, ", ".join(shas)))
        if len({c.sha for c in matching}) > 1:
            raise UsageError("keep %s=%s matches several exports; give more of the sha" % (ns, choice))
        if not override:
            raise Refused("keep needs override: the id of an active decision that records this choice (%s)"
                          % hints.call("decide"))
        winner = _pick(matching, old)
        entry = _entry_for(winner, old)
        entry["override"] = {"decision": override, "kept": winner.sha, "dropped": [s for s in shas if s != winner.sha]}
        return winner, entry, None
    ov = (old or {}).get("override")
    if isinstance(ov, dict) and ov.get("decision") in active and ov.get("kept") in shas \
            and all(s == ov.get("kept") or s in (ov.get("dropped") or []) for s in shas):
        winner = _pick([c for c in group if c.sha == ov.get("kept")], old)
        entry = _entry_for(winner, old)
        entry["override"] = dict(ov)
        return winner, entry, None
    info = {"ns": ns, "name": names[0], "pins": [c.describe() for c in
                                                 sorted(group, key=lambda c: (str(c.via or ""), c.sha))]}
    if len(versions) == 1:
        # one commit, two byte forms: a direct pin and the flattened bundle of the same release. It is one pin
        # (dedup); the lock check may still count the bundle as another sha, which ``_write`` reports with these.
        winner = _pick(group, old)
        entry = _entry_for(winner, old)
        entry["override"] = None
        return winner, entry, dict(info, same_commit=True)
    return None, None, info


def _conflict_way_out(conflicts: Sequence[Dict[str, Any]], hints: _Hints) -> str:
    """How to settle pin conflicts: the decision to record (one option per export sha when one namespace conflicts,
    its id a sha prefix that ``keep`` takes) and this call again with ``override`` and ``keep``."""
    nss = [str(c["ns"]) for c in conflicts]
    options: Optional[List[str]] = None
    if len(conflicts) == 1:
        labels: Dict[str, List[str]] = {}
        first: Dict[str, Dict[str, Any]] = {}
        for p in conflicts[0]["pins"]:
            sha = _short(p["sha256"])
            first.setdefault(sha, p)
            labels.setdefault(sha, []).append("via %s" % p["via"] if p["via"] else "direct")
        options = ["%s=%s %s (%s)" % (sha, first[sha]["ref"] or "-", first[sha]["commit7"], " and ".join(how))
                   for sha, how in labels.items()]
    decide = hints.call("decide", question="Which %s release should this topic keep?" % _and(nss), options=options,
                        chosen="<option>" if options else "<choice>")
    keep = "%s=<option>" % nss[0] if options else ",".join("%s=<sha>" % ns for ns in nss)
    return "Record which one to keep (%s), then run again with that decision and the export sha kept%s: %s" % (
        decide, " (the option id)" if options else "", hints.rerun(override="<decision>", keep=keep))


def _plan(repo: store.Repo, old_lock: Dict[str, Any], directs: List[_Cand], keep: Dict[Optional[str], str],
          override: Optional[str], hints: _Hints = _CLI) -> _Plan:
    """The lock that pins ``directs`` and the parents they bundle (see the module docstring). ``hints`` words the
    follow-up calls the refusals name (``again``: this call)."""
    old = _entries(old_lock)
    active = {d for d, rec in ledger.all_decisions(repo).items() if rec.get("status") == "active"}
    fresh = {c.ns for c in directs if c.fresh}
    live = list(directs)
    winners: Dict[str, _Cand] = {}
    chosen: Dict[str, Dict[str, Any]] = {}
    conflicts: List[Dict[str, Any]] = []
    conflict_groups: Dict[str, List[_Cand]] = {}
    groups: Dict[str, List[_Cand]] = {}
    same_commit: List[Dict[str, Any]] = []
    multi_final: List[str] = []
    for _round in range(MAX_ROUNDS):
        groups = {}
        for cand in live:
            groups.setdefault(cand.ns, []).append(cand)
        for cand in list(live):
            for via in _vias(cand):
                groups.setdefault(via.ns, []).append(via)
        multi = [ns for ns, g in groups.items() if len({c.commit or c.sha for c in g}) > 1]
        winners, chosen, conflicts, conflict_groups, same_commit = {}, {}, [], {}, []
        for ns in sorted(groups):
            group = sorted(groups[ns], key=lambda c: (bool(c.via), not c.fresh, str(c.via or "")))
            winner, entry, conflict = _resolve(ns, group, old.get(ns), keep, override, active, len(multi) == 1,
                                               fresh, hints)
            if conflict is not None and not conflict.get("same_commit"):
                conflicts.append(conflict)
                conflict_groups[ns] = sorted(group, key=lambda c: (str(c.via or ""), c.sha))  # the order of "pins"
                continue
            if conflict is not None:
                same_commit.append(conflict)
            winners[ns] = winner  # type: ignore[assignment]
            chosen[ns] = entry  # type: ignore[assignment]
        multi_final = multi
        losers = [c for c in live if c.ns in winners and winners[c.ns] is not c]
        if not losers:
            break
        live = [c for c in live if c not in losers]
    if conflicts:
        standing = {w.ns: w.export for w in winners.values() if w.export is not None}
        parts = []
        for c in conflicts:
            bridges = _bridges_into(repo, standing, c["ns"])
            for pin, cand in zip(c["pins"], conflict_groups[c["ns"]]):
                pin["cuts"] = _cuts(bridges, c["ns"], cand.export) if cand.export is not None else []
            text = "%s is pinned at %s" % (c["ns"], " and at ".join(
                "%s %s (sha %s, %s)" % (p["ref"], p["commit7"], p["sha256"],
                                        "via %s" % p["via"] if p["via"] else "direct")
                for p in c["pins"]))
            for p in c["pins"]:
                if p["cuts"]:
                    text += "; keeping %s %s would cut %s" % (p["ref"], p["commit7"], _cut_text(p["cuts"]))
            parts.append(text)
        raise Refused("pin conflict: %s. %s" % ("; ".join(parts), _conflict_way_out(conflicts, hints)),
                      conflicts=conflicts)
    fixed = {ns: sorted({str(c.via) for c in g if c.via}) for ns, g in groups.items()}  # who bundles each ns
    for ns in sorted(k for k in keep if k is not None):
        if ns not in chosen:
            raise UsageError("keep names %s, which this change does not pin" % ns)
    plan = _Plan()
    plan.same_commit = same_commit
    names: Dict[str, str] = {}
    for ns in sorted(chosen):
        entry = chosen[ns]
        if not ids.NS_RE.match(ns) or ns in ids.RESERVED_NS:
            raise Refused("an import cannot use the namespace %r" % ns)
        if entry.get("name") == repo.name:
            if fixed.get(ns):
                raise Refused("the release%s of %s bundle%s this topic itself (%s) as %s; a topic cannot import "
                              "itself, even through another topic" % (
                                  "s" if len(fixed[ns]) > 1 else "", _and(fixed[ns]),
                                  "" if len(fixed[ns]) > 1 else "s", repo.name, ns), fixed_by=fixed[ns])
            raise Refused("%s is this topic itself (%s); a topic cannot import itself" % (ns, repo.name))
        if ns == repo.ns:
            raise _own_ns(ns, entry, fixed.get(ns) or [], fresh, chosen, hints)
        if entry.get("name") in names:
            first = names[str(entry["name"])]
            raise _twice(str(entry["name"]), [(first, chosen[first], fixed.get(first, [])),
                                              (ns, entry, fixed.get(ns, []))], fresh, hints)
        names[str(entry.get("name"))] = ns
        winner = winners[ns]
        if winner.tampered:
            raise Refused("imports/%s/export.json differs from the lock; restore it (git checkout) or run %s to "
                          "vendor it again" % (ns, hints.call("import", action="update", ns=ns, ref=entry.get("ref"))))
        errors = records.check(entry, "lock_entry")
        if errors:
            raise DataError("the lock entry for %s fails its schema: %s" % (ns, "; ".join(errors[:3])))
        plan.lock["imports"].append(entry)
        plan.data[ns] = winner.data  # type: ignore[assignment]
        plan.exports[ns] = winner.export  # type: ignore[assignment]
    if override and not any(isinstance(e.get("override"), dict) and e["override"].get("decision") == override
                            for e in plan.lock["imports"]):
        plan.notes.append("override %s was not needed: no pin conflict" % override)
    elif keep and not override and not multi_final:
        plan.notes.append("keep was not needed: no pin conflict")
    return plan


# diffs ---------------------------------------------------------------------------------------------------------
def _node_map(ns: str, export: Optional[Dict[str, Any]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for row in (export or {}).get("nodes") or []:
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            out[ids.qualify(ns, row["id"])] = util.canonical_line(row)
    return out


def _source_ids(ns: str, export: Optional[Dict[str, Any]]) -> Set[str]:
    """The qualified ids of the sources an export lists (``<ns>/src-...``): the graph loads them as read-only source
    nodes, so a bridge ending on one is present, not dangling."""
    return {ids.qualify(ns, r["id"]) for r in (export or {}).get("sources") or []
            if isinstance(r, dict) and isinstance(r.get("id"), str) and r["id"].startswith("src-")}


def _archived(ns: str, export: Optional[Dict[str, Any]]) -> Set[str]:
    return {ids.qualify(ns, r["id"]) for r in (export or {}).get("nodes") or []
            if isinstance(r, dict) and isinstance(r.get("id"), str) and r.get("status") == "archived"}


def _side(e: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """One side of a lock diff item."""
    if e is None:
        return None
    ov = e.get("override") if isinstance(e.get("override"), dict) else None
    return {"ref": e.get("ref"), "commit7": _short(e.get("commit"), 7), "sha12": _short(e.get("export_sha256")),
            "via": e.get("via"), "from": e.get("from"), "override": (ov or {}).get("decision")}


def _lock_diff(old: Dict[str, Any], new: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``[{ns, change: added|removed|changed, before, after}]`` for the entries that differ."""
    a, b = _entries(old), _entries(new)
    out: List[Dict[str, Any]] = []
    keys = ("ref", "commit", "export_sha256", "via", "from", "override")
    for ns in sorted(set(a) | set(b)):
        before, after = a.get(ns), b.get(ns)
        if before is not None and after is not None and all(before.get(k) == after.get(k) for k in keys):
            continue
        change = "added" if before is None else ("removed" if after is None else "changed")
        out.append({"ns": ns, "change": change, "before": _side(before), "after": _side(after)})
    return out


def _affected(old: Dict[str, Any], new: Dict[str, Any]) -> List[str]:
    """Namespaces whose pinned data changes (added, removed or another sha)."""
    a, b = _entries(old), _entries(new)
    return sorted(ns for ns in set(a) | set(b)
                  if (a.get(ns) or {}).get("export_sha256") != (b.get(ns) or {}).get("export_sha256"))


def _old_export(repo: store.Repo, ns: str) -> Optional[Dict[str, Any]]:
    try:
        return lockfile.read_export(repo, ns)
    except DataError:
        return None


def _touching(onto: Ontology, namespaces: Set[str]) -> List[str]:
    """Local edges with an end in ``namespaces`` and local records citing their pinned exports."""
    out: List[str] = []
    for eid in onto.local_edge_ids:
        edge = onto.edges[eid]
        if any(onto.ns_of(str(edge.get(k) or "")) in namespaces for k in ("src", "dst")):
            out.append(eid)
    for rid in list(onto.local_ids) + list(onto.local_edge_ids):
        rec = onto.record(rid) or {}
        for p in rec.get("prov") or []:
            src = str((p or {}).get("src") or "") if isinstance(p, dict) else ""
            m = ids.IMPSRC_RE.match(src)
            if m and m.group(1) in namespaces and rid not in out:
                out.append(rid)
                break
    return out


def _bridge_item(eid: str, edge: Dict[str, Any], end: str, owner: Optional[str]) -> Dict[str, Any]:
    """One affected bridge: a local one (``owner`` None) or one an import brings (``inherited``, ``owner`` its ns)."""
    item = {"edge": eid, "src": edge.get("src"), "rel": edge.get("rel"), "dst": edge.get("dst"), "end": end,
            "status": edge.get("status"), "trust": edge.get("trust"), "inherited": owner is not None}
    if owner is not None:
        item["owner"] = owner
    item.update(render.flags(edge))  # C.7: untrusted and draft flags on the JSON item
    return item


def _inherited(new: Ontology, affected: Set[str]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(dangling, archived): the active bridges the imports bring (a composed topic's bridges, read-only here) that
    the planned pins leave pointing at a missing or archived node. Only those an affected import brings or with an
    end in an affected namespace are listed."""
    dangling: List[Dict[str, Any]] = []
    archived: List[Dict[str, Any]] = []
    for eid in sorted(new.bridges):
        origin = new.edge_origin.get(eid)
        edge = new.edges.get(eid)
        if origin is None or edge is None or edge.get("status") == "archived":
            continue
        owners = {o[0] for o in new.edge_origins.get(eid) or [origin]}
        ends = [str(edge.get(k) or "") for k in ("src", "dst")]
        if not owners & affected and not any(new.ns_of(end) in affected for end in ends):
            continue
        for end in ends:
            node = new.nodes.get(end)
            if node is None:
                dangling.append(_bridge_item(eid, edge, end, origin[0]))
            elif node.get("status") == "archived":
                archived.append(_replaced(_bridge_item(eid, edge, end, origin[0]), new))
    return dangling, archived


def _replaced(item: Dict[str, Any], new: Ontology) -> Dict[str, Any]:
    """An archived-end item with ``now``: the ids that replace its end under the planned pins (the graph qualifies an
    imported node's ``archived.superseded_by`` at load), when the release names any."""
    block = (new.nodes.get(str(item.get("end"))) or {}).get("archived")
    now = block.get("superseded_by") if isinstance(block, dict) else None
    if isinstance(now, list) and now:
        item["now"] = [str(x) for x in now if isinstance(x, str)]
    return item


def _bridges_into(repo: store.Repo, standing: Dict[str, Dict[str, Any]], ns: str
                  ) -> List[Tuple[str, Dict[str, Any], str, Optional[str]]]:
    """``(edge id, edge, end, owner)`` for the active bridges with an end in ``ns``: the local ones (owner None) and
    those the ``standing`` exports bring (owner their ns). Used to say what each side of a pin conflict cuts."""
    onto = Ontology.load(repo)
    out: List[Tuple[str, Dict[str, Any], str, Optional[str]]] = []
    for eid in onto.local_edge_ids:
        edge = onto.edges[eid]
        if eid in onto.bridges and edge.get("status") != "archived":
            out += [(eid, edge, str(edge.get(k)), None) for k in ("src", "dst") if onto.ns_of(str(edge.get(k))) == ns]
    seen: Set[str] = set()
    for owner in sorted(x for x in standing if x != ns):
        for row in standing[owner].get("edges") or []:
            if not isinstance(row, dict) or row.get("status") == "archived" or not row.get("src") or not row.get("dst"):
                continue
            src, dst = ids.qualify(owner, str(row["src"])), ids.qualify(owner, str(row["dst"]))
            if ids.split_ns(src)[0] == ids.split_ns(dst)[0]:
                continue
            eid = ids.edge_id(src, str(row.get("rel") or ""), dst, str(row.get("key") or ""))
            if eid in seen:
                continue
            seen.add(eid)
            edge = dict(row, id=eid, src=src, dst=dst)
            out += [(eid, edge, end, owner) for end in (src, dst) if ids.split_ns(end)[0] == ns]
    return out


def _cuts(bridges: Sequence[Tuple[str, Dict[str, Any], str, Optional[str]]], ns: str,
          export: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The ``bridges`` whose end in ``ns`` is missing (``why`` dangling) or archived in ``export``."""
    status = {sid: "confirmed" for sid in _source_ids(ns, export)}
    status.update({ids.qualify(ns, r["id"]): r.get("status") for r in export.get("nodes") or []
                   if isinstance(r, dict) and isinstance(r.get("id"), str)})
    out = []
    for eid, edge, end, owner in bridges:
        if end not in status or status[end] == "archived":
            out.append(dict(_bridge_item(eid, edge, end, owner), why="dangling" if end not in status else "archived"))
    return out


def _cut_text(cuts: Sequence[Dict[str, Any]]) -> str:
    shown = "; ".join("%s %s" % (c["why"], _edge_text(c)) for c in cuts[:3])
    tail = render.more(len(cuts), min(3, len(cuts)))
    return "%d bridge(s): %s%s" % (len(cuts), shown, "; " + tail if tail else "")


def _planned(repo: store.Repo, onto: Ontology, plan: _Plan) -> Ontology:
    """The graph of ``repo`` (loaded as ``onto``) under the planned pins, built in memory."""
    return Ontology.from_rows(
        repo, repo.manifest, [onto.nodes[n] for n in onto.local_ids], [onto.edges[e] for e in onto.local_edge_ids],
        list(onto.sources.values()), plan.lock, plan.exports,
    )


def open_conflicts(onto: Ontology) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """``{(class, field): {class, field, values}}``: the fields on which the members of a ``same_as`` class disagree
    and no decision settles (``needs``), keyed by the class's first member id."""
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for members in onto.classes.values():
        if len(members) < 2:
            continue
        first = sorted(members)[0]
        try:
            found = needs._conflicts(onto, first)
            for c in found:
                if not needs.conflict_settled(onto, first, c["field"], c["values"]):
                    out[(first, c["field"])] = {"class": first, "field": c["field"], "values": list(c["values"])}
        except Exception:  # a broken record is validate's to report; the diff never fails on it
            continue
    return out


def _conflict_diff(onto: Ontology, new: Ontology) -> Dict[str, List[Dict[str, Any]]]:
    """``{new, changed}``: the open conflicts the new pins add, and those whose disagreeing values they change
    (a new value, or a new member; a decision settles only the values it recorded)."""
    before, after = open_conflicts(onto), open_conflicts(new)
    added = [after[k] for k in sorted(after) if k not in before]
    changed = [after[k] for k in sorted(after) if k in before and before[k]["values"] != after[k]["values"]]
    return {"new": added, "changed": changed}


def _diff(repo: store.Repo, old_lock: Dict[str, Any], plan: _Plan) -> Dict[str, Any]:
    """The lock diff, node diff, affected bridges (local ones, and the bridges an import brings) and the validation
    problems the new pins would add."""
    onto = Ontology.load(repo)
    new = _planned(repo, onto, plan)
    affected = _affected(old_lock, plan.lock)
    added: List[str] = []
    removed: List[str] = []
    changed: List[str] = []
    new_nodes: Dict[str, Dict[str, str]] = {}
    new_sources: Dict[str, Set[str]] = {}
    for ns in affected:
        new_sources[ns] = _source_ids(ns, plan.exports.get(ns))
        before = _node_map(ns, _old_export(repo, ns) if ns in _entries(old_lock) else None)
        after = _node_map(ns, plan.exports.get(ns))
        new_nodes[ns] = after
        added += [i for i in after if i not in before]
        removed += [i for i in before if i not in after]
        changed += [i for i in after if i in before and after[i] != before[i]]
    dangling: List[Dict[str, Any]] = []
    archived_ends: List[Dict[str, Any]] = []
    gone = set(affected)
    touched = _touching(onto, gone)
    archived_now = {ns: _archived(ns, plan.exports.get(ns)) for ns in affected}
    for eid in touched:
        edge = onto.edges.get(eid)
        if edge is None or edge.get("status") == "archived":
            continue
        for key in ("src", "dst"):
            end = str(edge.get(key) or "")
            ns = onto.ns_of(end)
            if ns not in gone:
                continue
            item = _bridge_item(eid, edge, end, None)
            if end not in new_nodes.get(ns, {}) and end not in new_sources.get(ns, set()):
                dangling.append(item)
            elif end in archived_now.get(ns, set()):
                archived_ends.append(_replaced(item, new))
    more_dangling, more_archived = _inherited(new, gone)
    dangling += more_dangling
    archived_ends += more_archived
    found = _new_problems(onto, new, touched)
    return {
        "conflicts": _conflict_diff(onto, new),
        "lock_diff": _lock_diff(old_lock, plan.lock),
        "node_diff": {"added": sorted(added), "removed": sorted(removed), "changed": sorted(changed)},
        "bridges_affected": {"dangling": dangling, "archived": archived_ends},
        "problems": [p.text() for p in found],
        "found": found,
        "affected": affected,
        "new": new,  # the graph under the planned pins
    }


def _new_problems(onto: Ontology, new: Ontology, touched: Sequence[str]) -> List[Problem]:
    """Problems the local records and the local pack would gain under the planned pins (``new``)."""
    ids_now = [t for t in touched if onto.record(t)]
    before = {(p.code, p.message) for p in validate.check_graph(onto, ids_now)}
    before |= {(p.code, p.message) for p in onto.registry.problems()}
    before |= {(p.code, p.message) for p in validate.calibration_problems(onto)}
    found = [p for p in validate.check_graph(new, touched) if (p.code, p.message) not in before]
    found += [p for p in new.registry.problems() if (p.code, p.message) not in before]
    found += [p for p in validate.calibration_problems(new) if (p.code, p.message) not in before]
    return found


def _problem_bridges(repo: store.Repo, found: Sequence[Problem]) -> List[Dict[str, Any]]:
    """The local edges the planned pins' problems are about (a P09 when a kind a bridge ends on changes upstream),
    found by the edge line a problem names or by the edge id in its message."""
    onto = Ontology.load(repo)
    at_line = {onto.lines[e]: e for e in onto.local_edge_ids if e in onto.lines}
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for p in found:
        eid = at_line.get((p.file, p.line)) if p.line else None
        if eid is None:
            eid = next((e for e in onto.local_edge_ids if e in str(p.message)), None)
        if eid is None or eid in seen or eid not in onto.edges:
            continue
        seen.add(eid)
        edge = onto.edges[eid]
        out.append(_bridge_item(eid, edge, str(edge.get("dst") or ""), None))
    return out


def _remove_refusal(ns: str, found: Sequence[Problem]) -> str:
    """Why ``remove`` is refused, worded by where the problems would be: local records or the local pack."""
    records_ = [p for p in found if str(p.file).startswith("graph/")]
    pack = [p for p in found if str(p.file).startswith("packs/")]
    why = []
    if records_:
        why.append("%d local record(s) still refer to %s (bridges or provenance)" % (len(records_), ns))
    if pack:
        why.append("the local pack names kinds of %s (%d problem(s): a kind_map or relation entry)" % (ns, len(pack)))
    other = len(found) - len(records_) - len(pack)
    if other:
        why.append("%d other problem(s)" % other)
    return "remove refused: %s; these would be problems: %s" % ("; ".join(why), "; ".join(
        p.text() for p in list(found)[:3]))


# writing -------------------------------------------------------------------------------------------------------
def _snapshot(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def _put_back(path: str, data: Optional[bytes], prune: bool = False) -> None:
    """Restore ``path`` to ``data`` (None: the file did not exist). ``prune`` also removes its folder when that
    is left empty (an ``imports/<ns>/`` folder)."""
    if data is not None:
        store.write_bytes(path, data)
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    if prune:
        try:
            os.rmdir(os.path.dirname(path))
        except OSError:
            pass


def _clear_caches() -> None:
    clear_graph_cache()
    lockfile.clear_cache()
    store.clear_cache()


def _rewrites(repo: store.Repo, plan: _Plan) -> List[str]:
    """Namespaces whose vendored file must be (re)written: new, changed, or differing from the planned bytes."""
    return [ns for ns in sorted(plan.data) if _snapshot(lockfile.export_path(repo, ns)) != plan.data[ns]]


def _touched(repo: store.Repo, old_lock: Dict[str, Any], plan: _Plan) -> List[str]:
    """Namespaces whose vendored file a write creates, replaces or removes."""
    gone = set(_entries(old_lock)) - set(_entries(plan.lock))
    return sorted(set(_rewrites(repo, plan)) | gone)


def _guard_paths(repo: store.Repo, namespaces: Iterable[str]) -> None:
    """Refuse (P19, nothing written) when a path a write would create, replace or remove is a symbolic link or
    resolves outside the topic repo: ``imports/``, the lock, ``imports/<ns>/`` and its ``export.json``. So a
    linked folder never makes the kit write or delete a file elsewhere."""
    rels = ["imports", lockfile.LOCK]
    for ns in sorted(set(namespaces)):
        rels += ["imports/%s" % ns, lockfile.EXPORT % ns]
    found: List[Problem] = []
    for rel in rels:
        full = repo.path(rel)
        if os.path.islink(full):
            found.append(Problem("P19", rel, 0, "a symbolic link; topic folders hold regular files only"))
        elif not store.inside(repo.root, full):
            found.append(Problem("P19", rel, 0, "resolves outside the topic repo"))
    if found:
        raise Refused("nothing was written: %s. Replace it with a regular folder or file inside the topic repo" % (
            "; ".join(p.text() for p in found[:3])), problems=[p.text() for p in found])


# open proposals that cite the pins -----------------------------------------------------------------------------
STALE = "stale"  # the warning code of an op that no longer applies at the current pins


def _open_proposal_files(repo: store.Repo) -> List[Tuple[str, Dict[str, Any]]]:
    """``(path, proposal)`` for each open proposal in ``proposals/pending/``: regular files inside the topic only,
    so nothing is ever written through a link."""
    folder = repo.path(pipeline.PENDING)
    if not os.path.isdir(folder) or os.path.islink(folder) or not store.inside(repo.root, folder):
        return []
    out: List[Tuple[str, Dict[str, Any]]] = []
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if not name.startswith("prop-") or not name.endswith(".json") or os.path.islink(path):
            continue
        try:
            prop = store.read_json(path)
        except (DataError, OSError):
            continue
        if isinstance(prop, dict) and isinstance(prop.get("id"), str) and prop.get("status") in pipeline.OPEN:
            out.append((path, prop))
    return out


def _held(onto: Ontology, ns: str, loc: str) -> bool:
    """True when ``loc`` is an active node or edge that the import ``ns`` brings."""
    if not loc or not onto.active(loc):
        return False
    origin = onto.edge_origin.get(loc)
    return (origin[0] if origin else onto.ns_of(loc)) == ns


def _sync_op(op: Dict[str, Any], onto: Ontology, current: Dict[str, str], labels: Dict[str, str]
             ) -> Tuple[List[str], List[str]]:
    """Re-cite, in place, each ``imp:<ns>@<commit>`` provenance of ``op`` that names another pin of a namespace
    still imported, when the id it cites is still active there. Returns (the namespaces re-cited, why the op no
    longer applies at the current pins: a cited id or a bridge end gone or archived, or its namespace no longer
    imported)."""
    recited: List[str] = []
    why: Dict[str, str] = {}
    for p in op.get("prov") or []:
        src = str((p or {}).get("src") or "") if isinstance(p, dict) else ""
        m = ids.IMPSRC_RE.match(src)
        if not m or src == current.get(m.group(1)):
            continue
        ns, loc = m.group(1), str(p.get("loc") or "")
        if ns not in current:
            why.setdefault(ns, "%s is not imported now" % ns)
        elif _held(onto, ns, loc):
            p["src"] = current[ns]
            recited.append(ns)
        else:
            why.setdefault(loc or src, "%s is not in %s %s (removed or archived)" % (loc or src, ns, labels[ns]))
    if op.get("op") == "add_edge":
        edge = op.get("edge") or {}
        for end in (str(edge.get("src") or ""), str(edge.get("dst") or "")):
            ns = onto.ns_of(end)
            if not end or end.startswith("$") or ns == "self":
                continue
            if ns not in current:
                why.setdefault(ns, "%s is not imported now" % ns)
            elif not onto.active(end):
                why.setdefault(end, "%s is not in %s %s (removed or archived)" % (end, ns, labels[ns]))
    return sorted(set(recited)), list(why.values())


def _sync_proposals(repo: store.Repo, onto: Ontology, lock: Dict[str, Any]
                    ) -> Tuple[List[Tuple[str, Dict[str, Any]]], List[Dict[str, Any]]]:
    """(writes, report) that keep the open proposals in step with the pins ``lock`` names (``onto`` loaded with
    them). Apply takes provenance of the current pins only, so an ``imp:`` citation of another pin of a namespace
    still imported is re-cited to the current pin when the id it cites is still active there. An op that no longer
    applies (a cited id or a bridge end removed or archived, or its namespace no longer imported) gets
    ``annot.stale`` and a ``stale`` warning, which review shows; ``suggest`` then neither counts its pair as offered
    nor, once rejected, as a rejected identity. An op that applies again loses both. ``writes`` is ``[(path, new
    proposal)]``; each report item is ``{id, recited: [{n, ns}], stale: [{n, why}], cleared: [n], changed}``."""
    entries = _entries(lock)
    current = {ns: ids.impsrc(ns, str(e["commit"])) for ns, e in entries.items() if e.get("commit")}
    labels = {ns: _pin_text(dict(e, via=None)) for ns, e in entries.items()}
    writes: List[Tuple[str, Dict[str, Any]]] = []
    report: List[Dict[str, Any]] = []
    for path, prop in _open_proposal_files(repo):
        new = copy.deepcopy(prop)
        review_ = new.get("review") if isinstance(new.get("review"), dict) else {}
        edits = review_.get("edits") if isinstance(review_.get("edits"), dict) else {}
        verdicts = review_.get("verdicts") if isinstance(review_.get("verdicts"), dict) else {}
        item: Dict[str, Any] = {"id": new["id"], "recited": [], "stale": [], "cleared": [], "changed": False}
        for op in new.get("ops") or []:
            if not isinstance(op, dict):
                continue
            n = op.get("n")
            recited, why = _sync_op(op, onto, current, labels)
            edit = edits.get(str(n))
            if isinstance(edit, dict):
                recited_e, why_e = _sync_op(edit, onto, current, labels)
                recited = sorted(set(recited) | set(recited_e))
                if verdicts.get(str(n)) == "edit":  # the edit is what applies
                    why = why_e
            item["recited"] += [{"n": n, "ns": ns} for ns in recited]
            annot = op.get("annot") if isinstance(op.get("annot"), dict) else None
            if why:
                if annot is None:
                    annot = op["annot"] = {}
                annot["stale"] = True
                item["stale"].append({"n": n, "why": "; ".join(why)})
            elif annot is not None and annot.get("stale"):
                annot["stale"] = False
                item["cleared"].append(n)
        checks = new.get("checks")
        if isinstance(checks, dict):
            kept = [w for w in checks.get("warnings") or [] if not (isinstance(w, dict) and w.get("code") == STALE)]
            checks["warnings"] = kept + [
                {"n": s["n"], "code": STALE, "message": "no longer applies at the current pins: %s; reject or edit "
                                                        "this op" % s["why"]} for s in item["stale"]]
        if util.canonical_bytes(new) != util.canonical_bytes(prop):
            if records.check(new, "proposal"):  # a proposal edited by hand: leave it as it is
                item["recited"], item["cleared"] = [], []
            else:
                item["changed"] = True
                writes.append((path, new))
        if item["changed"] or item["stale"]:
            report.append(item)
    return writes, report


def _sync_notes(report: Sequence[Dict[str, Any]], done: bool, hints: _Hints = _CLI) -> List[str]:
    """Notes on the open proposals a pin change re-cites or leaves stale (``done``: written, else it would be)."""
    notes: List[str] = []
    recited = [r for r in report if r["recited"]]
    if recited:
        notes.append("%s %d open proposal(s) to the current pins: %s" % (
            "re-cited" if done else "would re-cite", len(recited), "; ".join(
                "%s op%s %s" % (r["id"], "s" if len({x["n"] for x in r["recited"]}) > 1 else "",
                                ", ".join(str(n) for n in sorted({x["n"] for x in r["recited"]})))
                for r in recited[:SHOW])))
    for r in [r for r in report if r["stale"]][:SHOW]:
        many = len(r["stale"]) > 1
        notes.append("%s op%s %s %s (%s); review it (%s) and reject or edit %s: a rejected stale op is not "
                     "recorded as a rejected pair" % (
                         r["id"], "s" if many else "", ", ".join(str(s["n"]) for s in r["stale"]),
                         ("no longer apply at the current pins" if many else "no longer applies at the current pins")
                         if done else "would no longer apply at the new pins", r["stale"][0]["why"],
                         hints.call("review", id=r["id"]), "them" if many else "it"))
    return notes


def sync_proposals(repo: store.Repo) -> List[Dict[str, Any]]:
    """Bring the open proposals in step with the pins in the lock now (see ``_sync_proposals``), under the write
    lock; returns the report of what changed or is stale."""
    with store.write_lock(repo):
        repo.reload()
        _clear_caches()
        writes, report = _sync_proposals(repo, Ontology.load(repo), lockfile.read(repo))
        for path, prop in writes:
            store.write_json(path, prop)
    return report


def _lock_bytes(lock: Dict[str, Any]) -> bytes:
    """The bytes ``lockfile.write`` writes for ``lock`` (canonical, entries sorted by ``ns``), known before the
    write so the write intent can name them."""
    out = dict(lock)
    out.setdefault("format", FORMAT)
    out["imports"] = sorted((dict(e) for e in lock.get("imports") or []), key=lambda e: str(e.get("ns") or ""))
    return util.canonical_bytes(out)


def _rel(repo: store.Repo, path: str) -> str:
    return os.path.relpath(path, repo.root).replace(os.sep, "/")


def _write(repo: store.Repo, old_lock: Dict[str, Any], plan: _Plan, summary: str, change_ids: List[str],
           extra: Dict[str, Any], synced: Optional[List[Dict[str, Any]]] = None,
           hints: _Hints = _CLI) -> Dict[str, Any]:
    """Vendor the planned files, write the lock, re-cite the open proposals (``_sync_proposals``; the report goes
    into ``synced``), append the change and the history point; all or nothing. Every file it rewrites or removes
    and both logs it appends to are named in a write intent first (``store.begin_write``), so a process killed half
    way is rolled back by the next writer, as an apply is; a failure in process puts every file back."""
    with store.write_lock(repo):
        repo.reload()
        mutate.check_format(repo)
        if util.canonical_bytes(lockfile.read(repo)) != util.canonical_bytes(old_lock):
            raise Conflict("imports/lock.json changed while this import was planned; nothing was written. Run it again")
        before_problems = {(p.code, p.message) for p in lockfile.verify(repo)}
        touched = _touched(repo, old_lock, plan)
        _guard_paths(repo, touched)  # again under the lock: nothing is written or removed through a link
        rels = {ns: lockfile.EXPORT % ns for ns in touched}
        paths = {ns: repo.path(rels[ns]) for ns in touched}
        saved_exports = {ns: _snapshot(paths[ns]) for ns in touched}
        logs = [ledger.CHANGES, history.HISTORY]
        saved = {rel: _snapshot(repo.path(rel)) for rel in [lockfile.LOCK] + logs}
        lock_data = _lock_bytes(plan.lock)
        # the open proposals as they read at the new pins (the graph under the planned pins, built before any file
        # is written), so the intent names every proposal the write re-cites
        writes, report = _sync_proposals(repo, _planned(repo, Ontology.load(repo), plan), plan.lock)
        props = [(path, util.canonical_bytes(prop)) for path, prop in writes]
        saved_props = {path: _snapshot(path) for path, _data in props}
        rewrites = [(rels[ns], saved_exports[ns], plan.data.get(ns)) for ns in touched]
        rewrites.append((lockfile.LOCK, saved[lockfile.LOCK], lock_data))
        rewrites += [(_rel(repo, path), saved_props[path], data) for path, data in props]
        before = store.data_hash(repo)
        store.begin_write(repo, [r for r in rewrites if r[1] != r[2]], [(rel, saved[rel]) for rel in logs])
        try:
            for ns in touched:
                if ns in plan.data:
                    store.write_bytes(paths[ns], plan.data[ns])
                else:
                    _put_back(paths[ns], None, prune=True)
            if saved[lockfile.LOCK] != lock_data:
                store.write_bytes(repo.path(lockfile.LOCK), lock_data)
            _clear_caches()
            added = [p for p in lockfile.verify(repo) if (p.code, p.message) not in before_problems]
            if added:
                hint = ""
                direct = [c for c in plan.same_commit if any(not p["via"] for p in c["pins"])]
                if direct:
                    hint = (". %s is pinned directly and bundled flattened at the same commit; to keep the direct "
                            "pin, record a decision (%s) and run again with it: %s" % (
                                ", ".join(c["ns"] for c in plan.same_commit), hints.call("decide"),
                                hints.rerun(override="<decision>", keep=",".join(
                                    "%s=%s" % (c["ns"], next(p["sha256"] for p in c["pins"] if not p["via"]))
                                    for c in direct))))
                raise Refused("the new pins would not verify; nothing was written: %s%s" % ("; ".join(
                    p.text() for p in added[:3]), hint), problems=added, conflicts=plan.same_commit)
            for path, data in props:
                store.write_bytes(path, data)
            if synced is not None:
                synced[:] = report
            after = store.data_hash(repo)
            change = ledger.append_change(repo, "import", "user", change_ids, summary, before=before, after=after,
                                          extra=extra)
            history.append_point(repo, mutate.history_point(Ontology.load(repo), "import"))
        except BaseException:
            for rel in [lockfile.LOCK] + logs:
                _put_back(repo.path(rel), saved[rel])
            for ns in touched:
                _put_back(paths[ns], saved_exports[ns], prune=True)
            for path, data in saved_props.items():
                _put_back(path, data)
            store.end_write(repo)
            _clear_caches()
            raise
        store.end_write(repo)
        _clear_caches()
    return change


def _impsrcs(lock: Dict[str, Any], namespaces: Iterable[str]) -> List[str]:
    found = _entries(lock)
    return [ids.impsrc(ns, str(found[ns].get("commit") or "")) for ns in sorted(namespaces)
            if ns in found and found[ns].get("commit")]


def _run(repo: store.Repo, action: str, ns: str, old_lock: Dict[str, Any], plan: _Plan, preview: bool,
         notes: List[str], refuse_problems: bool = False, hints: _Hints = _CLI) -> Dict[str, Any]:
    diff = _diff(repo, old_lock, plan)
    result: Dict[str, Any] = {
        "action": action, "ns": ns, "written": False, "change": None,
        "lock_diff": diff["lock_diff"], "node_diff": diff["node_diff"], "bridges_affected": diff["bridges_affected"],
        "conflicts": diff["conflicts"], "problems": diff["problems"], "pins": [], "proposal": None, "proposals": [],
        "notes": list(notes) + plan.notes,
    }
    if refuse_problems and diff["problems"]:
        raise Refused(_remove_refusal(ns, diff["found"]), problems=diff["problems"],
                      bridges_affected=diff["bridges_affected"])
    repairs = [x for x in _rewrites(repo, plan) if x not in diff["affected"]]
    writes, report = _sync_proposals(repo, diff["new"], plan.lock)
    result["proposals"] = report
    if not diff["lock_diff"] and not repairs:
        result["notes"].append("nothing to change: %s is already pinned that way" % ns)
        if writes and not preview:  # open proposals that cite another pin (a lock changed by git): re-cite them
            result["proposals"] = report = sync_proposals(repo)
        result["notes"] += _sync_notes(report, not preview, hints)
        result["pins"] = pin_status(repo)
        return result
    _guard_paths(repo, _touched(repo, old_lock, plan))  # a preview says what the write would say
    if repairs:
        result["notes"].append("vendors again %s (the file differed from the lock)" % ", ".join(
            "imports/%s/export.json" % x for x in repairs))
    if diff["problems"]:
        failing = _problem_bridges(repo, diff["found"])
        if failing:
            shown = "; ".join(_edge_text(i) for i in failing[:5])
            tail = render.more(len(failing), min(5, len(failing)))
            result["notes"].append("validate will report %d problem(s) until these bridges are re-pointed or "
                                   "archived: %s%s" % (len(diff["problems"]), shown, "; " + tail if tail else ""))
        else:
            result["notes"].append("validate will report %d problem(s) until the bridges listed are re-pointed or "
                                   "archived" % len(diff["problems"]))
    inherited = [i for k in ("dangling", "archived") for i in diff["bridges_affected"][k] if i.get("inherited")]
    if inherited:
        owners = _and(sorted({str(i["owner"]) for i in inherited}))
        result["notes"].append(
            "%d bridge(s) that %s brings would dangle or point at archived nodes; they are read-only here, so keep "
            "the pins %s was released with, or have %s re-point them and release again" % (
                len({i["edge"] for i in inherited}), owners, owners, owners))
    if preview:
        result["notes"] += _sync_notes(report, False, hints)
        result["pins"] = pin_status(repo)
        return result
    affected = sorted(set(diff["affected"]) | set(repairs))
    change_ids = sorted(set(_impsrcs(old_lock, affected)) | set(_impsrcs(plan.lock, affected))) or [ns]
    parts = []
    for item in diff["lock_diff"]:
        if item["change"] == "added":
            parts.append("+%s %s" % (item["ns"], _pin_text(item["after"])))
        elif item["change"] == "removed":
            parts.append("-%s" % item["ns"])
        else:
            parts.append("%s %s -> %s" % (item["ns"], _pin_text(item["before"]), _pin_text(item["after"])))
    parts += ["%s vendored again" % x for x in repairs]
    nd = diff["node_diff"]
    summary = "import %s %s: %s; nodes +%d -%d ~%d" % (action, ns, ", ".join(parts), len(nd["added"]),
                                                       len(nd["removed"]), len(nd["changed"]))
    synced: List[Dict[str, Any]] = []
    change = _write(repo, old_lock, plan, summary, change_ids, {"action": action, "ns": ns}, synced, hints)
    result["written"] = True
    result["change"] = change["id"]
    result["proposals"] = synced
    result["notes"] += _sync_notes(synced, True, hints)
    result["pins"] = pin_status(repo)
    return result


# the public contract -------------------------------------------------------------------------------------------
def add(repo: store.Repo, ns: Optional[str], from_: str, ref: Optional[str], override: Optional[str] = None,
        keep: Any = None, preview: bool = False, *, mcp: bool = False) -> Dict[str, Any]:
    """Pin the release of the topic at ``from_`` (``ref``: a ``vN`` tag or a release commit; default the newest
    ``vN`` tag) under ``ns`` (default: the ns the topic gives itself), with the parents it bundles. With ``preview``
    only the diff is returned. When ``ns`` already pins the same topic directly, the new release replaces that pin
    (as ``update`` would, from the ``from`` given here), so it is never a conflict with itself. See the module
    docstring for dedup, collisions and pin conflicts. ``mcp``: the follow-up calls in messages name MCP tools."""
    root, stored = _from_path(repo, from_)
    notes: List[str] = []
    if not ref:
        ref = newest_tag(root)
        if ref is None:
            raise Refused("%s has no release tag (vN); release it first (onto release --write --commit)" % from_)
        notes.append("ref %s: the newest release of %s" % (ref, stored))
    hints = _Hints(mcp, {"action": "add", "ns": ns or None, "from": from_.strip(), "ref": ref})
    release = read_release(root, ref, stored, hints)
    notes += _ref_notes(ref, release, stored)
    ns = ns or str(release["export"]["meta"].get("ns") or "")
    if not ids.NS_RE.match(ns) or ns in ids.RESERVED_NS:
        raise UsageError("not a namespace: %r (lower case letters, digits and '-', starting with a letter)" % ns)
    hints.again.update(ns=ns, ref=release["ref"])
    old_lock = lockfile.read(repo)
    current = _entries(old_lock).get(ns) or {}
    # the same topic pinned directly under this ns is replaced; another topic under it stays a collision
    moves = bool(current) and not current.get("via") and current.get("name") == release["export"]["meta"].get("name")
    if moves and current.get("export_sha256") != release["sha"]:
        notes.append("%s was pinned at %s; add moves the pin, as import update would" % (ns, _pin_text(current)))
    fresh = _direct_from_release(ns, release, stored)
    directs = _existing_directs(repo, old_lock, skip=[ns] if moves else []) + [fresh]
    plan = _plan(repo, old_lock, directs, parse_keep(keep), _active_decision(repo, override, hints), hints)
    return _run(repo, "add", ns, old_lock, plan, preview, notes, hints=hints)


def _ref_notes(ref: str, release: Dict[str, Any], where: str) -> List[str]:
    """A note when a release commit given as ``ref`` is pinned under its ``vN`` tag."""
    if release["ref"] == ref:
        return []
    return ["ref %s is the commit of release %s of %s; pinned as %s" % (ref, release["ref"], where, release["ref"])]


def update(repo: store.Repo, ns: str, ref: Optional[str], preview: bool = False, *, override: Optional[str] = None,
           keep: Any = None, mcp: bool = False) -> Dict[str, Any]:
    """Move the direct pin ``ns`` to ``ref`` of its ``from`` path (default: the newest ``vN`` tag), with the
    parents the new release bundles. The same commit vendors the file again (a repair). With ``preview`` only the
    diff is returned; bridges that would dangle or point at archived nodes are listed either way."""
    old_lock = lockfile.read(repo)
    old = _entries(old_lock).get(ns or "")
    if old is None:
        raise NotFound("%s is not imported" % ns, searched=ns)
    if old.get("via"):
        raise Refused("%s is bundled via %s; update %s, or add %s directly (with an override when the pins differ)"
                      % (ns, old["via"], old["via"], ns))
    root = _stored_root(repo, old.get("from"))
    if root is None:
        raise Refused("%s: its from path %s is not here; import it again with from" % (ns, old.get("from")))
    notes: List[str] = []
    if not ref:
        ref = newest_tag(root)
        if ref is None:
            raise Refused("%s has no release tag (vN)" % old.get("from"))
        notes.append("ref %s: the newest release of %s" % (ref, old.get("from")))
    hints = _Hints(mcp, {"action": "update", "ns": ns, "ref": ref})
    release = read_release(root, ref, str(old.get("from")), hints)
    notes += _ref_notes(ref, release, str(old.get("from")))
    hints.again["ref"] = release["ref"]
    if release["export"]["meta"].get("name") != old.get("name"):
        raise Refused("namespace collision: %s pins %s, but %s at %s is %s" % (
            ns, old.get("name"), old.get("from"), ref, release["export"]["meta"].get("name")))
    directs = _existing_directs(repo, old_lock, skip=[ns]) + [_direct_from_release(ns, release, str(old.get("from")))]
    plan = _plan(repo, old_lock, directs, parse_keep(keep), _active_decision(repo, override, hints), hints)
    return _run(repo, "update", ns, old_lock, plan, preview, notes, hints=hints)


def remove(repo: store.Repo, ns: str, preview: bool = False, *, override: Optional[str] = None, keep: Any = None,
           mcp: bool = False) -> Dict[str, Any]:
    """Drop the direct pin ``ns`` and the parents only it bundles. Refused while local bridges or provenance still
    refer to a namespace that would go (the problems are listed). When the pin goes, the parents other imports
    bundle may be left at two releases (the direct pin settled that with an override): ``override`` and ``keep``
    then say which one stays, as for ``add``."""
    old_lock = lockfile.read(repo)
    old = _entries(old_lock).get(ns or "")
    if old is None:
        raise NotFound("%s is not imported" % ns, searched=ns)
    if old.get("via"):
        raise Refused("%s is bundled via %s; remove %s instead" % (ns, old["via"], old["via"]))
    hints = _Hints(mcp, {"action": "remove", "ns": ns})
    directs = _existing_directs(repo, old_lock, skip=[ns])
    plan = _plan(repo, old_lock, directs, parse_keep(keep), _active_decision(repo, override, hints), hints)
    return _run(repo, "remove", ns, old_lock, plan, preview, [], refuse_problems=True, hints=hints)


def pin_status(repo: store.Repo) -> List[Dict[str, Any]]:
    """One item per pin: ``{ns, name, ref, commit7, via, from, ok, verdict, text, latest, override, nodes, edges,
    repair}``. ``ok`` is False when the vendored file differs from the lock. ``verdict``: ``current``, ``behind`` (a
    newer ``vN`` tag exists at ``from``), ``bundled`` (pinned by a parent's release), ``unknown`` (no ``from`` here)
    or ``mismatch``. ``repair`` is None, or for a mismatch the update that vendors the file again without moving any
    pin: ``{ns, ref}`` of the direct pin itself or, for a bundled pin, of the direct pin that bundles it (at that
    pin's own ref). Read-only git; nothing is fetched."""
    try:
        lock = lockfile.read(repo)
    except DataError:
        return []
    entries = _entries(lock)
    out: List[Dict[str, Any]] = []
    for ns, e in sorted(entries.items()):
        path = lockfile.export_path(repo, ns)
        try:
            ok = os.path.isfile(path) and store.file_sha256(path) == e.get("export_sha256")
        except OSError:
            ok = False
        ov = e.get("override") if isinstance(e.get("override"), dict) else None
        item: Dict[str, Any] = {
            "ns": ns, "name": e.get("name"), "ref": e.get("ref"), "commit7": _short(e.get("commit"), 7),
            "via": e.get("via"), "from": e.get("from"), "ok": ok, "verdict": "current", "text": "current",
            "latest": None, "override": (ov or {}).get("decision"), "nodes": e.get("nodes"), "edges": e.get("edges"),
            "repair": None,
        }
        if not ok:
            owner = str(e.get("via") or ns)
            ref = (entries.get(owner) or {}).get("ref")
            item.update(verdict="mismatch", repair={"ns": owner, "ref": ref},
                        text="the vendored export differs from the lock; restore it with git, or update %s at its "
                             "current ref %s" % (owner, ref or "-"))
        elif e.get("via"):
            item.update(verdict="bundled", text="pinned by the release of %s" % e["via"])
        else:
            item.update(_upstream(repo, e))
        out.append(item)
    return out


def _upstream(repo: store.Repo, e: Dict[str, Any]) -> Dict[str, Any]:
    root = _stored_root(repo, e.get("from"))
    if root is None:
        return {"verdict": "unknown", "text": "unknown: from %s is not here" % (e.get("from") or "(none)")}
    found = newest_release(root)  # this topic's own tags: the topics of one repo share the vN sequence
    if found is None:
        return {"verdict": "unknown", "text": "unknown: %s has no release tag" % e.get("from")}
    latest, latest_commit = found
    if latest_commit == e.get("commit"):
        return {"verdict": "current", "text": "current", "latest": latest}
    pinned_n = _tag_number(str(e.get("ref") or ""))
    newer = pinned_n is not None and (_tag_number(latest) or 0) > pinned_n
    if not newer and gitutil.SHA_RE.match(str(e.get("commit") or "")):
        ok, _out, _err = gitutil.git_ok(root, "merge-base", "--is-ancestor", str(e["commit"]), latest_commit)
        newer = ok
    if newer:
        return {"verdict": "behind", "latest": latest, "text": "behind: %s is released" % latest}
    return {"verdict": "current", "text": "current (pinned past %s)" % latest, "latest": latest}


def status(repo: store.Repo) -> Dict[str, Any]:
    """``{pins, bridges, proposals}``: the pin status, the count of local bridges per namespace pair, and the open
    proposals that cite another pin than the lock's or no longer apply (``_sync_proposals``, read only)."""
    onto = Ontology.load(repo)
    pairs: Dict[str, int] = {}
    for eid in onto.local_edge_ids:
        if eid in onto.bridges and onto.active(eid):
            edge = onto.edges[eid]
            key = "|".join(sorted((onto.ns_of(str(edge.get("src"))), onto.ns_of(str(edge.get("dst"))))))
            pairs[key] = pairs.get(key, 0) + 1
    try:
        _writes, report = _sync_proposals(repo, onto, lockfile.read(repo))
    except DataError:
        report = []
    return {"pins": pin_status(repo), "bridges": dict(sorted(pairs.items())), "proposals": report}


def bundle(repo: store.Repo) -> Dict[str, Any]:
    """The ``bundled`` object of this topic's export: ``{ns: {sha256, export}}`` for every lock entry, flattened
    (each export's own ``bundled`` is ``{}``). A vendored file that is missing or differs from the lock raises
    ``DataError``. The exports are shared with the load cache: do not change them."""
    lock = lockfile.read(repo)
    out: Dict[str, Any] = {}
    for ns, e in sorted(_entries(lock).items()):
        path = lockfile.export_path(repo, ns)
        if not os.path.isfile(path) or store.file_sha256(path) != e.get("export_sha256"):
            raise DataError("imports/%s/export.json is missing or differs from the lock; run onto validate" % ns)
        export = lockfile.read_export(repo, ns)
        flat = dict(export or {})
        flat["bundled"] = {}
        out[ns] = {"sha256": lockfile.bundle_sha(flat), "export": flat}
    return out


# bridge suggestion ---------------------------------------------------------------------------------------------
def _proposed_pairs(repo: store.Repo) -> Tuple[Dict[Tuple[str, str], str], Set[Tuple[str, str]]]:
    """(``same_as`` pairs in an open proposal -> its id, pairs rejected in a reviewed proposal). An op marked
    ``annot.stale`` (it could not apply at the pins of its time, see ``_sync_proposals``) counts as neither: its
    pair waits nowhere, and rejecting it judged nothing about the identity."""
    offered: Dict[Tuple[str, str], str] = {}
    rejected: Set[Tuple[str, str]] = set()
    for folder in (pipeline.PENDING, pipeline.DONE):
        root = repo.path(folder)
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            if not name.startswith("prop-") or not name.endswith(".json"):
                continue
            try:
                prop = store.read_json(os.path.join(root, name))
            except (DataError, OSError):
                continue
            if not isinstance(prop, dict):
                continue
            review = prop.get("review") if isinstance(prop.get("review"), dict) else {}
            verdicts = review.get("verdicts") or {}
            for op in prop.get("ops") or []:
                if not isinstance(op, dict) or op.get("op") != "add_edge":
                    continue
                edge = op.get("edge") or {}
                if edge.get("rel") != "same_as" or (op.get("annot") or {}).get("stale"):
                    continue
                a, b = str(edge.get("src") or ""), str(edge.get("dst") or "")
                pair = (min(a, b), max(a, b))
                if prop.get("status") in pipeline.OPEN:
                    offered.setdefault(pair, str(prop.get("id")))
                elif prop.get("status") == "rejected" or verdicts.get(str(op.get("n"))) == "reject":
                    rejected.add(pair)
    return offered, rejected


def candidates(repo: store.Repo, a: Optional[str], b: str) -> Dict[str, Any]:
    """``{pairs, skipped, offered, sides}``: the ``same_as`` candidates between namespace ``a`` (default: every
    other namespace, ``self`` included) and ``b``, best first. Pairs already linked, in one ``same_as`` class or
    rejected before are skipped (counted); pairs waiting in an open proposal are left out and named in
    ``offered`` (``{proposal id: count}``)."""
    if not b:
        raise UsageError("suggest needs with: the other namespace")
    if a == b:
        raise UsageError("suggest pairs two different namespaces; got %s twice" % b)
    onto = Ontology.load(repo)
    known = {str(e.get("ns")) for e in onto.imports}
    for side in [x for x in (a, b) if x]:
        if side != "self" and side not in known:
            raise NotFound("%s is not imported (imports: %s)" % (side, ", ".join(sorted(known)) or "none"),
                           searched=side)
    sides = [a] if a else [s for s in sorted(known) + ["self"] if s != b]  # never a namespace with itself
    if not sides:
        raise UsageError("suggest pairs %s with another namespace, and there is none (imports: none)" % b)
    open_pairs, rejected = _proposed_pairs(repo)
    best: Dict[Tuple[str, str], Dict[str, Any]] = {}
    skipped: Set[Tuple[str, str]] = set()
    offered: Dict[str, Set[Tuple[str, str]]] = {}
    for side in sides:
        for pair in entities.cross(onto, side, b):
            lo, hi = sorted((pair["a"], pair["b"]))
            if lo == hi:  # a node is never its own bridge
                continue
            key = (lo, hi)
            if key in open_pairs:
                offered.setdefault(open_pairs[key], set()).add(key)
                continue
            same_class = onto.same_as.get(lo) is not None and onto.same_as.get(lo) == onto.same_as.get(hi)
            linked = same_class or any(x["other"] == hi for x in onto.edges_of(lo, include_archived=True))
            if linked or key in rejected or not onto.registry.allowed("same_as", onto.kind_of(lo), onto.kind_of(hi)):
                skipped.add(key)
                continue
            cur = best.get(key)
            if cur is None or pair["score"] > cur["score"]:
                best[key] = {"a": lo, "b": hi, "score": pair["score"], "why": pair["why"],
                             "a_flags": render.flags(onto.node(lo)), "b_flags": render.flags(onto.node(hi))}
    pairs = sorted(best.values(), key=lambda p: (-p["score"], p["a"], p["b"]))
    return {"pairs": pairs, "skipped": len(skipped), "offered": {k: len(v) for k, v in sorted(offered.items())},
            "sides": sides}


def _bridge_op(onto: Ontology, pair: Dict[str, Any]) -> Dict[str, Any]:
    prov = []
    for end in (pair["a"], pair["b"]):
        ns = onto.ns_of(end)
        if ns == "self":
            continue
        entry = next((e for e in onto.imports if e.get("ns") == ns), None)
        if entry and entry.get("commit"):
            prov.append({"src": ids.impsrc(ns, str(entry["commit"])), "loc": end, "by": "agent"})
    return {"op": "add_edge", "edge": {"src": pair["a"], "rel": "same_as", "dst": pair["b"], "key": ""},
            "conf": float(pair["score"]), "basis": "inferred", "prov": prov}


def _dead_bridge_proposals(repo: store.Repo) -> List[str]:
    """Open proposals, oldest first, whose every op is a ``same_as`` bridge marked stale: nothing in them can apply
    at the current pins, so a new suggestion replaces (supersedes) one."""
    out = []
    for _path, prop in _open_proposal_files(repo):
        ops = [op for op in prop.get("ops") or [] if isinstance(op, dict)]
        if ops and all(op.get("op") == "add_edge" and (op.get("edge") or {}).get("rel") == "same_as"
                       and (op.get("annot") or {}).get("stale") for op in ops):
            out.append((str(prop.get("created") or ""), str(prop["id"])))
    return [pid for _created, pid in sorted(out)]


def _suggest(repo: store.Repo, a: Optional[str], b: str) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    found = candidates(repo, a, b)  # checks the arguments before anything is written
    # a pin moved by git (a merge or checkout of imports/lock.json) leaves open proposals citing another pin: bring
    # them in step first, so the pairs they hold count as offered only when they can still apply
    synced = sync_proposals(repo)
    if any(r["changed"] for r in synced):
        found = candidates(repo, a, b)
    found["synced"] = synced
    found["supersedes"] = None
    pairs = found["pairs"][:MAX_SUGGEST]
    if not pairs:
        return None, found
    onto = Ontology.load(repo)
    draft: Dict[str, Any] = {
        "by": "agent",
        "summary": "Bridge suggestions between %s and %s: %d same_as candidate%s%s" % (
            b, ", ".join(found["sides"]), len(pairs), "" if len(pairs) == 1 else "s",
            " (%d more not offered)" % (len(found["pairs"]) - len(pairs)) if len(found["pairs"]) > len(pairs) else ""),
        "ops": [_bridge_op(onto, p) for p in pairs],
    }
    dead = _dead_bridge_proposals(repo)
    if dead:
        draft["supersedes"] = found["supersedes"] = dead[0]
    return pipeline.prepare(repo, draft, by="agent"), found


def suggest(repo: store.Repo, a: Optional[str], b: str) -> Optional[Dict[str, Any]]:
    """Save a pending proposal of ``same_as`` bridges between namespaces ``a`` and ``b`` (see ``candidates``) and
    return it; None when there is nothing new to offer. Each op cites the pinned exports (``imp:<ns>@<commit12>``,
    ``loc`` the qualified id) and carries the match score as ``conf``. The open proposals are first brought in step
    with the pins (``sync_proposals``), and the new proposal supersedes the oldest open one whose bridges all went
    stale."""
    proposal, _found = _suggest(repo, a, b)
    return proposal


# the command ---------------------------------------------------------------------------------------------------
def _empty(action: str, ns: Optional[str]) -> Dict[str, Any]:
    return {"action": action, "ns": ns, "written": False, "change": None, "lock_diff": [],
            "node_diff": {"added": [], "removed": [], "changed": []},
            "bridges_affected": {"dangling": [], "archived": []}, "problems": [], "pins": [], "proposal": None,
            "proposals": [], "notes": []}


def cmd_import(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    action = args.get("action")
    if action not in ACTIONS:
        raise UsageError("import action must be one of %s" % ", ".join(ACTIONS))
    repo = ctx.repo
    ns = args.get("ns") or None
    if action in ("status", "suggest"):
        ctx.preview = False  # read, or a pending proposal only: no confirm gate
    if action == "status":
        result = _empty(action, ns)
        found = status(repo)
        pins = found["pins"]
        result["pins"] = [p for p in pins if not ns or p["ns"] == ns or p.get("via") == ns]
        result["bridges"] = found["bridges"]
        if ns and not result["pins"]:
            raise NotFound("%s is not imported" % ns, searched=ns)
        by_ns = {p["ns"]: p for p in pins}
        for item in found["proposals"]:
            # the update that re-cites: the direct pin at its own ref (a bundled pin moves with its bundler)
            owners = sorted({str((by_ns.get(x["ns"]) or {}).get("via") or x["ns"]) for x in item["recited"]})
            item["fix"] = [{"ns": o, "ref": (by_ns.get(o) or {}).get("ref")} for o in owners]
        result["proposals"] = found["proposals"]
        return result
    if action == "suggest":
        other = args.get("with")
        proposal, found = _suggest(repo, ns, other)
        result = _empty(action, ns)
        result["with"] = other
        result["candidates"] = found["pairs"][:MAX_SUGGEST]
        result["totals"] = {"candidates": len(found["pairs"]), "skipped": found["skipped"]}
        result["offered"] = found["offered"]
        result["sides"] = found["sides"]
        result["proposals"] = found["synced"]
        if proposal is not None:
            result["proposal"] = {"id": proposal["id"], "priority": proposal.get("priority"),
                                  "summary": proposal.get("summary"), "ops": len(proposal.get("ops") or []),
                                  "note": proposal.get("note"), "supersedes": proposal.get("supersedes")}
        else:
            result["notes"].append("no new same_as candidates between %s and %s" % (
                other, ", ".join(found["sides"])))
        result["notes"] += _sync_notes([r for r in found["synced"] if r["id"] != (proposal or {}).get("supersedes")],
                                       True, _Hints(ctx.mcp))
        if proposal is not None and proposal.get("supersedes"):
            result["notes"].append("%s replaces %s, whose bridges no longer apply at the current pins (superseded, "
                                   "not rejected)" % (proposal["id"], proposal["supersedes"]))
        return result
    preview = bool(ctx.preview)
    choice = {"override": args.get("override"), "keep": args.get("keep"), "mcp": bool(ctx.mcp)}
    if action == "add":
        return add(repo, ns, args.get("from"), args.get("ref"), preview=preview, **choice)
    if not ns:
        raise UsageError("import %s needs ns" % action)
    if action == "update":
        return update(repo, ns, args.get("ref"), preview=preview, **choice)
    return remove(repo, ns, preview=preview, **choice)


# rendering -----------------------------------------------------------------------------------------------------
def _ids_line(label: str, items: Sequence[str], cap: int) -> str:
    shown = list(items[:cap])
    tail = render.more(len(items), len(shown))
    return "%s %d%s%s" % (label, len(items), ": " + ", ".join(shown) if shown else "", "; " + tail if tail else "")


def _edge_text(item: Dict[str, Any]) -> str:
    """``[untrusted] e:... src -rel-> dst (draft)``: an affected bridge with its C.7 markers, ``(bridge of <ns>)``
    when an import brings it, and ``now <ids>`` when its archived end names a replacement."""
    text = render.mark(item, "%s %s -%s-> %s" % (item.get("edge"), item.get("src"), item.get("rel"), item.get("dst")))
    if item.get("inherited"):
        text += " (bridge of %s)" % item.get("owner")
    if item.get("now"):
        text += " now %s" % ", ".join(item["now"])
    return text


def _pin_line(p: Dict[str, Any]) -> str:
    parts = ["%s %s %s" % (p.get("ns"), p.get("ref") or "-", p.get("commit7") or "-"),
             "ok" if p.get("ok") else "mismatch"]
    if p.get("verdict") not in ("mismatch",):
        parts.append(str(p.get("text") or p.get("verdict")))
    if p.get("from") and not p.get("via"):
        parts.append("from %s" % p["from"])
    if p.get("override"):
        parts.append("override %s" % p["override"])
    return " | ".join(parts)


def render_import(result: Dict[str, Any], mode: str, ctx: Any) -> List[str]:
    cap = SHOW if mode == "compact" else SHOW_TEXT
    action = result.get("action")
    lines: List[str] = []
    if action == "status":
        pins = result.get("pins") or []
        if not pins:
            lines.append("imports: none (to add one: %s)" % _Hints(ctx.mcp).call(
                "import", action="add", **{"from": "<released topic repo>"}))
        for p in pins:
            lines.append(_pin_line(p))
        bridges = result.get("bridges") or {}
        if bridges:
            lines.append("bridges: " + ", ".join("%s %d" % kv for kv in sorted(bridges.items())))
        fixes: List[str] = []
        for item in (result.get("proposals") or [])[:cap]:
            if item.get("recited"):
                calls = [ctx.call("import", action="update", ns=f["ns"], ref=f["ref"])
                         for f in item.get("fix") or [] if f.get("ref")]
                fixes += [c for c in calls if c not in fixes]
                ops = sorted({x["n"] for x in item["recited"]})
                lines.append("proposal %s: op%s %s cite%s another pin of %s than the lock%s" % (
                    item["id"], "s" if len(ops) > 1 else "", ", ".join(str(n) for n in ops),
                    "" if len(ops) > 1 else "s", _and(sorted({x["ns"] for x in item["recited"]})),
                    "; %s re-cites %s" % (" and ".join(calls), "them" if len(ops) > 1 else "it") if calls else ""))
            if item.get("stale"):
                lines.append("proposal %s: op%s %s no longer appl%s at the current pins (%s); %s" % (
                    item["id"], "s" if len(item["stale"]) > 1 else "",
                    ", ".join(str(s["n"]) for s in item["stale"]), "y" if len(item["stale"]) > 1 else "ies",
                    item["stale"][0]["why"], ctx.call("review", id=item["id"])))
        broken = [p for p in pins if p.get("verdict") == "mismatch"]
        behind = [p for p in pins if p.get("verdict") == "behind"]
        if broken:
            first = broken[0]
            # a bundled pin is repaired by its bundler at the bundler's own ref, never at the bundled pin's ref
            repair = first.get("repair") or (None if first.get("via") else {"ns": first["ns"], "ref": first.get("ref")})
            fix = ", or %s" % ctx.call("import", action="update", ns=repair["ns"], ref=repair.get("ref")) \
                if repair and repair.get("ref") else ""
            lines.append("Next: restore imports/%s/export.json with git%s" % (first["ns"], fix))
        elif behind:
            lines.append("Next: %s (%s is released)" % (ctx.call("import", action="update", ns=behind[0]["ns"]),
                                                        behind[0].get("latest")))
        elif fixes:
            lines.append("Next: %s (re-cites the open proposals to the current pin)" % fixes[0])
        elif len({p["ns"] for p in pins}) >= 2 and not bridges:
            lines.append("Next: %s" % ctx.call("import", action="suggest", **{"with": pins[-1]["ns"]}))
    elif action == "suggest":
        prop = result.get("proposal")
        totals = result.get("totals") or {}
        pairs = result.get("candidates") or []
        sides = ", ".join(result.get("sides") or [])
        if prop:
            lines.append("bridges %s <-> %s: proposal %s, %d same_as candidate%s, priority %s%s" % (
                result.get("with"), sides, prop["id"], prop["ops"], "" if prop["ops"] == 1 else "s",
                prop.get("priority"), " (%s)" % prop["note"] if prop.get("note") else ""))
        for n, p in enumerate(pairs[:cap], start=1):
            lines.append("%d %s =same_as= %s %.2f %s" % (n, render.mark(p.get("a_flags") or {}, p["a"]),
                                                         render.mark(p.get("b_flags") or {}, p["b"]), p["score"],
                                                         p["why"]))
        tail = render.more(len(pairs), min(len(pairs), cap))
        if tail:
            lines.append(tail)
        if totals.get("skipped"):
            lines.append("skipped %d pair(s) already linked, merged or rejected" % totals["skipped"])
        for prop_id, count in sorted((result.get("offered") or {}).items()):
            lines.append("waiting in %s: %d pair(s) (%s)" % (prop_id, count, ctx.call("review", id=prop_id)))
        if prop:
            lines.append("Next: %s, then apply it with your verdicts" % ctx.call("review", id=prop["id"]))
    else:
        head = "import %s %s: %s" % (action, result.get("ns") or "", "written %s" % result["change"]
                                     if result.get("written") else ("nothing to change" if not result.get("lock_diff")
                                                                     else "preview"))
        lines.append(head)
        for item in result.get("lock_diff") or []:
            if item["change"] == "added":
                lines.append("+ %s %s" % (item["ns"], _pin_text(item["after"])))
            elif item["change"] == "removed":
                lines.append("- %s %s" % (item["ns"], _pin_text(item["before"])))
            else:
                ov = (item.get("after") or {}).get("override")
                lines.append("~ %s %s -> %s%s" % (item["ns"], _pin_text(item["before"]), _pin_text(item["after"]),
                                                  " (override %s)" % ov if ov else ""))
        nd = result.get("node_diff") or {}
        if any(nd.get(k) for k in ("added", "removed", "changed")):
            lines.append("nodes: " + "; ".join(_ids_line(k, nd.get(k) or [], cap) for k in ("added", "removed",
                                                                                             "changed")))
        ba = result.get("bridges_affected") or {}
        for key in ("dangling", "archived"):
            items = ba.get(key) or []
            if items:
                shown = items[:cap]
                tail = render.more(len(items), len(shown))
                lines.append("bridges %s %d: %s%s" % (key, len(items), "; ".join(_edge_text(i) for i in shown),
                                                      "; " + tail if tail else ""))
        for key in ("new", "changed"):
            items = (result.get("conflicts") or {}).get(key) or []
            if items:
                shown = items[:cap]
                tail = render.more(len(items), len(shown))
                lines.append("conflicts %s %d: %s%s; %s asks which value holds" % (
                    key, len(items), "; ".join("%s %s" % (c["class"], c["field"]) for c in shown),
                    "; " + tail if tail else "", ctx.call("next")))
        problems = result.get("problems") or []
        for text in problems[:cap]:
            lines.append("problem after: %s" % render.trunc(text, 200))
        if len(problems) > cap:
            lines.append("problems after: %s" % render.more(len(problems), cap))
        pins = result.get("pins") or []
        if pins and mode == "text":
            for p in pins:
                lines.append("pin: %s" % _pin_line(p))
        if result.get("written") and action == "add" and len(pins) >= 2:
            lines.append("Next: %s" % ctx.call("import", action="suggest", **{"with": result.get("ns")}))
    for note in result.get("notes") or []:
        lines.append("note: %s" % note)
    return lines


__all__ = ["add", "update", "remove", "status", "pin_status", "suggest", "candidates", "bundle", "read_release",
           "newest_tag", "newest_release", "release_tags", "topic_prefix", "sync_proposals", "parse_keep", "cmd_import",
           "render_import"]
