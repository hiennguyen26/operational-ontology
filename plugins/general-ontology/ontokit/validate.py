"""Validation: every problem (P01 to P23) and warning (W01 to W10) of a topic repo, collected, never stopping at the
first. W08 repeats the note a crash recovery left (``store.recovery_note``: a log it did not cut back because lines
added since sit after the interrupted write's own line); ``--fix`` reports it once more and clears it.

``validate(repo, fix=False)`` returns a ``Report(problems, warnings, summary)``. Problems print as
``file:line: CODE message``. ``fix`` rewrites only what is marked fixable: files not sorted or not canonical (P04),
byte-identical duplicate lines (P05), and two P06 cases. Index lines of one source that hold the same text (the
same ``id`` and ``sha256``, as a union merge of two branches that ingested the same text leaves them) collapse into
one line, the earliest ``captured_at`` kept (an erased copy wins, so an erasure is never undone). A local edge under
an imported edge's id is dropped: imports are read-only and the import holds that edge. Everything else is reported
for a person to decide.

``check_graph(onto_like)`` runs the record checks (P02, P06 to P13) on a graph object, so a writer can check a
result in memory before writing it. ``touched`` limits the per-record checks to the records a write changes; the
checks between records (duplicates, dangling ends, supersession) always cover the whole graph. A ``superseded_by``
chain (merge A into B, then B into C) is followed to its end: it is fine while it ends at an active record, an erased
one, or one archived by a decision with nothing superseding it.

Personal data (P18) is checked with ``sanitize.check_text`` when that module is built, in the same mode ingest
uses, so anything validate accepts, export accepts too: in the stored source texts, and in the free text of the
graph records and the proposal files (``records.text_slots``), in the release notes of ``VERSIONS.md`` (the Notes
cell) and in the free text of ``ledger/changes.jsonl`` (summary, done, next, open_questions). Control characters in
the free text of records are P02.

With the opt-in ``assessment`` pack listed, ``assessment.calibration`` checks the risk ratings: P23 for a risk whose
status is reviewed or approved, W09 for a draft one (so drafts never block a build). ``calibration_problems(onto)``
gives the P23s alone, so a writer refuses a change that would add one (a reviewed risk rated past its controls,
rated without all three measures of a dimension, or not rated at all, or a control it relies on archived). W10 flags an active decision that narrows a superseded one.
"""

from __future__ import annotations

import glob
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from . import (FORMAT, __version__, assessment, ids, ledger, lockfile, needs, packs, records, secrets, sources, store,
               util)
from .errors import DataError, Problem
from .graph import EDGES, NODES, SOURCES, Ontology, clear_cache

__all__ = ["Problem", "Report", "validate", "check_graph", "MAX_LINES"]

MAX_LINES = 200
APPEND_ONLY = (
    ("interview/log.jsonl", "answer"),
    ("ledger/changes.jsonl", "change"),
    ("metrics/history.jsonl", "point"),
)
SORTED_JSONL = (NODES, EDGES, SOURCES)
# History points carry no id: two equal measurements (A, B, A) are two points, and with a fixed clock their lines
# are byte-identical, so P05 (and its fix) never applies to this file.
REPEATABLE_LINES = ("metrics/history.jsonl",)
PROPOSAL_FOLDERS = {"pending": ("pending", "reviewed"), "done": ("applied", "rejected", "superseded")}
_TEXT_RE = re.compile(r"^sources/(src-[0-9a-f]{12})\.txt$")
_ORIG_RE = re.compile(r"^sources/(src-[0-9a-f]{12})\.orig\.[a-z0-9]{1,8}$")


class Report(object):
    """The outcome of ``validate``: ``problems`` and ``warnings`` (``Problem`` records) and a ``summary`` dict."""

    def __init__(self, problems: List[Problem], warnings: List[Problem], summary: Dict[str, Any]) -> None:
        self.problems = problems
        self.warnings = warnings
        self.summary = summary

    @property
    def ok(self) -> bool:
        return not self.problems

    def ok_line(self) -> str:
        s = self.summary
        return "ok: %d nodes, %d edges, %d sources, %d imports, richness %s" % (
            s.get("nodes", 0), s.get("edges", 0), s.get("sources", 0), s.get("imports", 0),
            "n/a" if s.get("richness") is None else s.get("richness"))

    def lines(self, limit: int = MAX_LINES) -> List[str]:
        """Problems (up to ``limit`` then ``+N more``); with none, the warnings then the ok line."""
        if self.problems:
            shown = [p.text() for p in self.problems[:limit]]
            if len(self.problems) > limit:
                shown.append("+%d more" % (len(self.problems) - limit))
            return shown
        shown = [p.text() for p in self.warnings[:limit]]
        if len(self.warnings) > limit:
            shown.append("+%d more" % (len(self.warnings) - limit))
        return shown + [self.ok_line()]

    def to_json(self) -> Dict[str, Any]:
        return {
            "problems": [p.to_json() for p in self.problems],
            "warnings": [p.to_json() for p in self.warnings],
            "summary": dict(self.summary),
        }


def _optional(name: str) -> Any:
    """A package module when it is built, else None (the lazy peer rule; ``commands.optional`` sits higher). A
    module that exists but fails to import raises."""
    return util.optional_module(name)


def _sort(problems: Iterable[Problem]) -> List[Problem]:
    unique = {}
    for p in problems:
        unique.setdefault((p.code, p.file, p.line, p.message), p)
    return sorted(unique.values(), key=lambda p: (p.code, str(p.file), p.line, p.message))


# the record checks ---------------------------------------------------------------------------------------------
def _where(onto: Any, rid: str) -> Tuple[str, int]:
    return onto.lines.get(rid, (NODES if ids.is_local(rid) or ids.is_qualified(rid) else EDGES, 0))


def calibration_problems(onto: Any) -> List[Problem]:
    """The P23 problems of ``assessment.calibration`` (none when the pack is off): a writer compares them before
    and after a change, so only a new P23 refuses it."""
    return assessment.calibration(onto, lambda rid: _where(onto, rid))[0]


def _decisions(onto: Any) -> Dict[str, Dict[str, Any]]:
    cached = onto._cache.get("decisions")
    if cached is None:
        cached = ledger.all_decisions(onto.repo) if onto.repo is not None else {}
        onto._cache["decisions"] = cached
    return cached


