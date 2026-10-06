"""The import lock (``imports/lock.json``) and the vendored exports (``imports/<ns>/export.json``).

A lock entry pins one import by commit and by the sha256 of its vendored file. A direct entry's vendored file is the
upstream ``build/export.json`` bytes; a ``via`` entry (a parent bundled inside a direct import) is the canonical
bytes of its bundle object. ``export_sha256`` is always the sha256 of the vendored file.

``verify`` returns the P15 problems: a vendored file that differs from the lock or is not canonical, a bundled sha
that does not match its object, a namespace collision, and a pin conflict (the same topic at two shas) without an
active override decision. Nothing here writes except ``write``.
"""

from __future__ import annotations

import glob
import os
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from . import FORMAT, records, store, util
from .errors import DataError, Problem

LOCK = "imports/lock.json"
EXPORT = "imports/%s/export.json"

_PARSED: Dict[str, Tuple[str, Dict[str, Any]]] = {}  # real path -> (sha256, parsed export)


def empty() -> Dict[str, Any]:
    return {"format": FORMAT, "imports": []}


def read(repo: store.Repo) -> Dict[str, Any]:
    """The lock, or ``{"format": 1, "imports": []}`` when absent. Raises ``DataError`` when unreadable."""
    value = store.read_json(repo.path(LOCK), None)
    if value is None:
        return empty()
    if not isinstance(value, dict):
        raise DataError("%s: not a JSON object" % LOCK, file=LOCK)
    return value


def write(repo: store.Repo, lock: Dict[str, Any]) -> None:
    """Write the lock canonically, entries sorted by ``ns``."""
    out = dict(lock)
    out.setdefault("format", FORMAT)
    out["imports"] = sorted((dict(e) for e in lock.get("imports") or []), key=lambda e: str(e.get("ns") or ""))
    store.write_json(repo.path(LOCK), out)