def _impsrcs(onto: Any) -> Set[str]:
    return {ids.impsrc(str(e["ns"]), str(e.get("commit") or "")) for e in onto.imports if e.get("commit")}


def _imported_impsrc(onto: Any, src: str) -> bool:
    """An ``imp:<ns>@<12hex>`` of a namespace that is still imported, at any commit: ``import update`` moves the pin,
    and no op can re-cite the bridges that cite the old commit (new proposals must cite the current pin)."""
    m = ids.IMPSRC_RE.match(src)
    return bool(m) and any(e.get("ns") == m.group(1) for e in onto.imports)


def _decision_resolves(decisions: Dict[str, Dict[str, Any]], dec_id: str) -> bool:
    """A superseded decision whose ``superseded_by`` chain ends at a known decision (no loop, no gap)."""
    seen: Set[str] = set()
    cur: Any = dec_id
    while isinstance(cur, str) and cur not in seen:
        seen.add(cur)
        found = decisions.get(cur)
        if found is None:
            return False
        if found.get("status") == "active":
            return True
        cur = found.get("superseded_by")
    return False


def _archive_problems(onto: Any, rid: str, rec: Dict[str, Any], file: str, line: int) -> List[Problem]:
    out: List[Problem] = []
    block = rec.get("archived")
    status = rec.get("status")
    if status != "archived":
        if block is not None:
            out.append(Problem("P12", file, line, "%s: an archive block on a record that is %s" % (rid, status)))
        return out
    if not isinstance(block, dict):
        return [Problem("P12", file, line, "%s: archived without an archive block (on, reason, decision, "
                                           "superseded_by)" % rid)]
    if len(str(block.get("reason") or "").strip()) < 20:
        out.append(Problem("P12", file, line, "%s: archived.reason must be 20 or more characters" % rid))
    decision = block.get("decision")
    superseded = block.get("superseded_by") or []
    if decision is None:
        if not superseded:
            out.append(Problem("P12", file, line, "%s: archived.decision is required unless a merge supersedes it"
                               % rid))
    else:
        found = _decisions(onto).get(decision)
        if found is None:
            out.append(Problem("P12", file, line, "%s: archived.decision %s is not in the ledger" % (rid, decision)))
        elif found.get("status") != "active" and not _decision_resolves(_decisions(onto), decision):
            # "active" is checked when the archive is applied (pipeline, mutate, erase); a decision superseded
            # later is fine while its chain resolves, since an archived record is final and cannot be re-cited
            out.append(Problem("P12", file, line, "%s: archived.decision %s is not active (superseded by %s)"
                               % (rid, decision, found.get("superseded_by"))))
    return out


def _chain_ends(onto: Any, ref: str, seen: Set[str]) -> bool:
    """True when following ``superseded_by`` from ``ref`` reaches an active record, an erased one, or one archived by
    a decision with nothing superseding it (a deliberate end). A missing record or a loop is a dead end."""
    if ref in seen:
        return False
    seen.add(ref)
    rec = onto.record(ref)
    if rec is None:
        return False
    if rec.get("status") != "archived" or rec.get("erased") is True:
        return True
    block = rec.get("archived") if isinstance(rec.get("archived"), dict) else {}
    nxt = [str(s) for s in block.get("superseded_by") or []]
    if not nxt:
        return block.get("decision") is not None
    return any(_chain_ends(onto, s, seen) for s in nxt)


def _supersession_problems(onto: Any, rid: str, rec: Dict[str, Any], file: str, line: int) -> List[Problem]:
    """P13 for the ``superseded_by`` of an archived record (checked over the whole graph by ``check_graph``)."""
    block = rec.get("archived")
    if rec.get("status") != "archived" or not isinstance(block, dict):
        return []
    out: List[Problem] = []
    for ref in block.get("superseded_by") or []:
        if ref == rid:
            out.append(Problem("P13", file, line, "%s: archived.superseded_by names the record itself" % rid))
        elif onto.record(ref) is None:
            out.append(Problem("P13", file, line, "%s: archived.superseded_by %s does not exist" % (rid, ref)))
        elif not _chain_ends(onto, str(ref), {rid}):
            out.append(Problem("P13", file, line, "%s: archived.superseded_by %s is archived, and its chain ends "
                                                  "nowhere; name an active record" % (rid, ref)))
    return out


def _prov_problems(onto: Any, rid: str, rec: Dict[str, Any], file: str, line: int, impsrcs: Set[str]) -> List[Problem]:
    out: List[Problem] = []
    prov = rec.get("prov") if isinstance(rec.get("prov"), list) else []  # another type is P02
    archived = rec.get("status") == "archived"
    if rec.get("status") == "confirmed" and not prov:
        out.append(Problem("P11", file, line, "%s: confirmed without provenance" % rid))
    for p in prov:
        if not isinstance(p, dict):
            continue
        src = str(p.get("src") or "")
        if src.startswith("imp:"):
            # an archived bridge is history: it may cite an import that has since been removed (no op can re-cite
            # an archived record, and import remove must not stay blocked by it)
            if not archived and src not in impsrcs and not _imported_impsrc(onto, src):
                out.append(Problem("P11", file, line, "%s: provenance %s is not a pinned import" % (rid, src)))
            continue
        if src not in onto.sources:
            out.append(Problem("P11", file, line, "%s: provenance %s is not in the sources index" % (rid, src)))
            continue
        where = sources.loc_problem(src, p.get("loc"), onto.sources.get(src))
        if where:
            out.append(Problem("P11", file, line, "%s: provenance %s: %s" % (rid, src, where)))
    return out


def _text_problems(rid: str, rec: Dict[str, Any], shape: str, file: str, line: int) -> List[Problem]:
    return [Problem("P02", file, line, "%s: %s" % (rid, m)) for m in records.control_problems(rec, shape)]


def _endpoint_problem(onto: Any, eid: str, end: str, file: str, line: int) -> Optional[Problem]:
    if end in onto.nodes:
        return None
    ns = onto.ns_of(end)
    if ids.record_prefix(end) == "src":
        return Problem("P10", file, line, "%s: endpoint %s is not in the sources index" % (eid, end))
    if ns == "self":
        return Problem("P10", file, line, "%s: endpoint %s does not exist" % (eid, end))
    if not any(e.get("ns") == ns for e in onto.imports):
        return Problem("P10", file, line, "%s: endpoint %s names %s, which is not imported" % (eid, end, ns))
    return Problem("P10", file, line, "%s: endpoint %s is absent at the pinned import (%s)"
                   % (eid, end, onto.import_label(ns)))