def entries(lock: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [e for e in lock.get("imports") or [] if isinstance(e, dict)]


def entry(lock: Dict[str, Any], ns: str) -> Optional[Dict[str, Any]]:
    """The lock entry for ``ns``, or None."""
    for e in entries(lock):
        if e.get("ns") == ns:
            return e
    return None


def export_path(repo: store.Repo, ns: str) -> str:
    return repo.path(EXPORT % ns)


def read_export(repo: store.Repo, ns: str) -> Optional[Dict[str, Any]]:
    """The parsed vendored export of ``ns`` (``bundled`` included), cached by file sha; None when absent.
    Raises ``DataError`` when it is not a JSON object."""
    path = export_path(repo, ns)
    if not os.path.isfile(path):
        return None
    key = os.path.realpath(path)
    digest = store.file_sha256(path)
    hit = _PARSED.get(key)
    if hit and hit[0] == digest:
        return hit[1]
    value = store.read_json(path)
    if not isinstance(value, dict):
        raise DataError("%s: not a JSON object" % (EXPORT % ns), file=EXPORT % ns)
    _PARSED[key] = (digest, value)
    return value


def vendored(repo: store.Repo) -> Dict[str, Dict[str, Any]]:
    """``{ns: export}`` for every lock entry whose vendored file parses; the ``bundled`` key is left out. Entries
    whose file is missing or unreadable are skipped (``verify`` reports them)."""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        lock = read(repo)
    except DataError:
        return out
    for e in entries(lock):
        ns = e.get("ns")
        if not isinstance(ns, str) or not ns:
            continue
        try:
            value = read_export(repo, ns)
        except DataError:
            continue
        if value is None:
            continue
        out[ns] = {k: v for k, v in value.items() if k != "bundled"}
    return out


def bundle_sha(export: Dict[str, Any]) -> str:
    """The sha256 of a bundle object's canonical bytes."""
    return util.sha256_hex(util.canonical_bytes(export))


def _active_decisions(repo: store.Repo) -> Set[str]:
    """Ids of the active decisions on disk (read directly: the ledger is a peer module)."""
    found: Set[str] = set()
    for path in glob.glob(os.path.join(repo.path("ledger/decisions"), "dec-*.json")):
        try:
            value = store.read_json(path)
        except (DataError, OSError):
            continue
        if isinstance(value, dict) and value.get("status") == "active" and isinstance(value.get("id"), str):
            found.add(value["id"])
    return found


def verify(repo: store.Repo, lock: Optional[Dict[str, Any]] = None,
           active_decisions: Optional[Iterable[str]] = None) -> List[Problem]:
    """P15 problems for the lock and its vendored files (P01 when the lock itself is unreadable)."""
    problems: List[Problem] = []
    if lock is None:
        try:
            lock = read(repo)
        except DataError as exc:
            return [Problem("P01", LOCK, 0, str(exc))]
    for message in records.check(lock, "lock"):
        problems.append(Problem("P15", LOCK, 0, "lock: %s" % message))
    active = set(active_decisions) if active_decisions is not None else _active_decisions(repo)
    own_ns = repo.ns
    seen: Dict[str, Dict[str, Any]] = {}
    by_name: Dict[str, Tuple[str, str]] = {}
    for e in entries(lock):
        ns = str(e.get("ns") or "")
        if not ns:
            continue
        if ns == own_ns:
            problems.append(Problem("P15", LOCK, 0, "import %s: its namespace is this topic's own ns" % ns))
        if ns in seen:
            problems.append(Problem("P15", LOCK, 0, "import %s: namespace listed twice" % ns))
            continue
        seen[ns] = e
        name = str(e.get("name") or "")
        if name in by_name and by_name[name][0] != ns:
            problems.append(
                Problem("P15", LOCK, 0, "imports %s and %s are the same topic %s" % (by_name[name][0], ns, name))
            )
        by_name.setdefault(name, (ns, str(e.get("export_sha256") or "")))
        override = e.get("override")
        if isinstance(override, dict):
            if override.get("decision") not in active:
                problems.append(
                    Problem("P15", LOCK, 0, "import %s: override decision %s is unknown or not active"
                            % (ns, override.get("decision")))
                )
            if override.get("kept") != e.get("export_sha256"):
                problems.append(Problem("P15", LOCK, 0, "import %s: override keeps another sha than the lock" % ns))
        if e.get("via") and e.get("via") not in {str(x.get("ns")) for x in entries(lock)}:
            problems.append(Problem("P15", LOCK, 0, "import %s: bundled via %s, which is not imported" % (ns, e["via"])))
        problems.extend(_verify_file(repo, ns, e))
    problems.extend(_pin_conflicts(repo, lock, seen, active))
    for path in sorted(glob.glob(os.path.join(repo.path("imports"), "*", "export.json"))):
        ns = os.path.basename(os.path.dirname(path))
        if ns not in seen:
            problems.append(Problem("P15", EXPORT % ns, 0, "vendored export with no lock entry"))
    return problems


def _verify_file(repo: store.Repo, ns: str, e: Dict[str, Any]) -> List[Problem]:
    rel = EXPORT % ns
    path = repo.path(rel)
    if not os.path.isfile(path):
        return [Problem("P15", rel, 0, "vendored export is missing")]
    out: List[Problem] = []
    if store.file_sha256(path) != e.get("export_sha256"):
        out.append(Problem("P15", rel, 0, "sha256 differs from the lock (the vendored file was changed)"))
    try:
        value = read_export(repo, ns)
    except DataError as exc:
        return out + [Problem("P15", rel, 0, str(exc))]
    with open(path, "rb") as fh:
        data = fh.read()
    if value is not None and data != util.canonical_bytes(value):
        out.append(Problem("P15", rel, 0, "vendored export is not in canonical form"))
    export_format = ((value or {}).get("meta") or {}).get("format")
    if isinstance(export_format, int) and export_format > FORMAT:
        out.append(Problem("P15", rel, 0, "export format %d is newer than this kit reads; upgrade the kit"
                           % export_format))
    for bns, bundle in sorted(((value or {}).get("bundled") or {}).items()):
        if not isinstance(bundle, dict) or not isinstance(bundle.get("export"), dict):
            out.append(Problem("P15", rel, 0, "bundled %s: not an object with export" % bns))
            continue
        if bundle_sha(bundle["export"]) != bundle.get("sha256"):
            out.append(Problem("P15", rel, 0, "bundled %s: sha256 does not match its object" % bns))
    return out


def _same_release(repo: store.Repo, ns: str, sha: Any) -> bool:
    """True when ``sha`` is the flattened bundle sha of the export pinned under ``ns``."""
    try:
        pinned = read_export(repo, ns)
    except DataError:
        return False
    if not isinstance(pinned, dict):
        return False
    return bundle_sha(dict(pinned, bundled={})) == sha


def _pin_conflicts(repo: store.Repo, lock: Dict[str, Any], seen: Dict[str, Dict[str, Any]],
                   active: Set[str]) -> List[Problem]:
    """A bundled parent whose sha differs from the lock's pin for that ns is a pin conflict, allowed only when the
    lock entry's override names an active decision and lists the other sha as dropped."""
    out: List[Problem] = []
    for ns in sorted(seen):
        try:
            value = read_export(repo, ns)
        except DataError:
            continue
        for bns, bundle in sorted(((value or {}).get("bundled") or {}).items()):
            pinned = seen.get(bns)
            if pinned is None or not isinstance(bundle, dict):
                continue
            sha = bundle.get("sha256")
            if sha == pinned.get("export_sha256"):
                continue
            if _same_release(repo, bns, sha):
                continue  # one release in two byte forms: the pinned upstream file and its flattened bundle
            override = pinned.get("override") if isinstance(pinned.get("override"), dict) else {}
            if override.get("decision") in active and sha in (override.get("dropped") or []):
                continue
            out.append(
                Problem("P15", LOCK, 0, "pin conflict: %s bundles %s at another sha than the lock pins; record a "
                        "decision and import with an override" % (ns, bns))
            )
    return out


def counts(export: Dict[str, Any]) -> Dict[str, int]:
    return {"nodes": len(export.get("nodes") or []), "edges": len(export.get("edges") or [])}


def label(e: Dict[str, Any]) -> str:
    """``garden v1`` for a lock entry."""
    return "%s %s" % (e.get("ns"), e.get("ref") or str(e.get("commit") or "")[:7])


def clear_cache() -> None:
    _PARSED.clear()