def check_graph(onto: Any, touched: Optional[Iterable[str]] = None) -> List[Problem]:
    """P02 and P06 to P13 over the local records of ``onto`` (a ``graph.Ontology``). ``touched`` limits the
    per-record checks (schema, ids, kinds, relations, provenance, archive blocks) to those ids."""
    only = set(touched) if touched is not None else None
    registry = onto.registry
    impsrcs = _impsrcs(onto)
    problems: List[Problem] = [p for p in onto.problems if p.code in ("P02", "P06")]
    # aliases (P06): against active ids and other active aliases
    seen_alias: Dict[str, str] = {}
    for nid in onto.local_ids:
        node = onto.nodes[nid]
        if node.get("status") == "archived":
            continue
        file, line = _where(onto, nid)
        for alias in node.get("aliases") if isinstance(node.get("aliases"), list) else []:  # another type is P02
            if not isinstance(alias, str):
                continue
            key = alias.strip().lower()
            other = onto.nodes.get(key) or onto.nodes.get(alias)
            if other is not None and other.get("id") != nid and other.get("status") != "archived":
                problems.append(Problem("P06", file, line, "%s: alias %r is the id of %s" % (nid, alias, other["id"])))
            if key in seen_alias and seen_alias[key] != nid:
                problems.append(Problem("P06", file, line, "%s: alias %r is also an alias of %s"
                                        % (nid, alias, seen_alias[key])))
            seen_alias.setdefault(key, nid)
    for nid in onto.local_ids:
        node = onto.nodes[nid]
        file, line = _where(onto, nid)
        if only is None or nid in only:
            for message in records.check(node, "node"):
                problems.append(Problem("P02", file, line, "%s: %s" % (nid, message)))
            if ids.is_qualified(nid):
                problems.append(Problem("P16", file, line, "%s: a qualified id in graph/nodes.jsonl (imports are "
                                                           "read-only)" % nid))
            elif not ids.is_local(nid):
                problems.append(Problem("P07", file, line, "%s: not a valid node id" % nid))
            elif nid.split(":", 1)[0] != node.get("kind"):
                problems.append(Problem("P07", file, line, "%s: the id's kind differs from kind %r"
                                        % (nid, node.get("kind"))))
            kind = node.get("kind")
            decl = registry.kind(str(kind)) if isinstance(kind, str) else None
            if decl is None:
                problems.append(Problem("P08", file, line, "%s: unknown kind %r" % (nid, kind)))
            elif isinstance(node.get("attrs"), dict) and node.get("status") != "archived":
                # an archived node is history: archived is final, so no op could make its attrs fit a later
                # declaration of its kind (a fold into the core pack)
                for message in packs.field_errors(node["attrs"], decl.get("fields") or {}):
                    problems.append(Problem("P08", file, line, "%s: attrs %s" % (nid, message)))
            problems.extend(_prov_problems(onto, nid, node, file, line, impsrcs))
            problems.extend(_archive_problems(onto, nid, node, file, line))
            problems.extend(_text_problems(nid, node, "node", file, line))
        # between records: a supersession chain a later merge or archive broke is caught at that write
        problems.extend(_supersession_problems(onto, nid, node, file, line))
    for eid in onto.local_edge_ids:
        edge = onto.edges[eid]
        file, line = _where(onto, eid)
        problems.extend(_supersession_problems(onto, eid, edge, file, line))
        src, rel, dst = str(edge.get("src") or ""), str(edge.get("rel") or ""), str(edge.get("dst") or "")
        for end in (src, dst):
            if edge.get("status") == "archived" and onto.ns_of(end) != "self":
                continue  # an archived bridge whose far end left the import is history, not a problem
            found = _endpoint_problem(onto, eid, end, file, line)
            if found is not None:
                problems.append(found)
        if edge.get("status") != "archived" and not edge.get("background"):
            for end in (src, dst):
                if onto.ns_of(end) != "self":
                    continue  # an import archived upstream is W04, not a problem here
                other = onto.node(end)
                if other is not None and other.get("status") == "archived":
                    problems.append(Problem("P13", file, line, "%s: an active edge touches the archived node %s"
                                            % (eid, end)))
        if only is not None and eid not in only:
            continue
        for message in records.check(edge, "edge"):
            problems.append(Problem("P02", file, line, "%s: %s" % (eid, message)))
        full = util.sha256_text("\x1f".join((src, rel, dst, str(edge.get("key") or ""))))
        if not ids.EDGE_RE.match(eid) or eid[2:] != full[: len(eid) - 2]:
            problems.append(Problem("P07", file, line, "%s: the edge id does not match its src, rel, dst and key "
                                                       "(expected %s)" % (eid, "e:" + full[:12])))
        rel_ns = onto.rel_ns(eid)  # both ends in one import: that import's declaration of the name governs
        decl = registry.relation(rel, rel_ns)
        if decl is None:
            problems.append(Problem("P09", file, line, "%s: unknown relation %r" % (eid, rel)))
        else:
            if decl.get("symmetric") and src > dst:
                problems.append(Problem("P09", file, line, "%s: symmetric relation %s must store its endpoints "
                                                           "sorted" % (eid, rel)))
            # an archived edge is history: it fit the declaration it was made under, and a later declaration (a
            # local name a newer kit folds into the core one) must not turn it into a problem no op can fix
            if (src in onto.nodes and dst in onto.nodes and edge.get("status") != "archived"
                    and not registry.allowed(rel, onto.kind_of(src), onto.kind_of(dst), rel_ns)):
                problems.append(Problem("P09", file, line, "%s: %s does not link a %s to a %s"
                                        % (eid, rel, onto.kind_of(src), onto.kind_of(dst))))
        problems.extend(_prov_problems(onto, eid, edge, file, line, impsrcs))
        problems.extend(_archive_problems(onto, eid, edge, file, line))
        problems.extend(_text_problems(eid, edge, "edge", file, line))
    return _sort(problems)


# files ---------------------------------------------------------------------------------------------------------
def _read(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def _jsonl_order_key(rel: str):
    """Sorted files order by id. Append-only files need only a non-decreasing ``at``: lines with the same ``at``
    keep their append order (a fixed clock gives every line the same time), and the fix sorts stably."""
    if rel in (r for r, _d in APPEND_ONLY):
        return lambda r: str(r.get("at") or "")
    return lambda r: str(r.get("id") or "")


def _jsonl_file(repo: store.Repo, rel: str, fix: bool) -> Tuple[List[Problem], bool]:
    """P04 and P05 for one JSONL file; with ``fix`` rewrites it sorted, canonical and without duplicate lines.
    Returns (problems left, fixed)."""
    data = _read(repo.path(rel))
    if data is None or not data:
        return [], False
    numbered, bad = store.read_jsonl_lines(repo.path(rel))
    if bad:
        return [], False  # P01 is reported by the loaders; a file with bad lines is not rewritten
    collapsed = False
    if fix and rel == SOURCES:  # the same source ingested on two branches: one line (the graph reports it as P06)
        numbered, collapsed = _collapse_sources(numbered)
    problems: List[Problem] = []
    seen: Dict[bytes, int] = {}
    repeatable = rel in REPEATABLE_LINES
    raw_lines = [ln for ln in data.split(b"\n")]
    for number, raw in enumerate(raw_lines, start=1):
        if not raw.strip() or repeatable:
            continue
        if raw in seen:
            problems.append(Problem("P05", rel, number, "the same line as line %d (fix: drop duplicates)" % seen[raw]))
        else:
            seen[raw] = number
    rows = []
    kept: Set[str] = set()
    for _n, row in numbered:
        line = util.canonical_line(row)
        if line in kept and not repeatable:
            continue
        kept.add(line)
        rows.append(row)
    key = _jsonl_order_key(rel)
    want = "".join(util.canonical_line(r) + "\n" for r in sorted(rows, key=key)).encode("utf-8")
    present = [x for x in raw_lines if x.strip()]
    unique_raw = b"".join(r + b"\n" for r in (present if repeatable else dict.fromkeys(present)))
    if unique_raw != want:
        problems.append(Problem("P04", rel, 0, "not sorted or not canonical (fix: rewrite it)"))
    if fix and (problems or collapsed):
        store.write_bytes(repo.path(rel), want)
        return [], True
    return problems, False


def damaged_edges(onto: Any) -> bool:
    """True when the edges file has lines a rewrite would drop (P01, or one id on two lines)."""
    return any(p.file == EDGES and (p.code == "P01" or (p.code == "P06" and "already used" in p.message))
               for p in onto.problems)


def same_text_copies(rows: List[Dict[str, Any]]) -> bool:
    """True when index lines of one source id all hold the same text (one ``sha256``) but differ otherwise."""
    shas = {r.get("sha256") for r in rows}
    return len(rows) > 1 and len(shas) == 1 and isinstance(next(iter(shas)), str)


def _collapse_sources(numbered: List[Tuple[int, Dict[str, Any]]]) -> Tuple[List[Tuple[int, Dict[str, Any]]], bool]:
    """Index lines that share an id and a ``sha256`` as one line: an erased copy if there is one (an erasure is
    never undone), else the earliest ``captured_at`` (ties by the canonical line); ``erased`` is true when any copy
    is. Lines of one id with different texts are left for a person (P06)."""
    groups: Dict[str, List[Tuple[int, Dict[str, Any]]]] = {}
    for n, row in numbered:
        groups.setdefault(str(row.get("id")), []).append((n, row))
    out: List[Tuple[int, Dict[str, Any]]] = []
    changed = False
    for n, row in numbered:
        group = groups[str(row.get("id"))]
        distinct = {util.canonical_line(r): r for _n, r in group}
        if len(distinct) < 2 or not same_text_copies(list(distinct.values())):
            out.append((n, row))
            continue
        if n != group[0][0]:
            continue  # folded into the first line of the group
        copies = sorted(distinct.values(), key=lambda r: (not r.get("erased"), str(r.get("captured_at") or ""),
                                                          util.canonical_line(r)))
        keep = dict(copies[0])
        if any(r.get("erased") for r in copies):
            keep["erased"] = True
        out.append((n, keep))
        changed = True
    return out, changed


def _json_file(repo: store.Repo, rel: str, fix: bool) -> Tuple[List[Problem], bool]:
    data = _read(repo.path(rel))
    if data is None:
        return [], False
    try:
        value = store.read_json(repo.path(rel))
    except DataError:
        return [], False
    if data == util.canonical_bytes(value):
        return [], False
    if fix:
        store.write_json(repo.path(rel), value)
        return [], True
    return [Problem("P04", rel, 0, "not canonical JSON (fix: rewrite it)")], False


def _json_files(repo: store.Repo) -> List[str]:
    rels = ["ontology.json", packs.LOCAL_PACK, lockfile.LOCK]
    for pattern in ("ledger/decisions/*.json", "proposals/pending/*.json", "proposals/done/*.json"):
        rels += sorted(os.path.relpath(p, repo.root).replace(os.sep, "/")
                       for p in glob.glob(os.path.join(repo.root, *pattern.split("/"))))
    return rels


def _format_problems(repo: store.Repo) -> List[Problem]:
    out: List[Problem] = []
    fmt = repo.manifest.get("format")
    if not isinstance(fmt, int) or isinstance(fmt, bool):
        return [Problem("P03", "ontology.json", 0, "format must be a whole number")]
    if fmt > FORMAT:
        out.append(Problem("P03", "ontology.json", 0, "format %d: kit too old: upgrade the kit" % fmt))
    elif fmt < FORMAT:
        out.append(Problem("P03", "ontology.json", 0, "format %d: run onto migrate" % fmt))
    return out


def _line_records(repo: store.Repo, rel: str, def_name: str, code: str) -> Tuple[List[Problem], List[Dict[str, Any]]]:
    numbered, bad = store.read_jsonl_lines(repo.path(rel))
    problems = [Problem("P01", rel, n, m) for n, m in bad]
    for n, row in numbered:
        for message in records.check(row, def_name):
            problems.append(Problem(code, rel, n, message))
    return problems, [r for _n, r in numbered]


def _source_problems(repo: store.Repo, onto: Any) -> List[Problem]:
    out: List[Problem] = []
    for sid, entry in sorted(onto.sources.items()):
        file, line = onto.lines.get(sid, (SOURCES, 0))
        for message in records.check(entry, "source"):
            out.append(Problem("P02", file, line, "%s: %s" % (sid, message)))
        if not ids.SOURCE_RE.match(sid):
            out.append(Problem("P07", file, line, "%s: not a valid source id" % sid))
        data = _read(sources.text_path(repo, sid))
        if data is None:
            out.append(Problem("P14", file, line, "%s: the text file sources/%s.txt is missing" % (sid, sid)))
            continue
        if entry.get("erased"):
            continue
        digest = util.sha256_hex(data)
        if digest != entry.get("sha256"):
            out.append(Problem("P14", file, line, "%s: the stored text's sha256 differs from the index" % sid))
        elif sid != "src-" + digest[:12]:
            out.append(Problem("P14", file, line, "%s: the id does not match the text's sha256" % sid))
        elif entry.get("bytes") != len(data):
            out.append(Problem("P14", file, line, "%s: bytes differ from the index" % sid))
    folder = repo.path("sources")
    if os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            rel = "sources/" + name
            if rel == SOURCES:
                continue
            m = _TEXT_RE.match(rel) or _ORIG_RE.match(rel)
            if m and m.group(1) in onto.sources:
                continue
            if m or not name.startswith("."):
                out.append(Problem("P14", rel, 0, "a file under sources/ that the index does not list"))
    return out


def _secret_problems(repo: store.Repo) -> List[Problem]:
    out: List[Problem] = []
    files, _links = store._topic_files(repo.root)
    for rel in files:
        path = repo.path(rel)
        if not os.path.isfile(path) or os.path.islink(path):
            continue
        data = _read(path) or b""
        for kind, match in secrets.scan_bytes(data):
            out.append(Problem("P17", rel, 0, "%s secret pattern (starts %r)" % (kind, match[:6].decode("ascii",
                                                                                                        "replace"))))
    return out


def _personal_problems(repo: store.Repo, onto: Any) -> List[Problem]:
    module = _optional("sanitize")
    if module is None or not hasattr(module, "check_text"):
        return []
    out: List[Problem] = []
    policy = repo.policy
    title = repo.manifest.get("title") if isinstance(repo.manifest, dict) else None
    title_kinds = sorted(set(module.check_text(title, policy))) if isinstance(title, str) else []
    if title_kinds:  # the title reaches the export's meta.title, the MCP instructions and every question
        out.append(Problem("P18", "ontology.json", 0, "the title holds personal data the policy redacts: %s (edit "
                                                      "the title in ontology.json)" % ", ".join(title_kinds)))
    for sid, entry in sorted(onto.sources.items()):
        if entry.get("erased"):
            continue
        data = _read(sources.text_path(repo, sid))
        if data is None:
            continue
        kinds = module.check_text(data.decode("utf-8", "replace"), policy)
        if kinds:
            out.append(Problem("P18", "sources/%s.txt" % sid, 0, "holds personal data the policy redacts: %s"
                               % ", ".join(sorted(set(kinds)))))
    # the free text of records and proposals, in the same mode: what a proposal copies out of a redacted source
    # (or types in) would otherwise reach the build and the release. One pass over all of it first; the records are
    # checked one by one only when that pass finds something.
    rids = list(onto.local_ids) + list(onto.local_edge_ids)
    shapes = {rid: "node" if rid in onto.nodes else "edge" for rid in rids}
    everything = "\n\n".join(container[key] for rid in rids
                             for container, key, role, _p in records.text_slots(onto.record(rid) or {}, shapes[rid])
                             if role in (records.LINE, records.BLOCK))
    for rid in rids if everything and module.check_text(everything, policy) else []:
        kinds = _slot_kinds(module, onto.record(rid) or {}, shapes[rid], policy)
        if kinds:
            file, line = _where(onto, rid)
            out.append(Problem("P18", file, line, "%s: holds personal data the policy redacts: %s (edit it with an "
                                                  "update op, or erase it)" % (rid, ", ".join(kinds))))
    for path in sorted(glob.glob(os.path.join(repo.path("proposals"), "*", "*.json"))):
        rel = os.path.relpath(path, repo.root).replace(os.sep, "/")
        try:
            prop = store.read_json(path)
        except (DataError, OSError):
            continue  # P01 and P21 report it
        kinds = _slot_kinds(module, prop, "proposal", policy) if isinstance(prop, dict) else []
        if kinds:
            out.append(Problem("P18", rel, 0, "holds personal data the policy redacts: %s" % ", ".join(kinds)))
    out.extend(_ledger_personal(repo, module, policy))
    return out


VERSIONS_ROW_RE = re.compile(r"^\| (v[0-9]+) \|")  # release.ROW_RE: a row of VERSIONS.md
CHANGE_TEXT_LISTS = ("done", "next", "open_questions")


def _ledger_personal(repo: store.Repo, module: Any, policy: Dict[str, Any]) -> List[Problem]:
    """P18 in the release notes (the Notes cell of each ``VERSIONS.md`` row: the date and the data hash are left
    out) and in the free text of ``ledger/changes.jsonl`` (summary, done, next, open_questions)."""
    out: List[Problem] = []
    data = _read(repo.path("VERSIONS.md"))
    for n, line in enumerate((data or b"").decode("utf-8", "replace").splitlines(), 1):
        if not VERSIONS_ROW_RE.match(line):
            continue
        notes = " | ".join(line.strip().strip("|").split(" | ")[6:]).strip()
        kinds = sorted(set(module.check_text(notes, policy))) if notes else []
        if kinds:
            out.append(Problem("P18", "VERSIONS.md", n, "holds personal data the policy redacts: %s (edit the Notes "
                                                        "cell)" % ", ".join(kinds)))
    numbered, _bad = store.read_jsonl_lines(repo.path("ledger/changes.jsonl"))
    rows = [(n, row, _change_texts(row)) for n, row in numbered if isinstance(row, dict)]
    everything = "\n\n".join(t for _n, _row, texts in rows for t in texts)
    for n, row, texts in rows if everything and module.check_text(everything, policy) else []:
        kinds = sorted({k for t in texts for k in module.check_text(t, policy)})
        if kinds:
            out.append(Problem("P18", "ledger/changes.jsonl", n, "%s: holds personal data the policy redacts: %s "
                                                                 "(rewrite that text in the line)"
                               % (row.get("id"), ", ".join(kinds))))
    return out


def _change_texts(row: Dict[str, Any]) -> List[str]:
    texts = [row["summary"]] if isinstance(row.get("summary"), str) else []
    for key in CHANGE_TEXT_LISTS:
        value = row.get(key)
        if isinstance(value, list):
            texts.extend(v for v in value if isinstance(v, str))
    return texts


def _slot_kinds(module: Any, obj: Dict[str, Any], shape: str, policy: Dict[str, Any]) -> List[str]:
    """The personal kinds ``check_text`` finds in the free text of ``obj`` (quotes left out: they match their
    source, which is checked on its own)."""
    found: Set[str] = set()
    for container, key, role, _path in records.text_slots(obj, shape):
        if role in (records.LINE, records.BLOCK):
            found |= set(module.check_text(container[key], policy))
    return sorted(found)


def _proposal_problems(repo: store.Repo) -> Tuple[List[Problem], Set[str], List[Dict[str, Any]]]:
    out: List[Problem] = []
    known: Set[str] = set()
    pending: List[Dict[str, Any]] = []
    for folder, statuses in sorted(PROPOSAL_FOLDERS.items()):
        for path in sorted(glob.glob(os.path.join(repo.path("proposals/" + folder), "*.json"))):
            rel = os.path.relpath(path, repo.root).replace(os.sep, "/")
            try:
                prop = store.read_json(path)
            except DataError as exc:
                out.append(Problem("P01", rel, 0, str(exc)))
                continue
            if not isinstance(prop, dict):
                out.append(Problem("P21", rel, 0, "not a JSON object"))
                continue
            for message in records.check(prop, "proposal"):
                out.append(Problem("P21", rel, 0, message))
            pid = prop.get("id")
            if isinstance(pid, str):
                known.add(pid)
                if os.path.basename(path) != pid + ".json":
                    out.append(Problem("P21", rel, 0, "the file name differs from the id %s" % pid))
            if prop.get("status") not in statuses:
                out.append(Problem("P21", rel, 0, "status %r belongs in proposals/%s/"
                                   % (prop.get("status"), "done" if folder == "pending" else "pending")))
            if prop.get("status") == "pending" and folder == "pending":
                pending.append(prop)
    return out, known, pending


def _decision_problems(repo: store.Repo) -> List[Problem]:
    out: List[Problem] = []
    found: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    for path in sorted(glob.glob(os.path.join(repo.path("ledger/decisions"), "*.json"))):
        rel = os.path.relpath(path, repo.root).replace(os.sep, "/")
        try:
            dec = store.read_json(path)
        except DataError as exc:
            out.append(Problem("P01", rel, 0, str(exc)))
            continue
        if not isinstance(dec, dict):
            out.append(Problem("P21", rel, 0, "not a JSON object"))
            continue
        for message in records.check(dec, "decision"):
            out.append(Problem("P21", rel, 0, message))
        did = dec.get("id")
        if isinstance(did, str):
            found[did] = (rel, dec)
            if os.path.basename(path) != did + ".json":
                out.append(Problem("P21", rel, 0, "the file name differs from the id %s" % did))
    for did, (rel, dec) in sorted(found.items()):
        old = dec.get("supersedes")
        if old:
            if old not in found:
                out.append(Problem("P21", rel, 0, "supersedes %s, which is not in the ledger" % old))
            elif found[old][1].get("superseded_by") != did or found[old][1].get("status") != "superseded":
                out.append(Problem("P21", found[old][0], 0, "%s is superseded by %s but not marked so" % (old, did)))
        narrowed = dec.get("narrows")
        if narrowed is not None and not isinstance(narrowed, str):
            # records.check names the schema problem; the lookups below need one decision id
            out.append(Problem("P21", rel, 0, "narrows must be one decision id (a string), not %s"
                               % type(narrowed).__name__))
        elif narrowed:
            if narrowed == did:
                out.append(Problem("P21", rel, 0, "narrows itself"))
            elif narrowed not in found:
                out.append(Problem("P21", rel, 0, "narrows %s, which is not in the ledger" % narrowed))
        if dec.get("status") == "superseded":
            newer = dec.get("superseded_by")
            if not newer or newer not in found:
                out.append(Problem("P21", rel, 0, "superseded without a known superseded_by"))
        elif dec.get("superseded_by"):
            out.append(Problem("P21", rel, 0, "superseded_by is set on an active decision"))
    return out


def _change_problems(onto: Any, change_rows: List[Dict[str, Any]], proposals_known: Set[str],
                     numbered: List[Tuple[int, Dict[str, Any]]]) -> List[Problem]:
    out: List[Problem] = []
    change_ids = {r.get("id") for r in change_rows}
    for rid in onto.local_ids + onto.local_edge_ids:
        rec = onto.record(rid) or {}
        cid = rec.get("change")
        if isinstance(cid, str) and cid not in change_ids:
            file, line = _where(onto, rid)
            out.append(Problem("P22", file, line, "%s: change %s is not in ledger/changes.jsonl" % (rid, cid)))
    for n, row in numbered:
        prop = row.get("proposal")
        if prop and prop not in proposals_known:
            out.append(Problem("P22", "ledger/changes.jsonl", n, "change %s names the unknown proposal %s"
                               % (row.get("id"), prop)))
    return out


def _proposal_cites(repo: store.Repo, prop_id: Any, wanted: Set[str]) -> bool:
    """True when the proposal ``prop_id`` (pending or done) is drafted from one of ``wanted``: its ``source``, or the
    provenance of an op or of a review edit."""
    if not isinstance(prop_id, str):
        return False
    for folder in ("done", "pending"):
        try:
            doc = store.read_json(repo.path("proposals/%s/%s.json" % (folder, prop_id)), None)
        except DataError:
            return False
        if not isinstance(doc, dict):
            continue
        if doc.get("source") in wanted:
            return True
        review = doc.get("review") if isinstance(doc.get("review"), dict) else {}
        ops = list(doc.get("ops") or []) + list((review.get("edits") or {}).values()
                                                 if isinstance(review.get("edits"), dict) else [])
        return any(isinstance(p, dict) and p.get("src") in wanted
                   for op in ops if isinstance(op, dict) for p in op.get("prov") or [])
    return False


def _erased_citation_problems(repo: store.Repo, onto: Any, numbered: List[Tuple[int, Dict[str, Any]]]) -> List[Problem]:
    """P11 for what an erase forbids that reached the graph anyway (a merge of a branch that still had it, or a kit
    that let it through): a provenance quote of an erased source (the erase drops every one), and a record whose
    last change came after the source's erase from a proposal drafted from that source."""
    erased = {sid for sid, entry in onto.sources.items() if entry.get("erased")}
    if not erased:
        return []
    position: Dict[str, int] = {}
    erased_at: Dict[str, int] = {}
    for i, (_n, row) in enumerate(numbered):
        if isinstance(row.get("id"), str):
            position[row["id"]] = i
        if row.get("type") == "erase":
            for sid in row.get("ids") or []:
                if sid in erased:
                    erased_at.setdefault(sid, i)
    out: List[Problem] = []
    for rid in onto.local_ids + onto.local_edge_ids:
        rec = onto.record(rid) or {}
        prov = rec.get("prov") if isinstance(rec.get("prov"), list) else []  # another type is P02
        cited = [p for p in prov if isinstance(p, dict) and p.get("src") in erased]
        if not cited:
            continue
        file, line = _where(onto, rid)
        for p in cited:
            if "quote" in p:
                out.append(Problem("P11", file, line, "%s: provenance %s keeps a quote of an erased source (erase the "
                                                      "source again, or archive the record)" % (rid, p.get("src"))))
        at = position.get(str(rec.get("change")))
        late = sorted({str(p.get("src")) for p in cited if at is not None and at > erased_at.get(str(p.get("src")),
                                                                                                    len(numbered))})
        if late and _proposal_cites(repo, numbered[at][1].get("proposal"), set(late)):  # type: ignore[index]
            out.append(Problem("P11", file, line, "%s: change %s applied text drafted from %s after it was erased "
                                                  "(archive the record, or erase it)"
                               % (rid, rec.get("change"), ", ".join(late))))
    return out


# warnings ------------------------------------------------------------------------------------------------------
def _warnings(repo: store.Repo, onto: Any, pending: List[Dict[str, Any]]) -> List[Problem]:
    out: List[Problem] = []
    table = needs.all_needs(onto)
    for nid in onto.local_nodes(active_only=True):
        file, line = _where(onto, nid)
        info = table.get(nid) or {"gaps": []}
        for gap in info["gaps"]:
            if gap["type"] == "missing_field":
                out.append(Problem("W01", file, line, "%s: expected field %s is missing" % (nid, gap.get("field"))))
            elif gap["type"] == "orphan":
                out.append(Problem("W02", file, line, "%s: orphan node (no active links)" % nid))
        touching = onto.edges_of(nid, include_archived=True)
        if touching and all(e["edge"].get("status") == "archived" or not onto.active(e["other"])
                            for e in touching):
            out.append(Problem("W03", file, line, "%s: referenced only by archived records" % nid))
    for eid in onto.local_edge_ids:
        edge = onto.edges[eid]
        if edge.get("status") == "archived" or not onto.is_bridge(edge):
            continue
        for end in (edge.get("src"), edge.get("dst")):
            if onto.ns_of(str(end)) == "self":
                continue
            other = onto.node(str(end))
            if other is not None and other.get("status") == "archived":
                file, line = _where(onto, eid)
                out.append(Problem("W04", file, line, "%s: bridge target %s is archived upstream" % (eid, end)))
    for end, need in needs.inherited_bridge_gaps(onto):  # bridges an import brings: read-only here, so a warning
        for gap in need["gaps"]:
            owner = str(gap.get("owner"))
            other, _sep, why = str(gap.get("note") or "").partition(" ")
            state = "absent" if why.startswith("absent") else "archived"
            out.append(Problem("W04", lockfile.EXPORT % owner, 0,
                               "%s: bridge of %s points at %s, which the pins leave %s; keep the pin %s was released "
                               "with, or have %s re-point it and release again"
                               % (gap.get("edge"), owner, other, state, owner, owner)))
    out += list(getattr(onto, "warnings", []) or [])  # load warnings: imports that disagree on one edge (W04)
    now = util.now()
    replaced = sources.superseded(onto.sources)
    for sid, entry in sorted(onto.sources.items()):
        if sid not in replaced and sources.stale(entry, now):
            file, line = onto.lines.get(sid, (SOURCES, 0))
            out.append(Problem("W05", file, line, "%s: stale (captured %s, refresh after %s days)"
                               % (sid, entry.get("captured_at"), entry.get("stale_after_days"))))
    policy = repo.policy
    max_pending = int(policy.get("max_pending") or 0)
    if len(pending) > max_pending:
        out.append(Problem("W06", "proposals/pending", 0, "%d pending proposals (more than max_pending %d)"
                           % (len(pending), max_pending)))
    stale_days = int(policy.get("pending_stale_days") or 0)
    for prop in pending:
        try:
            age = (now - util.parse_ts(str(prop.get("created")))).days
        except ValueError:
            continue
        if age > stale_days:
            out.append(Problem("W06", "proposals/pending/%s.json" % prop.get("id"), 0,
                               "pending for %d days (more than %d)" % (age, stale_days)))
    decisions = ledger.all_decisions(repo)
    for target, narrowers in sorted(ledger.narrowed_by(decisions).items()):
        old = decisions.get(target)
        if old is None or old.get("status") == "active":
            continue  # a missing target is P21
        end = ledger.active_end(decisions, target)  # superseded twice or more: the active end of the chain
        for did in narrowers:
            advice = ("record a decision that supersedes %s and narrows %s" % (did, end) if end else
                      "no active decision replaces it; record a decision that supersedes %s without narrowing" % did)
            out.append(Problem("W10", "%s/%s.json" % (ledger.DECISIONS_DIR, did), 0,
                               "narrows %s, which is superseded by %s; %s" % (target, old.get("superseded_by"),
                                                                             advice)))
    repo_kit = repo.manifest.get("kit")
    if repo_kit and repo_kit != __version__:
        older, newer = _kit_order(str(repo_kit), __version__)
        what = ("; run onto migrate to bring the topic up to this kit" if older else
                "; a newer kit wrote it: upgrade this kit (README, \"Upgrading the kit\") and restart the MCP server"
                if newer else "")
        out.append(Problem("W07", "ontology.json", 0, "the running kit is %s; this topic was written by %s%s"
                           % (__version__, repo_kit, what)))
    return out


def _kit_order(topic: str, running: str) -> Tuple[bool, bool]:
    """``(topic older than running, topic newer than running)`` for ``X.Y.Z`` versions; both False when either
    does not read as one."""
    def key(v: str) -> Optional[Tuple[int, ...]]:
        parts = v.split(".")
        return tuple(int(p) for p in parts) if len(parts) == 3 and all(p.isdigit() for p in parts) else None

    a, b = key(topic), key(running)
    if a is None or b is None:
        return False, False
    return a < b, a > b


# the whole repo ------------------------------------------------------------------------------------------------
def _collect(repo: store.Repo, fix: bool) -> Tuple[List[Problem], List[Problem], Dict[str, Any], bool]:
    problems: List[Problem] = []
    fixed = False
    try:
        repo.reload()
    except DataError as exc:
        return [Problem("P01", "ontology.json", 0, str(exc))], [], {}, False
    except FileNotFoundError:
        return [Problem("P01", "ontology.json", 0, "missing")], [], {}, False
    if not isinstance(repo.manifest, dict):
        return [Problem("P01", "ontology.json", 0, "not a JSON object")], [], {}, False
    intent = store.pending_intent(repo)  # with fix, the write lock has rolled it back already
    if intent is not None:
        problems.append(Problem("P22", store.INTENT_REL, 0, "a write stopped half way%s; run onto validate --fix "
                                                            "(or any write command) to roll it back first"
                                % (" (started %s)" % intent["at"] if intent.get("at") else "")))
    note = store.recovery_note(repo)
    recovered = [Problem("W08", str(item.get("path") or store.RECOVERY_REL), 0,
                         "an interrupted write%s was rolled back, but this file was left as it is: %s; check it "
                         "(git diff)%s" % (" (started %s)" % note["intent_at"] if note.get("intent_at") else "",
                                           item.get("why"), "" if fix else ", then run onto validate --fix to clear "
                                                                           "this note"))
                 for item in (note or {}).get("left") or [] if isinstance(item, dict)]
    if note is not None and fix:
        store.clear_recovery_note(repo)  # reported once more in this run, then cleared
    problems += _format_problems(repo)
    for message in records.check(repo.manifest, "ontology_manifest"):
        problems.append(Problem("P02", "ontology.json", 0, message))
    for rel in SORTED_JSONL + tuple(r for r, _d in APPEND_ONLY) + (packs.LOCAL_QUESTIONS,):
        found, did = _jsonl_file(repo, rel, fix)
        problems += found
        fixed = fixed or did
    for rel in _json_files(repo):
        found, did = _json_file(repo, rel, fix)
        problems += found
        fixed = fixed or did
    if fixed:
        clear_cache()
        store.clear_cache()
        repo.reload()
    onto = Ontology.load(repo)
    if fix and onto.shadowed and not damaged_edges(onto):
        # a local edge an import also holds (P06): imports are read-only, so the local copy goes
        numbered, _bad = store.read_jsonl_lines(repo.path(EDGES))
        store.write_bytes(repo.path(EDGES), store.jsonl_bytes([r for _n, r in numbered
                                                               if r.get("id") not in onto.shadowed], "id"))
        fixed = True
        clear_cache()
        store.clear_cache()
        onto = Ontology.load(repo)
    # P05 comes from the file checks and P15 from lockfile.verify; the graph's copies would repeat them
    problems += [p for p in onto.problems if p.code not in ("P02", "P05", "P06", "P15")]
    problems += onto.registry.problems()
    problems += check_graph(onto)
    problems += _source_problems(repo, onto)
    problems += lockfile.verify(repo)
    problems += _secret_problems(repo)
    problems += _personal_problems(repo, onto)
    problems += store.path_problems(repo)
    prop_problems, known, pending = _proposal_problems(repo)
    problems += prop_problems
    problems += _decision_problems(repo)
    change_problems, change_rows = _line_records(repo, "ledger/changes.jsonl", "change", "P21")
    problems += change_problems
    numbered, _bad = store.read_jsonl_lines(repo.path("ledger/changes.jsonl"))
    problems += _change_problems(onto, change_rows, known, numbered)
    problems += _erased_citation_problems(repo, onto, numbered)
    for rel, def_name in (("interview/log.jsonl", "answer"), ("metrics/history.jsonl", "point")):
        found, _rows = _line_records(repo, rel, def_name, "P02")
        problems += found
    q_numbered, _qbad = store.read_jsonl_lines(repo.path(packs.LOCAL_QUESTIONS))
    for n, row in q_numbered:
        for message in packs.check_question(row):
            problems.append(Problem("P20", packs.LOCAL_QUESTIONS, n, message))
    calibrated, uncalibrated = assessment.calibration(onto, lambda rid: _where(onto, rid))
    problems += calibrated
    warnings = _warnings(repo, onto, pending) + recovered + uncalibrated
    richness = None
    try:
        module = _optional("richness")
        if module is not None and hasattr(module, "summary"):
            richness = (module.summary(onto) or {}).get("score")
    except Exception:  # a package module must never break validation
        richness = None
    summary = {
        "nodes": len(onto.local_ids),
        "edges": len(onto.local_edge_ids),
        "sources": len(onto.sources),
        "imports": len(onto.imports),
        "richness": richness,
        "fixed": fixed,
        "kit": __version__,
    }
    return problems, warnings, summary, fixed


def validate(repo: store.Repo, fix: bool = False) -> Report:
    """Every problem and warning of ``repo``. With ``fix``, P04 and P05 are repaired first (under the write lock)
    and the rest is reported as found after the repair."""
    if fix:
        with store.write_lock(repo):
            problems, warnings, summary, _fixed = _collect(repo, True)
    else:
        problems, warnings, summary, _fixed = _collect(repo, False)
    return Report(_sort(problems), _sort(warnings), summary)
