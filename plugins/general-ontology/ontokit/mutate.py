"""The only writer of ``graph/*.jsonl`` and ``packs/local.*``.

``apply_ops(repo, resolved_ops, by=..., change_type=..., summary=...)`` applies ops whose ids, statuses and trusts
``pipeline`` has already decided. Under the write lock it reloads the graph, re-checks every ``annot.expect`` (a
mismatch raises ``Conflict`` and nothing is written), builds the new graph in memory and checks it with
``validate.check_graph`` (a problem the old graph did not have raises ``Refused``). Only then does it write the local
pack, the local questions, the nodes and the edges, append the change line and a richness point. If any write fails,
every file it touched is put back as it was; a process killed half way leaves an intent that the next writer rolls
back (``store.begin_write``). A file it rewrites must have parsed whole: unreadable lines, two lines with one id (a
git merge conflict) or a field of the wrong type refuse the write (``refuse_damaged``), so nothing is ever dropped
silently. A change to the local pack checks every local edge again (it may change how an import's kinds read).

Resolved op shapes (``n`` is the proposal's op number):

- ``add_node``: ``{node: {id, kind, name, summary?, attrs?, aliases?, gaps?, visibility?}, status, trust, conf, prov}``
- ``add_edge``: ``{edge: {src, rel, dst, key?, note?, background?}, status, trust, conf, prov}``. An edge that exists
  already gains the provenance, never the op's note (``update_edge`` changes a note); only an accept (status
  confirmed) also confirms it and may raise its trust, since a draft reviewed none of its text. An archived one is
  refused, and so is an imported one (imports are read-only).
- ``update_node`` / ``update_edge``: ``{id, set, unset, prov, status?, trust?, draft_trust?, annot: {expect}}``. A
  node's ``unset`` may also name ``gaps.<field>`` (or ``gaps``) to close the gaps recorded with ``add_gap``.
  ``status`` may only raise ``proposed`` to ``confirmed``; ``trust`` only rises, with one exception: a draft
  (``draft_trust`` set) that changes a name, summary, aliases, attrs or note lowers the record's trust to
  ``draft_trust`` when that is lower, so text from an untrusted source keeps its ``[untrusted]`` marker. A rename
  keeps the old name as an alias.
- ``merge``: ``{keep, drop, reason?, annot}``. Moves the edges and provenance of ``drop`` to ``keep`` (moved edges
  are new records; the old ones are archived and superseded by them), adds ``drop``'s id, name and aliases to
  ``keep``'s aliases, and archives ``drop`` with ``superseded_by=[keep]``. An edge whose place on ``keep`` an
  archived edge holds cannot move (archived is final), so the merge is refused (``merge_blocked``) rather than
  retire that fact.
- ``archive``: ``{id, archived: {reason, decision, superseded_by}, annot}``. ``on`` is filled here. Archiving a node
  archives its active edges with the same block.
- ``add_gap``: ``{id, gap: {field, note}}``.
- ``add_kind``, ``add_relation``, ``add_field``, ``map_kinds``: as authored (``packs.apply_op``, additive only).
- ``add_question``: ``{question}``, appended to ``packs/local.questions.jsonl`` (sorted by id).
- ``erase_node``: ``{id, decision}`` (G.6, ``onto erase``; never from a proposal). The name becomes ``[erased]``;
  the summary, attrs, aliases, gaps and every provenance quote are cleared; the node becomes ``local``, ``erased``
  and archived under the decision (an archived node keeps its block); its edges lose their quotes and notes and the
  active ones are archived with the same block. The proposal ops that created or changed it (found by the id their
  applied result records) lose the same text, and its names leave those proposals, the decisions (question, option
  labels, chosen text, rationale) and the change log (summaries and checkpoints), listed in ``names_scrubbed_in``.
  The result lists the files that still spell its id (``id_kept_in``): ids are permanent, and an id is a slug of the
  first name.
- ``erase_source``: ``{src, decision}`` (G.6). The stored text becomes ``[erased by <dec>]``, the index line keeps
  its ``sha256`` (and ``supersedes``) and gets ``erased: true``, its title becomes ``[erased]`` and its url null (a
  title or url often names a person), a stored original is deleted, and every quote citing the source is
  removed from the local nodes and edges. In the proposal files its quotes go too, and so does the text the ops
  citing it drafted wherever that text never reached the graph (open and rejected proposals, rejected ops); an open
  proposal citing it is closed as ``superseded``. The result lists the proposals changed (``names_scrubbed_in``) and
  closed (``proposals_closed``). These writes join the same all-or-nothing write as the graph files.
- ``scrub_text``: ``{text, decision, ids}`` (G.6, ``onto erase --scrub``; never from a proposal). The text becomes
  ``[erased]`` in the free text of the records ``ids`` names (summaries, attrs, gap notes, edge notes, archive
  reasons), the provenance quotes holding it are dropped, and it leaves the proposals, the decisions and the change
  log. Names, ids and source texts stay. ``erase_node`` refuses the topic's own node ``topic:<ns>``, and no erase
  takes the topic's own name out of the decisions or the log.

``init_topic`` creates a topic repo (B.2) and applies its root node ``topic:<ns>``.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import FORMAT, history, ids, ledger, lockfile, packs, records, secrets, sources, store, util, validate
from .errors import Conflict, DataError, Problem, Refused, UsageError
from .graph import EDGES, NODES, SHAPE_NOTE, Ontology
from .graph import clear_cache as clear_graph_cache

STATUS_RANK = {"proposed": 0, "confirmed": 1, "archived": 2}
TRUST_RANK = {"untrusted": 0, "agent": 1, "reviewed": 2, "user": 3}
NODE_PATHS = ("name", "summary", "aliases", "visibility", "conf")
EDGE_PATHS = ("note", "background", "conf")
TEXT_PATHS = ("name", "summary", "aliases", "note")  # with attrs.*: a draft that sets them passes on its trust
HISTORY_KINDS = ("init", "apply", "answer", "ingest", "import", "decide", "erase", "release", "migrate", "checkpoint",
                 "pack")
INIT_SOURCE_TITLE = "Topic title"
TOPIC_PREFIX = "Topic: "
EXTRA_DIRS = ("proposals/pending", "proposals/done", "ledger/decisions", "inbox")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")
PERSONAL_ACTIONS = ("keep", "redact", "refuse")


def _optional(name: str) -> Any:
    """A package module when it is built, else None (the lazy peer rule; ``commands.optional`` sits higher). A
    module that exists but fails to import raises."""
    return util.optional_module(name)


def get_path(rec: Dict[str, Any], path: str) -> Any:
    """The value at a dotted update path (``attrs.cadence``) or a top-level field, None when absent. ``gaps.<field>``
    is the list of the node's recorded gaps on that field (None when there is none)."""
    if path.startswith("attrs."):
        return (rec.get("attrs") or {}).get(path[6:])
    if path.startswith("keep."):
        return None
    if path.startswith("gaps."):
        found = [g for g in rec.get("gaps") or [] if isinstance(g, dict) and g.get("field") == path[5:]]
        return found or None
    return rec.get(path)


def _dedupe_prov(entries: Iterable[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for p in entries:
        if not isinstance(p, dict):
            continue
        key = util.canonical_line(p)
        if key not in seen:
            seen.add(key)
            out.append(dict(p))
    return out


def _dedupe(values: Iterable[Any]) -> List[Any]:
    out: List[Any] = []
    for v in values:
        if v not in out:
            out.append(v)
    return out


def _raise_trust(current: Any, new: Any) -> Any:
    if new is None:
        return current
    return new if TRUST_RANK.get(str(new), -1) > TRUST_RANK.get(str(current), -1) else current


def check_format(repo: store.Repo) -> None:
    """Refuse to write a topic whose data format differs from the kit's."""
    value = repo.manifest.get("format")
    if isinstance(value, int) and not isinstance(value, bool):
        if value > FORMAT:
            raise Refused("kit too old: upgrade the kit (the topic is format %d, this kit writes format %d)"
                          % (value, FORMAT))
        if value < FORMAT:
            raise Refused("the topic is format %d; run onto migrate first" % value)


# the working state ---------------------------------------------------------------------------------------------
class _State(object):
    """Copy-on-write view of the local records while ops are applied."""

    def __init__(self, repo: store.Repo, onto: Ontology) -> None:
        self.repo = repo
        self.onto = onto
        self.nodes: Dict[str, Dict[str, Any]] = {nid: onto.nodes[nid] for nid in onto.local_ids}
        self.edges: Dict[str, Dict[str, Any]] = {eid: onto.edges[eid] for eid in onto.local_edge_ids}
        self.copied: Set[str] = set()
        self.touched: List[str] = []
        self.created: Set[str] = set()
        self.pack_file = store.read_json(repo.path(packs.LOCAL_PACK), None)
        self.pack: Dict[str, Any] = copy.deepcopy(self.pack_file) if isinstance(self.pack_file, dict) \
            else packs.empty_local_pack()
        self.pack_changed = False
        rows, question_problems = store.read_jsonl(repo.path(packs.LOCAL_QUESTIONS))
        self.questions: List[Dict[str, Any]] = rows
        self.question_problems = question_problems  # unreadable lines: a rewrite would drop them
        self.questions_changed = False
        self.today = util.today()
        self.source_rows: Optional[List[Dict[str, Any]]] = None  # the index, once an op changes it
        self.files: Dict[str, Optional[bytes]] = {}  # other planned writes (None deletes the file)
        self.other_ids: List[str] = []  # ids a change names that are not nodes or edges (sources)

    def touch(self, rid: str) -> None:
        if rid not in self.touched:
            self.touched.append(rid)

    def node(self, nid: str) -> Dict[str, Any]:
        """A writable copy of a local node."""
        if nid not in self.nodes:
            raise Refused("%s is not a local node" % nid)
        if nid not in self.copied:
            self.nodes[nid] = copy.deepcopy(self.nodes[nid])
            self.copied.add(nid)
        self.touch(nid)
        return self.nodes[nid]

    def edge(self, eid: str) -> Dict[str, Any]:
        if eid not in self.edges:
            raise Refused("%s is not a local edge" % eid)
        if eid not in self.copied:
            self.edges[eid] = copy.deepcopy(self.edges[eid])
            self.copied.add(eid)
        self.touch(eid)
        return self.edges[eid]

    def exists(self, rid: str) -> bool:
        return rid in self.nodes or rid in self.edges or rid in self.onto.nodes

    def record(self, rid: str) -> Optional[Dict[str, Any]]:
        return self.nodes.get(rid) or self.edges.get(rid) or self.onto.nodes.get(rid)

    def default_visibility(self, kind: str) -> str:
        decl = (self.pack.get("kinds") or {}).get(kind)
        if isinstance(decl, dict):
            return str(decl.get("visibility") or "shared")
        return self.onto.registry.default_visibility(kind) or "shared"

    def symmetric(self, rel: str, ns: Optional[str] = None) -> bool:
        """Whether ``rel`` is symmetric, read with import ``ns``'s own declaration when it has one."""
        if ns and self.onto.registry.relation("%s/%s" % (ns, rel)) is not None:
            return self.onto.registry.is_symmetric(rel, ns)
        decl = (self.pack.get("relations") or {}).get(rel)
        if isinstance(decl, dict):
            return bool(decl.get("symmetric"))
        return self.onto.registry.is_symmetric(rel)

    def edge_record(self, eid: str) -> Optional[Dict[str, Any]]:
        """The edge an id names now: a local one as changed so far, else an imported one."""
        found = self.edges.get(eid)
        if found is None and eid in self.onto.edge_origin:
            return self.onto.edges.get(eid)
        return found

    get = edge_record  # the lookup ``ids.edge_id_in`` uses


# a write never drops lines it could not read -------------------------------------------------------------------
DAMAGE_CODES = ("P01", "P06")
DAMAGE_HINT = ("(a merge conflict?); resolve them as AGENTS.md describes under \"Merging topic branches\", run onto "
               "validate, then retry. Nothing was written")


def damaged(onto: Ontology, files: Sequence[str]) -> List[Problem]:
    """The load problems that make a rewrite of ``files`` lossy: unreadable lines (P01), two lines with one id and
    other content (P06), and records without an id or with a field of the wrong type (P02 at load: ``graph._shaped``
    reads it as empty, and a rewrite would write that). A rewrite keeps only what parsed, so writing
    would silently drop the other lines, such as the other side of a git merge conflict."""
    wanted = set(files)
    # a local edge under an imported edge's id is P06 too, but a rewrite keeps it (validate --fix drops it)
    shadow = {onto.lines.get(eid) for eid in getattr(onto, "shadowed", ())}
    return [p for p in onto.problems if p.file in wanted and (p.code in DAMAGE_CODES or (
        p.code == "P02" and ("without an id" in p.message or SHAPE_NOTE in p.message)))
        and not (p.code == "P06" and (p.file, p.line) in shadow)]


def refuse_damaged(onto: Ontology, files: Sequence[str]) -> None:
    """``Refused`` when a file about to be rewritten has lines a rewrite would drop (see ``damaged``)."""
    found = damaged(onto, files)
    if found:
        names = sorted({p.file for p in found})
        raise Refused("%s %s unreadable or conflicting lines %s: %s" % (
            " and ".join(names), "has" if len(names) == 1 else "have", DAMAGE_HINT,
            "; ".join(p.text() for p in found[:3])), problems=found)


def _local_check(state: _State, rid: str, n: Any, what: str = "") -> Dict[str, Any]:
    rec = state.nodes.get(rid) or state.edges.get(rid)
    if rec is None:
        if rid in state.onto.nodes or rid in state.onto.edges:
            raise Refused("op %s: %s is imported and read-only; change it in its own topic" % (n, rid))
        raise Refused("op %s: %s does not exist%s" % (n, rid, what))
    if rec.get("status") == "archived":
        raise Refused("op %s: %s is archived, and archived is final" % (n, rid))
    return rec


def _promote(rec: Dict[str, Any], status: Optional[str], trust: Optional[str]) -> None:
    """An accept (``status`` confirmed) confirms a draft record and may raise its trust. A draft never raises it: the
    record's text (a note, a name) was not reviewed, so text only an untrusted source wrote keeps its marker (C.7)."""
    if status != "confirmed":
        return
    if rec.get("status") == "proposed":
        rec["status"] = "confirmed"
    rec["trust"] = _raise_trust(rec.get("trust"), trust)


def _set_paths(rec: Dict[str, Any], op: Dict[str, Any], allowed: Sequence[str], n: Any, kind: str) -> List[str]:
    changed: List[str] = []
    for path, value in sorted((op.get("set") or {}).items()):
        if not (path in allowed or (kind == "node" and path.startswith("attrs."))):
            raise Refused("op %s: %s cannot be set on a %s" % (n, path, kind))
        if path.startswith("attrs."):
            attrs = rec.setdefault("attrs", {})
            if attrs.get(path[6:]) != value:
                attrs[path[6:]] = copy.deepcopy(value)
                changed.append(path)
            continue
        if rec.get(path) == value:
            continue
        if path == "name" and isinstance(value, str):
            old = rec.get("name")
            aliases = list(rec.get("aliases") or [])
            if old and util.name_key(str(old)) != util.name_key(value) and old not in aliases:
                aliases.append(old)
            rec["aliases"] = [a for a in aliases if a != value]
        rec[path] = copy.deepcopy(value)
        changed.append(path)
    for path in op.get("unset") or []:
        if path.startswith("attrs.") and kind == "node":
            if path[6:] in (rec.get("attrs") or {}):
                rec["attrs"].pop(path[6:])
                changed.append(path)
        elif path == "summary" and kind == "node":
            if rec.get("summary"):
                rec["summary"] = ""
                changed.append(path)
        elif path == "aliases" and kind == "node":
            if rec.get("aliases"):
                rec["aliases"] = []
                changed.append(path)
        elif (path == "gaps" or path.startswith("gaps.")) and kind == "node":
            # closing a recorded gap (a known unknown that is known now, or no longer matters)
            kept = [g for g in rec.get("gaps") or []
                    if path != "gaps" and not (isinstance(g, dict) and g.get("field") == path[5:])]
            if kept != list(rec.get("gaps") or []):
                rec["gaps"] = kept
                changed.append(path)
        elif path == "note" and kind == "edge":
            if rec.get("note"):
                rec["note"] = ""
                changed.append(path)
        elif path == "background" and kind == "edge":
            if rec.get("background"):
                rec["background"] = False
                changed.append(path)
        else:
            raise Refused("op %s: %s cannot be unset on a %s" % (n, path, kind))
    return changed


# the ops -------------------------------------------------------------------------------------------------------
def _add_node(state: _State, op: Dict[str, Any], n: Any) -> Dict[str, Any]:
    draft = op.get("node") or {}
    nid = draft.get("id")
    kind = draft.get("kind")
    if not isinstance(nid, str) or not ids.is_local(nid):
        raise Refused("op %s: add_node needs a local id, got %r" % (n, nid))
    if nid.split(":", 1)[0] != kind:
        raise Refused("op %s: the id %s does not match kind %r" % (n, nid, kind))
    if nid in state.nodes or nid in state.onto.nodes:
        raise Refused("op %s: %s exists already; use update_node" % (n, nid))
    row = {
        "id": nid,
        "kind": kind,
        "name": draft.get("name"),
        "summary": draft.get("summary") or "",
        "status": op.get("status") or "proposed",
        "trust": op.get("trust") or "untrusted",
        "conf": op.get("conf", 0.7),
        "visibility": draft.get("visibility") or state.default_visibility(str(kind)),
        "attrs": copy.deepcopy(draft.get("attrs") or {}),
        "aliases": _dedupe(draft.get("aliases") or []),
        "gaps": copy.deepcopy(draft.get("gaps") or []),
        "prov": _dedupe_prov(op.get("prov") or []),
        "created": state.today,
        "updated": state.today,
        "change": None,
        "archived": None,
    }
    state.nodes[nid] = row
    state.copied.add(nid)
    state.created.add(nid)
    state.touch(nid)
    return {"id": nid, "status": row["status"]}


def _edge_row(state: _State, src: str, rel: str, dst: str, key: str) -> Tuple[str, str, str]:
    """(id, src, dst) of an edge: endpoints sorted for a symmetric relation, and the id extended past any other edge
    that holds its 12-hex hash (C.2), so a colliding edge is never folded into an unrelated one."""
    ns = state.onto.ns_of(src)
    if state.symmetric(rel, ns if ns != "self" and ns == state.onto.ns_of(dst) else None) and src > dst:
        src, dst = dst, src
    return ids.edge_id_in(state, src, rel, dst, key), src, dst


def _add_edge(state: _State, op: Dict[str, Any], n: Any) -> Dict[str, Any]:
    draft = op.get("edge") or {}
    src, rel, dst = str(draft.get("src") or ""), str(draft.get("rel") or ""), str(draft.get("dst") or "")
    key = str(draft.get("key") or "")
    for end in (src, dst):
        if end.startswith("$") or end.startswith("self/"):
            raise Refused("op %s: endpoint %s is not resolved" % (n, end))
    eid, src, dst = _edge_row(state, src, rel, dst, key)
    if eid not in state.edges and eid in state.onto.edge_origin:
        raise Refused("op %s: the edge %s is imported from %s and read-only; change it in its own topic"
                      % (n, eid, state.onto.edge_origin[eid][0]))
    if eid in state.edges:
        existing = state.edges[eid]
        if existing.get("status") == "archived":
            raise Refused("op %s: the edge %s exists and is archived, and archived is final" % (n, eid))
        row = state.edge(eid)
        row["prov"] = _dedupe_prov(list(row.get("prov") or []) + list(op.get("prov") or []))
        _promote(row, op.get("status"), op.get("trust"))
        # the op's note is never copied onto the existing edge: a note changes only through update_edge, which
        # asks for a reason on a confirmed edge and keeps a draft's trust (C.7, C.12)
        return {"id": eid, "status": row["status"], "existed": True}
    row = {
        "id": eid,
        "src": src,
        "rel": rel,
        "dst": dst,
        "key": key,
        "status": op.get("status") or "proposed",
        "trust": op.get("trust") or "untrusted",
        "conf": op.get("conf", 0.7),
        "note": draft.get("note") or "",
        "background": bool(draft.get("background")),
        "prov": _dedupe_prov(op.get("prov") or []),
        "created": state.today,
        "updated": state.today,
        "change": None,
        "archived": None,
    }
    state.edges[eid] = row
    state.copied.add(eid)
    state.created.add(eid)
    state.touch(eid)
    return {"id": eid, "status": row["status"]}


def _update(state: _State, op: Dict[str, Any], n: Any, kind: str) -> Dict[str, Any]:
    rid = str(op.get("id") or "")
    _local_check(state, rid, n)
    rec = state.node(rid) if kind == "node" else state.edge(rid)
    changed = _set_paths(rec, op, NODE_PATHS if kind == "node" else EDGE_PATHS, n, kind)
    if op.get("prov"):
        rec["prov"] = _dedupe_prov(list(rec.get("prov") or []) + list(op.get("prov") or []))
    _promote(rec, op.get("status"), op.get("trust"))
    draft_trust = op.get("draft_trust")
    sets = op.get("set") or {}
    if draft_trust in TRUST_RANK and any(p in sets and (p in TEXT_PATHS or p.startswith("attrs.")) for p in changed):
        if TRUST_RANK[str(draft_trust)] < TRUST_RANK.get(str(rec.get("trust")), -1):
            rec["trust"] = draft_trust  # the new text is only as trusted as the draft that wrote it (C.7)
    return {"id": rid, "status": rec.get("status"), "changed": changed}


def _archive_block(state: _State, reason: str, decision: Optional[str], superseded_by: Sequence[str]) -> Dict[str, Any]:
    return {"on": state.today, "reason": reason, "decision": decision, "superseded_by": list(superseded_by)}


def _archive_edges_of(state: _State, nid: str, block: Dict[str, Any], reason: str) -> List[str]:
    done: List[str] = []
    for eid in sorted(state.edges):
        edge = state.edges[eid]
        if edge.get("status") == "archived" or nid not in (edge.get("src"), edge.get("dst")):
            continue
        row = state.edge(eid)
        row["status"] = "archived"
        row["archived"] = dict(block, reason=reason[:600])
        done.append(eid)
    return done


def merge_blocked(eid: str, new_id: str, target: Dict[str, Any], keep: str, drop: str) -> str:
    """Why a merge of ``drop`` into ``keep`` cannot move the edge ``eid``: on ``keep`` it would be ``new_id``, which
    is archived (and archived is final), so the active fact would leave the graph."""
    block = target.get("archived") if isinstance(target.get("archived"), dict) else {}
    why = " under %s" % block["decision"] if block.get("decision") else ""
    return ("merging %s into %s would move the active edge %s onto %s (%s -%s-> %s), which is archived%s, and archived "
            "is final. Archive %s first with its own reason, or keep both nodes"
            % (drop, keep, eid, new_id, target.get("src"), target.get("rel"), target.get("dst"), why, eid))


def _merge(state: _State, op: Dict[str, Any], n: Any) -> Dict[str, Any]:
    keep, drop = str(op.get("keep") or ""), str(op.get("drop") or "")
    if keep == drop:
        raise Refused("op %s: merge needs two different nodes" % n)
    _local_check(state, keep, n)
    _local_check(state, drop, n)
    if keep not in state.nodes or drop not in state.nodes:
        raise Refused("op %s: merge works on nodes only" % n)
    moved: List[str] = []
    archived: List[str] = []
    for eid in sorted(state.edges):
        edge = state.edges[eid]
        if edge.get("status") == "archived" or drop not in (edge.get("src"), edge.get("dst")):
            continue
        src = keep if edge.get("src") == drop else str(edge.get("src"))
        dst = keep if edge.get("dst") == drop else str(edge.get("dst"))
        rel, key = str(edge.get("rel")), str(edge.get("key") or "")
        old = state.edge(eid)
        if src == dst:
            old["status"] = "archived"
            old["archived"] = _archive_block(state, "merged away: it would link %s to itself" % keep, None, [keep])
            archived.append(eid)
            continue
        new_id, src, dst = _edge_row(state, src, rel, dst, key)
        target = state.edges.get(new_id)
        if target is not None and target.get("status") == "archived":
            # moving the edge onto an archived one would retire an active fact while claiming it moved
            raise Refused("op %s: %s" % (n, merge_blocked(eid, new_id, target, keep, drop)))
        if target is not None:
            row = state.edge(new_id)
            row["prov"] = _dedupe_prov(list(row.get("prov") or []) + list(old.get("prov") or []))
            _promote(row, old.get("status"), old.get("trust"))
        else:
            row = copy.deepcopy(old)
            row.update({"id": new_id, "src": src, "dst": dst, "created": state.today, "updated": state.today,
                        "change": None, "archived": None})
            state.edges[new_id] = row
            state.copied.add(new_id)
            state.created.add(new_id)
            state.touch(new_id)
        old["status"] = "archived"
        old["archived"] = _archive_block(state, "moved to %s by the merge of %s into %s" % (new_id, drop, keep),
                                         None, [new_id])
        moved.append(new_id)
        archived.append(eid)
    kept = state.node(keep)
    dropped = state.node(drop)
    aliases = list(kept.get("aliases") or [])
    extra = [drop]
    if util.name_key(str(dropped.get("name") or "")) != util.name_key(str(kept.get("name") or "")):
        extra.append(dropped.get("name"))
    extra += list(dropped.get("aliases") or [])
    for alias in extra:
        if isinstance(alias, str) and alias and alias not in aliases and alias != kept.get("name"):
            aliases.append(alias)
    kept["aliases"] = aliases
    kept["prov"] = _dedupe_prov(list(kept.get("prov") or []) + list(dropped.get("prov") or []))
    reason = str(op.get("reason") or "").strip()
    if len(reason) < 20:
        reason = "merged into %s as a duplicate" % keep
    dropped["status"] = "archived"
    dropped["archived"] = _archive_block(state, reason[:600], None, [keep])
    return {"id": keep, "merged": drop, "moved": moved, "archived": archived}


def _archive(state: _State, op: Dict[str, Any], n: Any) -> Dict[str, Any]:
    rid = str(op.get("id") or "")
    _local_check(state, rid, n)
    given = op.get("archived") or {}
    reason = str(given.get("reason") or "").strip()
    _require_active(state, given.get("decision"), n)
    block = _archive_block(state, reason, given.get("decision"), given.get("superseded_by") or [])
    if rid in state.nodes:
        rec = state.node(rid)
        rec["status"] = "archived"
        rec["archived"] = block
        edges = _archive_edges_of(state, rid, block, "archived with %s: %s" % (rid, reason))
        return {"id": rid, "status": "archived", "edges_archived": edges}
    rec = state.edge(rid)
    rec["status"] = "archived"
    rec["archived"] = block
    return {"id": rid, "status": "archived"}


def _add_gap(state: _State, op: Dict[str, Any], n: Any) -> Dict[str, Any]:
    nid = str(op.get("id") or "")
    _local_check(state, nid, n)
    if nid not in state.nodes:
        raise Refused("op %s: gaps sit on nodes only" % n)
    gap = {"field": (op.get("gap") or {}).get("field"), "note": (op.get("gap") or {}).get("note") or ""}
    rec = state.node(nid)
    gaps = list(rec.get("gaps") or [])
    if gap not in gaps:
        gaps.append(gap)
    rec["gaps"] = gaps
    return {"id": nid, "gap": gap["field"]}


def _pack_op(state: _State, op: Dict[str, Any], n: Any) -> Dict[str, Any]:
    try:
        state.pack = packs.apply_op(state.pack, op)
    except UsageError as exc:
        raise Refused("op %s: %s" % (n, exc.message))
    state.pack_changed = True
    kind = op.get("op")
    if kind == "add_kind":
        return {"kind": op.get("name")}
    if kind == "add_relation":
        return {"relation": op.get("name")}
    if kind == "add_field":
        return {"kind": op.get("kind"), "field": op.get("field")}
    return {"map": [op.get("a"), op.get("b")]}


def _add_question(state: _State, op: Dict[str, Any], n: Any) -> Dict[str, Any]:
    question = copy.deepcopy(op.get("question") or {})
    errors = packs.check_question(question)
    if errors:
        raise Refused("op %s: the question fails its schema: %s" % (n, errors[0]), problems=errors)
    qid = question.get("id")
    for row in state.questions:
        if row.get("id") == qid:
            if util.canonical_line(row) == util.canonical_line(question):
                return {"question": qid, "existed": True}
            raise Refused("op %s: question %s exists already with other content" % (n, qid))
    if any(q.get("id") == qid for q in state.onto.registry.questions()):
        raise Refused("op %s: question %s is already in a loaded question bank" % (n, qid))
    state.questions.append(question)
    state.questions_changed = True
    return {"question": qid}


ERASED_NAME = "[erased]"
ROOT_ERASE = ("%s is the topic's own node and is never erased (every record hangs off it, and archived is final); "
              "to take a text out of its summary, record a decision whose scope names it, then run onto erase --scrub "
              "<text> --decision <dec>")


def root_id(repo: store.Repo) -> str:
    """The topic's own node, ``topic:<ns>``: init creates it and every record links up to it."""
    return "topic:%s" % repo.ns


def _drop_quotes(rec: Dict[str, Any], src: Optional[str] = None) -> int:
    """Remove the ``quote`` of every provenance entry (citing ``src`` only, when given); returns how many."""
    count = 0
    out = []
    for p in rec.get("prov") or []:
        if isinstance(p, dict) and "quote" in p and (src is None or p.get("src") == src):
            p = {k: v for k, v in p.items() if k != "quote"}
            count += 1
        out.append(p)
    rec["prov"] = out
    return count


def _require_active(state: _State, decision: Any, n: Any) -> None:
    """An archive or erase cites a decision that is active when it is applied (validate later accepts one that was
    superseded since, while its chain resolves)."""
    if decision is None:
        return
    found = ledger.all_decisions(state.repo).get(str(decision))
    if found is None:
        raise Refused("op %s: decision %s is not in the ledger" % (n, decision))
    if found.get("status") != "active":
        raise Refused("op %s: decision %s is not active (superseded by %s)" % (n, decision, found.get("superseded_by")))


def _erase_node(state: _State, op: Dict[str, Any], n: Any) -> Dict[str, Any]:
    nid = str(op.get("id") or "")
    decision = op.get("decision")
    if not isinstance(decision, str) or not ids.DEC_RE.match(decision):
        raise Refused("op %s: erase needs the decision that allows it" % n)
    _require_active(state, decision, n)
    if nid not in state.nodes:
        if nid in state.onto.nodes:
            raise Refused("op %s: %s is imported; erase it in its own topic" % (n, nid))
        raise Refused("op %s: %s does not exist" % (n, nid))
    if nid == root_id(state.repo):
        raise Refused("op %s: %s" % (n, ROOT_ERASE % nid))
    rec = state.node(nid)
    names = {str(v) for v in [rec.get("name")] + list(rec.get("aliases") or []) if isinstance(v, str)}
    names.discard(ERASED_NAME)
    scrubbed = _drop_quotes(rec)
    rec.update({"name": ERASED_NAME, "summary": "", "attrs": {}, "aliases": [], "gaps": [], "visibility": "local",
                "erased": True})
    block = _archive_block(state, "erased for privacy under %s" % decision, decision, [])
    if rec.get("status") != "archived":
        rec["status"] = "archived"
        rec["archived"] = block
    archived: List[str] = []
    touching: List[str] = []
    for eid in sorted(state.edges):
        edge = state.edges[eid]
        if nid not in (edge.get("src"), edge.get("dst")):
            continue
        touching.append(eid)
        row = state.edge(eid)
        scrubbed += _drop_quotes(row)
        if row.get("note"):
            row["note"] = ""  # a note on the node's own edges is about the node
        if row.get("status") != "archived":
            row["status"] = "archived"
            row["archived"] = dict(block)
            archived.append(eid)
    every_name = set(names)
    scrubbed += _scrub_proposal_node(state, nid, names, set(touching), every_name)
    return {"id": nid, "erased": True, "quotes_scrubbed": scrubbed, "edges_archived": archived,
            "names_scrubbed_in": _scrub_ledger_names(state, every_name),
            "id_kept_in": _files_naming(state, nid, touching), "id_note": ID_NOTE % nid}


def _erase_source(state: _State, op: Dict[str, Any], n: Any) -> Dict[str, Any]:
    sid = str(op.get("src") or "")
    decision = op.get("decision")
    if not isinstance(decision, str) or not ids.DEC_RE.match(decision):
        raise Refused("op %s: erase needs the decision that allows it" % n)
    _require_active(state, decision, n)
    if state.source_rows is None:
        state.source_rows = [dict(r) for r in state.onto.sources.values()]
    row = next((r for r in state.source_rows if r.get("id") == sid), None)
    if row is None:
        raise Refused("op %s: %s is not in the sources index" % (n, sid))
    row["erased"] = True
    row["title"] = ERASED_NAME  # a title or url often names a person (a call title, a profile link)
    row["url"] = None
    if isinstance(row.get("original"), dict) and row["original"].get("stored"):
        row["original"] = dict(row["original"], stored=False)
    state.files[sources.TEXT_NAME % sid] = ("[erased by %s]\n" % decision).encode("utf-8")
    folder = state.repo.path("sources")
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if name.startswith(sid + ".orig."):
            state.files["sources/" + name] = None
    scrubbed = 0
    for rid in sorted(state.nodes):
        if any(isinstance(p, dict) and p.get("src") == sid and "quote" in p for p in state.nodes[rid].get("prov") or []):
            scrubbed += _drop_quotes(state.node(rid), sid)
    for rid in sorted(state.edges):
        if any(isinstance(p, dict) and p.get("src") == sid and "quote" in p for p in state.edges[rid].get("prov") or []):
            scrubbed += _drop_quotes(state.edge(rid), sid)
    found, touched, closed = _scrub_proposal_source(state, sid)
    scrubbed += found
    if sid not in state.other_ids:
        state.other_ids.append(sid)
    return {"id": sid, "erased": True, "quotes_scrubbed": scrubbed, "names_scrubbed_in": touched,
            "proposals_closed": closed}


def _scrub_prov_list(value: Any, sid: str) -> int:
    """Drop the ``quote`` of every provenance entry citing ``sid`` anywhere inside ``value`` (in place)."""
    count = 0
    if isinstance(value, dict):
        prov = value.get("prov")
        if isinstance(prov, list):
            for i, p in enumerate(prov):
                if isinstance(p, dict) and p.get("src") == sid and "quote" in p:
                    prov[i] = {k: v for k, v in p.items() if k != "quote"}
                    count += 1
        for key, item in value.items():
            if key != "prov":
                count += _scrub_prov_list(item, sid)
    elif isinstance(value, list):
        for item in value:
            count += _scrub_prov_list(item, sid)
    return count


def _proposal_docs(state: _State) -> List[Tuple[str, Dict[str, Any]]]:
    """``(rel, proposal)`` for every readable proposal file, as this change has left it so far (an earlier erase op
    of the same change may have rewritten it already, or moved it from pending to done)."""
    out: List[Tuple[str, Dict[str, Any]]] = []
    for folder in ("proposals/pending", "proposals/done"):
        path = state.repo.path(folder)
        names = set(os.listdir(path)) if os.path.isdir(path) else set()
        names |= {rel[len(folder) + 1:] for rel in state.files if rel.startswith(folder + "/")}
        for name in sorted(names):
            rel = "%s/%s" % (folder, name)
            if not name.endswith(".json"):
                continue
            try:
                if rel in state.files:
                    data = state.files[rel]
                    doc = None if data is None else json.loads(data.decode("utf-8"))
                else:
                    doc = store.read_json(state.repo.path(rel), None)
            except (DataError, ValueError):
                continue  # validate reports it (P21)
            if isinstance(doc, dict):
                out.append((rel, doc))
    return out


def _cites(op: Any, sid: str) -> bool:
    return isinstance(op, dict) and any(isinstance(p, dict) and p.get("src") == sid for p in op.get("prov") or [])


def _scrub_op_text(op: Dict[str, Any], names: Set[str]) -> None:
    """Clear the text a proposal op drafted from an erased source: a new node's name (``[erased]``), summary,
    aliases, attrs and gaps, the values an update sets, an edge's note and a gap's note. The names it held join
    ``names``; its annotations lose what they copied (expected values, a conflict, the matches)."""
    kind = op.get("op")
    if kind == "add_node" and isinstance(op.get("node"), dict):
        node = op["node"]
        names.update(v for v in [node.get("name")] + list(node.get("aliases") or []) if isinstance(v, str))
        node.update({"name": ERASED_NAME, "summary": "", "aliases": [], "attrs": {}, "gaps": []})
    elif kind in ("update_node", "update_edge") and isinstance(op.get("set"), dict):
        sets = op["set"]
        for path in list(sets):
            if path == "name":
                names.add(str(sets[path]))
                sets[path] = ERASED_NAME
            elif path == "aliases":
                names.update(v for v in sets[path] or [] if isinstance(v, str))
                sets[path] = []
            elif path in ("summary", "note"):
                sets[path] = ""
            elif path.startswith("attrs."):
                sets[path] = None
    elif kind == "add_edge" and isinstance(op.get("edge"), dict):
        op["edge"]["note"] = ""
    elif kind == "add_gap" and isinstance(op.get("gap"), dict):
        op["gap"]["note"] = ""
    annot = op.get("annot")
    if isinstance(annot, dict):
        annot["conflict"] = None
        if isinstance(annot.get("matches"), list):
            annot["matches"] = []
        expect = annot.get("expect")
        if isinstance(expect, dict):
            for path in list(expect):
                if path in ("name", "summary", "aliases", "note") or path.startswith("attrs."):
                    expect[path] = None


def _scrub_proposal_source(state: _State, sid: str) -> Tuple[int, List[str], List[str]]:
    """G.6 for a source, in the proposal files: every quote citing it goes (``_scrub_prov_list``), and so does the
    text the ops citing it drafted from it, wherever that text never reached the graph (an open proposal, a rejected
    one, a rejected op of an applied one): names, summaries, notes and attrs (``_scrub_op_text``), the quotes a check
    failed to find, and those names in the proposal's other free text. An open proposal that cites it, or that it
    is the source of, is closed as ``superseded``: nothing may be applied from a forgotten source (pipeline P11).
    Returns ``(quotes dropped, ids of the proposals changed, ids of those closed)``."""
    count = 0
    touched: List[str] = []
    closed: List[str] = []
    for rel, doc in _proposal_docs(state):
        before = util.canonical_bytes(doc)
        count += _scrub_prov_list(doc, sid)
        review = doc.get("review") if isinstance(doc.get("review"), dict) else {}
        verdicts = review.get("verdicts") if isinstance(review.get("verdicts"), dict) else {}
        edits = review.get("edits") if isinstance(review.get("edits"), dict) else {}
        applied = doc.get("status") == "applied"
        own = doc.get("source") == sid
        names: Set[str] = set()
        cited = False
        for op in doc.get("ops") or []:
            if not isinstance(op, dict):
                continue
            key = str(op.get("n"))
            edit = edits.get(key)
            if not (own or _cites(op, sid) or _cites(edit, sid)):
                continue
            cited = True
            if applied and verdicts.get(key) != "reject":
                continue  # that text is in the graph now, as a record of its own (erase that record to forget it)
            _scrub_op_text(op, names)
            if isinstance(edit, dict):
                _scrub_op_text(edit, names)
        checks = doc.get("checks") if isinstance(doc.get("checks"), dict) else {}
        quotes = checks.get("quotes") if isinstance(checks.get("quotes"), dict) else {}
        for item in quotes.get("failed") if isinstance(quotes.get("failed"), list) else []:
            if isinstance(item, dict) and item.get("src") == sid and "quote" in item:
                item.pop("quote")
        names = {n for n in names if isinstance(n, str) and n.strip() and n != ERASED_NAME}
        if names:
            for container, key, role, _path in records.text_slots(doc, "proposal"):
                if role in (records.LINE, records.BLOCK):
                    container[key] = _replace_names(container[key], sorted(names))
            keys = {util.name_key(n) for n in names}
            if isinstance(doc.get("new_terms"), list):
                doc["new_terms"] = [t for t in doc["new_terms"] if util.name_key(str(t)) not in keys]
            for group in (checks.get("problems"), checks.get("warnings")):
                for item in group if isinstance(group, list) else []:
                    if isinstance(item, dict) and isinstance(item.get("message"), str):
                        item["message"] = _replace_names(item["message"], sorted(names))
        if own and not applied:
            doc["summary"] = ERASED_NAME
        if (cited or own) and doc.get("status") in ("pending", "reviewed"):
            doc["status"] = "superseded"
            state.files[rel] = None
            rel = "proposals/done/%s" % rel.rsplit("/", 1)[-1]
            closed.append(str(doc.get("id")))
        after = util.canonical_bytes(doc)
        if after != before:
            state.files[rel] = after
            touched.append(str(doc.get("id")))
    return count, touched, closed


ID_NOTE = ("Ids are permanent, so the id %s (a slug of the old name) stays in the files listed as still naming it: "
           "the graph, the change log, decisions and proposals.")
NAME_MIN = 3  # shorter names are not replaced inside free text (they would match inside other words)


def text_pattern(text: str) -> "re.Pattern[str]":
    """``text`` as a pattern that ignores case and matches any run of spacing where it has one (the finder ignores
    both, so the scrub must too)."""
    return re.compile(r"\s+".join(re.escape(w) for w in str(text).split()), re.I)


def _replace_names(text: str, names: Sequence[str]) -> str:
    out = text
    for name in sorted(names, key=len, reverse=True):
        if len(name.strip()) >= NAME_MIN:
            out = text_pattern(name).sub(ERASED_NAME, out)
    return out


def created_id(op: Dict[str, Any], results: Any, key: Any) -> Any:
    """The node an ``add_node`` op of a proposal created: the id its applied result records under op number
    ``key`` (the id is assigned again at apply time, so a planned id taken since became ``-2``), else the id the
    op gives or the kit planned for it (a pending proposal, a rejected op, or a recovered apply)."""
    found = results.get(str(key)) if isinstance(results, dict) else None
    if isinstance(found, dict) and isinstance(found.get("id"), str):
        return found["id"]
    node = op.get("node") if isinstance(op.get("node"), dict) else {}
    annot = op.get("annot") if isinstance(op.get("annot"), dict) else {}
    return node.get("id") or annot.get("assigned_id")


def _scrub_node_op(op: Dict[str, Any], targets: Set[str], edge_ids: Set[str], names: Set[str],
                   created: Any = None) -> Optional[int]:
    """Clear what one proposal op says about an erased node (``targets``: its id and the ``$ref`` handles naming it)
    or about its edges; returns the quotes dropped, or None when the op is not about them. ``created`` is the id an
    ``add_node`` op created (``created_id``). The names it held join ``names``."""
    kind = op.get("op")
    annot = op.get("annot") if isinstance(op.get("annot"), dict) else {}
    hit = False
    if kind == "add_node":
        node = op.get("node") if isinstance(op.get("node"), dict) else {}
        if created in targets:
            names.update(v for v in [node.get("name")] + list(node.get("aliases") or []) if isinstance(v, str))
            node.update({"name": ERASED_NAME, "summary": "", "aliases": [], "attrs": {}, "gaps": []})
            hit = True
    elif kind == "update_node" and op.get("id") in targets:
        sets = op.get("set") if isinstance(op.get("set"), dict) else {}
        for path in list(sets):
            if path == "name":
                names.add(str(sets[path]))
                sets[path] = ERASED_NAME
            elif path == "summary":
                sets[path] = ""
            elif path == "aliases":
                names.update(v for v in sets[path] or [] if isinstance(v, str))
                sets[path] = []
            elif path.startswith("attrs."):
                sets[path] = None
        hit = True
    elif kind == "add_edge":
        edge = op.get("edge") if isinstance(op.get("edge"), dict) else {}
        if edge.get("src") in targets or edge.get("dst") in targets:
            edge["note"] = ""
            hit = True
    elif kind == "update_edge" and op.get("id") in edge_ids:
        if isinstance(op.get("set"), dict) and "note" in op["set"]:
            op["set"]["note"] = ""
        hit = True
    elif kind in ("merge", "archive") and targets & {op.get("keep"), op.get("drop"), op.get("id")}:
        hit = True
    if not hit:
        return None
    if annot:
        annot["conflict"] = None
        expect = annot.get("expect")
        if isinstance(expect, dict):
            for path in list(expect):
                if path in ("name", "summary", "aliases", "note") or path.startswith("attrs."):
                    expect[path] = None
    return _drop_quotes(op) if isinstance(op.get("prov"), list) else 0


def _scrub_proposal_node(state: _State, nid: str, names: Set[str], edge_ids: Set[str],
                         every_name: Optional[Set[str]] = None) -> int:
    """G.6 for a node: the proposal ops that created or changed it (and the notes of edges on it) lose its name,
    summary, aliases, attrs and quotes, and its names leave the proposal's other free text, its new terms and its
    check messages, in the same all-or-nothing write. Returns the quotes dropped; ``every_name`` gathers the names
    the node held in any proposal (an alias only a proposal knows)."""
    count = 0
    for rel, doc in _proposal_docs(state):
        review = doc.get("review") if isinstance(doc.get("review"), dict) else {}
        edits = review.get("edits") if isinstance(review.get("edits"), dict) else {}
        results = (doc.get("applied") or {}).get("results") if isinstance(doc.get("applied"), dict) else None
        keyed = [(op.get("n"), op) for op in doc.get("ops") or [] if isinstance(op, dict)]
        keyed += [(k, op) for k, op in edits.items() if isinstance(op, dict)]
        made = {id(op): created_id(op, results, key) if op.get("op") == "add_node" else None for key, op in keyed}
        targets = {nid}
        for _key, op in keyed:
            if op.get("op") == "add_node" and op.get("ref") and made[id(op)] == nid:
                targets.add(str(op["ref"]))
        found = set(names)
        before = util.canonical_bytes(doc)
        hits = [_scrub_node_op(op, targets, edge_ids, found, made[id(op)]) for _key, op in keyed]
        if all(h is None for h in hits):
            continue  # a proposal about other records keeps its text
        count += sum(h for h in hits if h)
        found.discard(ERASED_NAME)
        if every_name is not None:
            every_name.update(n for n in found if isinstance(n, str))
        if found:
            for container, key, role, _path in records.text_slots(doc, "proposal"):
                if role in (records.LINE, records.BLOCK):
                    container[key] = _replace_names(container[key], sorted(found))
            keys = {util.name_key(n) for n in found}
            if isinstance(doc.get("new_terms"), list):
                doc["new_terms"] = [t for t in doc["new_terms"] if util.name_key(str(t)) not in keys]
            checks = doc.get("checks") if isinstance(doc.get("checks"), dict) else {}
            for group in (checks.get("problems"), checks.get("warnings"),
                          (checks.get("quotes") or {}).get("failed") if isinstance(checks.get("quotes"), dict)
                          else None):
                for item in group if isinstance(group, list) else []:
                    for key in ("message", "quote"):
                        if isinstance(item, dict) and isinstance(item.get(key), str):
                            item[key] = _replace_names(item[key], sorted(found))
        after = util.canonical_bytes(doc)
        if after != before:
            state.files[rel] = after
    return count


DECISION_TEXT = ("question", "chosen_text", "rationale")
CHECKPOINT_TEXT = ("done", "next", "open_questions")


def _scrub_texts(value: Any, names: Sequence[str]) -> Any:
    if isinstance(value, str):
        return _replace_names(value, names)
    if isinstance(value, list):
        return [_scrub_texts(v, names) for v in value]
    return value


def _scrub_ledger_names(state: _State, names: Set[str]) -> List[str]:
    """G.6 beyond the graph and the proposals: an erased node's names leave the free text of the decisions (the
    question, option labels, the chosen text and the rationale) and of the change log (summaries, and a checkpoint's
    done, next and open questions), in the same all-or-nothing write. Ids stay (they are permanent). Returns the
    decision ids and change ids whose text changed. A change log with unreadable lines is left as it is (a rewrite
    would drop them); validate reports those lines."""
    root = state.onto.nodes.get(root_id(state.repo)) or {}
    own = {util.name_key(str(v)) for v in [root.get("name")] + list(root.get("aliases") or []) if isinstance(v, str)}
    # the topic's own name is never scrubbed: the setup decisions and the log name it, and it is not erased
    wanted = sorted(n for n in names if isinstance(n, str) and len(n.strip()) >= NAME_MIN and n != ERASED_NAME
                    and util.name_key(n) not in own)
    if not wanted:
        return []
    keys = [n.strip().lower() for n in wanted]

    def names_one(data: Optional[bytes]) -> bool:  # a cheap test before a file is parsed
        text = data.decode("utf-8", "replace").lower() if data is not None else ""
        return any(k in text for k in keys)

    out: List[str] = []
    folder = state.repo.path("ledger/decisions")
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        rel = "ledger/decisions/" + name
        if not name.endswith(".json"):
            continue
        data = state.files.get(rel) if rel in state.files else _snapshot(state.repo.path(rel))
        if not names_one(data):
            continue
        try:
            doc = util.loads_record(data.decode("utf-8"))  # type: ignore[union-attr]
        except ValueError:
            continue  # validate reports it (P01)
        if not isinstance(doc, dict):
            continue
        before = util.canonical_bytes(doc)
        for key in DECISION_TEXT:
            if isinstance(doc.get(key), str):
                doc[key] = _replace_names(doc[key], wanted)
        if not doc.get("options") and isinstance(doc.get("chosen"), str):
            doc["chosen"] = _replace_names(doc["chosen"], wanted)  # a free-text decision: chosen is the answer
        for opt in doc.get("options") if isinstance(doc.get("options"), list) else []:
            if isinstance(opt, dict) and isinstance(opt.get("label"), str):
                opt["label"] = _replace_names(opt["label"], wanted)
        after = util.canonical_bytes(doc)
        if after != before:
            state.files[rel] = after
            out.append(str(doc.get("id") or name[:-5]))
    data = _snapshot(ledger.changes_path(state.repo))
    if data is None or not names_one(data):
        return out
    _rows, bad = store.read_jsonl_lines(ledger.changes_path(state.repo))
    if bad:
        return out
    lines = data.split(b"\n")
    changed = False
    for i, raw in enumerate(lines):
        if not raw.strip() or not names_one(raw):
            continue
        row = util.loads_record(raw.decode("utf-8"))
        new = dict(row)
        for key in ("summary",) + CHECKPOINT_TEXT:
            if key in new:
                new[key] = _scrub_texts(new[key], wanted)
        if new != row:
            lines[i] = util.canonical_line(new).encode("utf-8")
            changed = True
            out.append(str(row.get("id")))
    if changed:
        state.files[ledger.CHANGES] = b"\n".join(lines)
    return out


def _files_naming(state: _State, nid: str, edge_ids: Sequence[str]) -> List[str]:
    """The files that still spell an erased node's id once the erase is written (ids are permanent)."""
    out = [NODES] + ([EDGES] if edge_ids else [])
    needle = nid.encode("utf-8")
    for rel in ([ledger.CHANGES] + sorted(
            "ledger/decisions/" + n for n in (os.listdir(state.repo.path("ledger/decisions"))
                                              if os.path.isdir(state.repo.path("ledger/decisions")) else [])
            if n.endswith(".json"))):
        data = _snapshot(state.repo.path(rel))
        if rel == ledger.CHANGES or (data is not None and needle in data):
            out.append(rel)
    for rel, doc in _proposal_docs(state):
        if needle in util.canonical_bytes(doc):
            out.append(rel)
    return out


# taking a text out of the fields that hold it (onto erase --scrub) ---------------------------------------------
NAME_FIELDS = ("name", "aliases")


def _holds(value: Any, needle: str) -> bool:
    return isinstance(value, str) and needle in util.normalize_ws(value).lower()


def _scrub_record(rec: Dict[str, Any], shape: str, pattern: "re.Pattern[str]", needle: str) -> Tuple[int, int]:
    """Rewrite ``needle`` to ``[erased]`` in the free text of one record (a node's summary, attrs and gap notes, an
    edge's note, an archive reason) and drop the provenance quotes that hold it; names and aliases stay (an erase
    takes a name out). Returns ``(texts rewritten, quotes dropped)``."""
    texts = quotes = 0
    for container, key, role, path in records.text_slots(rec, shape):
        if path and path[0] in NAME_FIELDS + ("key",):
            continue
        value = container[key]
        if not _holds(value, needle):
            continue
        if role == records.QUOTE:
            container.pop(key)
            quotes += 1
        else:
            container[key] = pattern.sub(ERASED_NAME, value)
            texts += 1
    archived = rec.get("archived")
    if isinstance(archived, dict) and _holds(archived.get("reason"), needle):
        archived["reason"] = pattern.sub(ERASED_NAME, archived["reason"])
        texts += 1
    return texts, quotes


def scrub_targets(onto: Ontology, text: str) -> List[str]:
    """The local nodes and edges whose free text (not a name or an alias) or provenance quotes hold ``text``."""
    needle = util.normalize_ws(str(text or "")).lower()
    pattern = text_pattern(text)
    out: List[str] = []
    for rid, shape in [(nid, "node") for nid in onto.local_ids] + [(eid, "edge") for eid in onto.local_edge_ids]:
        rec = copy.deepcopy(onto.nodes[rid] if shape == "node" else onto.edges[rid])
        if sum(_scrub_record(rec, shape, pattern, needle)):
            out.append(rid)
    return out


def _scrub_text(state: _State, op: Dict[str, Any], n: Any) -> Dict[str, Any]:
    """``scrub_text``: ``{text, decision, ids}`` (``onto erase --scrub``; never from a proposal). The text becomes
    ``[erased]`` in the free text of the records ``ids`` names, the quotes holding it are dropped, and it leaves the
    proposals' free text and quotes, the decisions and the change log. The records stay, with their names."""
    decision = op.get("decision")
    if not isinstance(decision, str) or not ids.DEC_RE.match(decision):
        raise Refused("op %s: a scrub needs the decision that allows it" % n)
    _require_active(state, decision, n)
    text = util.normalize_ws(str(op.get("text") or ""))
    if len(text) < FIND_MIN:
        raise Refused("op %s: give at least %d characters to scrub" % (n, FIND_MIN))
    needle, pattern = text.lower(), text_pattern(text)
    texts = quotes = 0
    changed: List[str] = []
    for rid in op.get("ids") or []:
        rid = str(rid)
        if rid in state.nodes:
            shape, rec = "node", copy.deepcopy(state.nodes[rid])
        elif rid in state.edges:
            shape, rec = "edge", copy.deepcopy(state.edges[rid])
        else:
            raise Refused("op %s: %s is not a local node or edge" % (n, rid))
        t, q = _scrub_record(rec, shape, pattern, needle)
        if not t + q:
            continue
        target = state.node(rid) if shape == "node" else state.edge(rid)
        target.clear()
        target.update(rec)
        texts, quotes = texts + t, quotes + q
        changed.append(rid)
    touched: List[str] = []
    for rel, doc in _proposal_docs(state):
        if needle not in util.normalize_ws(util.canonical_bytes(doc).decode("utf-8", "replace")).lower():
            continue
        before = util.canonical_bytes(doc)
        for container, key, role, _path in records.text_slots(doc, "proposal"):
            if _holds(container[key], needle):
                if role == records.QUOTE:
                    container.pop(key)
                    quotes += 1
                elif role in (records.LINE, records.BLOCK):
                    container[key] = pattern.sub(ERASED_NAME, container[key])
        checks = doc.get("checks") if isinstance(doc.get("checks"), dict) else {}
        for group in (checks.get("problems"), checks.get("warnings"),
                      (checks.get("quotes") or {}).get("failed") if isinstance(checks.get("quotes"), dict) else None):
            for item in group if isinstance(group, list) else []:
                for key in ("message", "quote"):
                    if isinstance(item, dict) and _holds(item.get(key), needle):
                        item[key] = pattern.sub(ERASED_NAME, item[key])
        after = util.canonical_bytes(doc)
        if after != before:
            state.files[rel] = after
            touched.append(str(doc.get("id")))
    return {"text_scrubbed_in": changed, "texts_rewritten": texts, "quotes_scrubbed": quotes,
            "names_scrubbed_in": touched + _scrub_ledger_names(state, {text})}


# finding text before an erase ----------------------------------------------------------------------------------
FIND_MIN = 3
FIND_WHAT = (  # (path prefix, what it is), the first match wins
    (NODES, "graph node"), (EDGES, "graph edge"), (sources.INDEX, "the source index: a title or url"),
    ("sources/", "source text"), ("proposals/", "proposal"), ("ledger/decisions/", "decision"),
    (ledger.CHANGES, "change log"), (history.HISTORY, "richness history"), ("interview/", "interview log"),
    ("packs/", "local pack"), ("imports/", "an import, read-only here: erase it in its own topic"),
    ("build/", "a build output: onto build writes it again"), ("ontology.json", "the topic manifest"),
    ("VERSIONS.md", "the release notes"), ("MANIFEST.json", "the release manifest"),
    ("inbox/", "the drop folder, local and not in git: delete the file"),
    (".onto/", "the agent's scratch files (answer.txt, ops.json, setup.json), local and not in git: delete the file"),
)


def _what(rel: str) -> str:
    return next((label for prefix, label in FIND_WHAT if rel == prefix or rel.startswith(prefix)), "a topic file")


def _string_paths(value: Any, needle: str, path: str = "") -> List[str]:
    """The paths (``summary``, ``prov[0].quote``) of the strings inside ``value`` that hold ``needle``."""
    out: List[str] = []
    if isinstance(value, str):
        if needle in util.normalize_ws(value).lower():
            out.append(path or "$")
    elif isinstance(value, dict):
        for key in sorted(value):
            out.extend(_string_paths(value[key], needle, "%s.%s" % (path, key) if path else str(key)))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            out.extend(_string_paths(item, needle, "%s[%d]" % (path, i)))
    return out


def _quoted_sources(row: Dict[str, Any], paths: Sequence[str]) -> List[str]:
    """The sources whose provenance quote holds the text (``prov[i].quote`` paths of a record)."""
    out: List[str] = []
    prov = row.get("prov") if isinstance(row.get("prov"), list) else []
    for path in paths:
        m = re.match(r"^prov\[([0-9]+)\]\.quote$", path)
        if m and int(m.group(1)) < len(prov) and isinstance(prov[int(m.group(1))], dict):
            sid = str(prov[int(m.group(1))].get("src") or "")
            if sid and sid not in out:
                out.append(sid)
    return out


def _find_in(rel: str, data: bytes, needle: str) -> List[Dict[str, Any]]:
    text = data.decode("utf-8", "replace")
    if needle not in util.normalize_ws(text).lower():
        return []
    hits: List[Dict[str, Any]] = []
    m = re.match(r"^sources/(src-[0-9a-f]{12})\.", rel)
    if rel.endswith(".jsonl"):
        for n, raw in enumerate(text.split("\n"), start=1):
            if needle not in util.normalize_ws(raw).lower():
                continue
            try:
                row = util.loads_record(raw)
            except ValueError:
                row = None
            if not isinstance(row, dict):
                hits.append({"file": rel, "line": n, "id": None, "where": []})
                continue
            paths = _string_paths(row, needle)
            hit = {"file": rel, "line": n, "id": row.get("id") if isinstance(row.get("id"), str) else None,
                   "where": paths}
            quoted = _quoted_sources(row, paths)
            if quoted:
                hit["quotes"] = quoted
            hits.append(hit)
        return hits or [{"file": rel, "line": 0, "id": None, "where": []}]  # the text spans lines
    if rel.endswith(".json"):
        try:
            doc = util.loads_record(text)
        except ValueError:
            doc = None
        rid = doc.get("id") if isinstance(doc, dict) and isinstance(doc.get("id"), str) else None
        return [{"file": rel, "line": 0, "id": rid, "where": _string_paths(doc, needle) if doc is not None else []}]
    where = ["L%d" % n for n, raw in enumerate(text.split("\n"), start=1) if needle in util.normalize_ws(raw).lower()]
    return [{"file": rel, "line": 0, "id": m.group(1) if m else None, "where": where}]


def find_text(repo: store.Repo, text: str, raw: bool = True) -> Dict[str, Any]:
    """Every place in the topic that holds ``text`` (spacing and case ignored), read only: the graph records (with
    the field: a name, a summary, a provenance quote), the source texts, titles and urls, the proposals (pending
    and done), the decisions, the change log, the local pack and questions, the manifest, the release files, the
    built outputs, the vendored imports, the local drop folder (``inbox/``) and the agent's scratch folder
    (``.onto/``, where answers are written before ``onto answer`` reads them). ``onto search`` reads names and
    summaries only; this is the finder to run before an erase ("forget this person"). Returns ``{text, hits:
    [{file, line, id, where, what, quotes?}], erase: [ids an erase of which takes the text out], raw}``.

    ``raw=False`` (``onto_search find=true`` over MCP) leaves out the files that hold text the redactor never saw:
    ``inbox/``, ``.onto/`` and the kept originals (``sources/<src>.orig.*``), so the finder is no way to test a
    guess against unredacted input; the CLI finder reads them too."""
    needle = util.normalize_ws(str(text or "")).lower()
    if len(needle) < FIND_MIN:
        raise UsageError("give at least %d characters to find" % FIND_MIN)
    files, links = store._topic_files(repo.root)
    if not raw:
        files = [f for f in files if not re.match(r"^sources/[^/]+\.orig\.", f)]
    for local in ("inbox", ".onto"):  # raw input and the agent's scratch copies of the user's words
        folder = repo.path(local)
        for dirpath, dirs, names in os.walk(folder) if raw and os.path.isdir(folder) else []:
            dirs.sort()
            for name in sorted(names):
                files.append(os.path.relpath(os.path.join(dirpath, name), repo.root).replace(os.sep, "/"))
    hits: List[Dict[str, Any]] = []
    for rel in files:
        path = repo.path(rel)
        if rel in links or not os.path.isfile(path) or os.path.islink(path):
            continue
        data = _snapshot(path)
        if data is None:
            continue
        for hit in _find_in(rel, data, needle):
            hit["what"] = _what(rel)
            hits.append(hit)
    erase: List[str] = []
    edit: List[str] = []
    root = root_id(repo)
    for hit in hits:
        targets: List[str] = list(hit.get("quotes") or [])
        rid = hit.get("id")
        where = hit.get("where") or []
        if isinstance(rid, str) and hit["file"].startswith("sources/"):
            targets.append(rid)
        elif isinstance(rid, str) and hit["file"] in (NODES, EDGES):
            # a name or an alias goes with an erase of the node; other free text (a summary, attrs, a note) is
            # taken out by onto erase --scrub, which keeps the record. The topic's own node is never erased.
            named = hit["file"] == NODES and any(_field_of(w) in NAME_FIELDS for w in where)
            text_field = any(_field_of(w) in SCRUB_FIELDS for w in where)
            if named and rid != root:
                targets.append(rid)
            elif (named or text_field) and rid not in edit:
                edit.append(rid)
        erase.extend(t for t in targets if t not in erase)
    edit = [rid for rid in edit if rid not in erase]
    return {"text": util.normalize_ws(str(text or "")), "hits": hits, "count": len(hits), "erase": erase,
            "edit": edit, "root_named": any(h.get("id") == root and h["file"] == NODES and any(
                _field_of(w) in NAME_FIELDS for w in h.get("where") or []) for h in hits), "raw": bool(raw)}


SCRUB_FIELDS = ("summary", "attrs", "gaps", "note", "archived")


def _field_of(path: str) -> str:
    """The top field of a ``_string_paths`` path: ``summary``, ``attrs`` (``attrs.owner``), ``aliases``
    (``aliases[0]``), ``prov`` (``prov[1].quote``)."""
    return re.split(r"[.\[]", path, maxsplit=1)[0]


APPLIERS = {
    "add_node": _add_node,
    "add_edge": _add_edge,
    "update_node": lambda s, o, n: _update(s, o, n, "node"),
    "update_edge": lambda s, o, n: _update(s, o, n, "edge"),
    "merge": _merge,
    "archive": _archive,
    "add_gap": _add_gap,
    "add_kind": _pack_op,
    "add_relation": _pack_op,
    "add_field": _pack_op,
    "map_kinds": _pack_op,
    "add_question": _add_question,
    "erase_node": _erase_node,
    "erase_source": _erase_source,
    "scrub_text": _scrub_text,
}


# expectations --------------------------------------------------------------------------------------------------
def expectation_failures(onto: Ontology, ops: Sequence[Dict[str, Any]]) -> List[Any]:
    """Op numbers whose ``annot.expect`` no longer matches the graph. ``change`` and ``status`` compare the
    target record (``drop`` for a merge); ``keep.change`` and ``keep.status`` compare a merge's ``keep``; any other
    key is an update path of the target."""
    failed: List[Any] = []
    for op in ops:
        expect = (op.get("annot") or {}).get("expect") or {}
        if not expect:
            continue
        target = op.get("drop") if op.get("op") == "merge" else op.get("id")
        rec = onto.record(str(target or "")) if target else None
        keep = onto.record(str(op.get("keep") or "")) if op.get("op") == "merge" else None
        ok = True
        for key, want in expect.items():
            if key.startswith("keep."):
                have = (keep or {}).get(key[5:]) if keep is not None else None
            elif rec is None:
                have = None
            elif key in ("change", "status"):
                have = rec.get(key)
            else:
                have = get_path(rec, key)
            if have != want:
                ok = False
                break
        if not ok:
            failed.append(op.get("n"))
    return failed


# the registry of a changed local pack --------------------------------------------------------------------------
def _registry_for(repo: store.Repo, manifest: Dict[str, Any], pack: Dict[str, Any],
                  questions: Sequence[Dict[str, Any]], exports: Dict[str, Dict[str, Any]]) -> packs.Registry:
    """The registry the topic would have with ``pack`` and ``questions`` as its local pack (built in memory;
    nothing in the repo is touched)."""
    extra: List[Tuple[str, Dict[str, Any]]] = []
    for ns in sorted(exports):
        for _name, block in sorted(((exports[ns].get("meta") or {}).get("packs") or {}).items()):
            if isinstance(block, dict) and isinstance(block.get("pack"), dict):
                extra.append((ns, block["pack"]))
    return packs.load(repo, manifest, extra, local_pack=pack, local_questions=list(questions))


# the change line and the history point -------------------------------------------------------------------------
def _change_id(repo: store.Repo, body: Dict[str, Any]) -> str:
    rows, _problems = store.read_jsonl(ledger.changes_path(repo))
    taken = {r.get("id") for r in rows if isinstance(r.get("id"), str)}
    return ids.record_id("chg", body, date=body["at"][:10], taken=taken)


def _counts_point(onto: Ontology, kind: str, reason: str) -> Dict[str, Any]:
    by_kind: Dict[str, int] = {}
    for nid in onto.local_nodes(active_only=True):
        k = onto.kind_of(nid)
        by_kind[k] = by_kind.get(k, 0) + 1
    by_rel: Dict[str, int] = {}
    for eid in onto.local_edge_ids:
        if onto.active(eid):
            rel = str(onto.edges[eid].get("rel"))
            by_rel[rel] = by_rel.get(rel, 0) + 1
    return history.new_point(
        kind, {"richness": None}, {},
        {"nodes_by_kind": by_kind, "edges_by_rel": by_rel, "sources": len(onto.sources)},
        {"richness": reason},
    )


def history_point(onto: Ontology, kind: str, in_flight: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """``richness.point(onto, kind)`` when that module is built, else a counts-only point. ``in_flight`` is the
    proposal being applied, with its final review, so the point neither counts it as pending nor misses its
    verdicts."""
    kind = kind if kind in HISTORY_KINDS else "apply"
    try:
        rich = _optional("richness")
    except Exception as exc:  # a broken measure module must not block a write; the reason is recorded
        return _counts_point(onto, kind, "richness failed to import: %s" % type(exc).__name__)
    if rich is not None and hasattr(rich, "point"):
        try:
            point = rich.point(onto, kind, in_flight=in_flight) if in_flight is not None else rich.point(onto, kind)
            if isinstance(point, dict) and not records.check(point, "point"):
                return point
            return _counts_point(onto, kind, "richness point failed its schema")
        except Exception as exc:  # a measure bug must not block a write; the reason is recorded
            return _counts_point(onto, kind, "richness failed: %s" % type(exc).__name__)
    return _counts_point(onto, kind, "richness module not built")


# apply ---------------------------------------------------------------------------------------------------------
def _snapshot(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def _restore(path: str, data: Optional[bytes]) -> None:
    if data is None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return
    store.write_bytes(path, data)


def _new_problems(old: Ontology, new: Ontology, touched: Sequence[str]) -> List[Problem]:
    before = {(p.code, p.message) for p in validate.check_graph(old, [t for t in touched if old.record(t)])}
    before |= {(p.code, p.message) for p in old.registry.problems()}
    found = [p for p in validate.check_graph(new, touched) if (p.code, p.message) not in before]
    found += [p for p in new.registry.problems() if (p.code, p.message) not in before]
    return found + new_calibration(old, new)


def new_calibration(old: Ontology, new: Ontology) -> List[Problem]:
    """The P23s ``new`` has and ``old`` does not. Calibration reads across records (a risk and the controls that
    mitigate it), so it covers the whole graph."""
    before = {(p.code, p.message) for p in validate.calibration_problems(old)}
    return [p for p in validate.calibration_problems(new) if (p.code, p.message) not in before]


Finish = Callable[[str, str, Dict[str, Any]], Sequence[Tuple[str, Optional[bytes]]]]


def _secret_kinds(rec: Dict[str, Any]) -> List[str]:
    return sorted({kind for kind, _match in secrets.scan_str(util.canonical_line(rec))})


def apply_ops(repo: store.Repo, resolved_ops: Sequence[Dict[str, Any]], *, by: str, change_type: str,
              summary: str, proposal_id: Optional[str] = None, source: Optional[str] = None,
              in_flight: Optional[Dict[str, Any]] = None, dry_run: bool = False,
              finish: Optional[Finish] = None) -> Dict[str, Any]:
    """Apply resolved ops as one change. Returns ``{change, results, ids, before, after}``; ``results`` is keyed by
    op number (as a string). Raises ``Conflict`` when an expectation fails and ``Refused`` when the result would
    not validate, when a file it rewrites holds lines it could not read (``refuse_damaged``) or when a changed
    record holds a credential; in every case nothing is written. ``in_flight`` is passed on to the history point.
    ``finish(change_id, at, results)`` returns more ``(rel, bytes or None)`` writes that belong to the same change
    (``pipeline.commit`` moves the proposal to ``proposals/done/`` this way); they land before the change line and
    are put back with everything else on a failure. ``dry_run`` writes nothing and returns ``{results, ids,
    problems}``: the problems the final graph check would refuse the change for."""
    if change_type not in ledger.CHANGE_TYPES:
        raise UsageError("change type must be one of %s" % ", ".join(ledger.CHANGE_TYPES))
    with store.write_lock(repo):
        repo.reload()
        check_format(repo)
        onto = Ontology.load(repo)
        refuse_damaged(onto, (NODES, EDGES))
        failed = expectation_failures(onto, resolved_ops)
        if failed:
            raise Conflict(
                "the data changed under op%s %s since the proposal was prepared; nothing was written. Prepare it "
                "again against the current data" % ("" if len(failed) == 1 else "s", ", ".join(str(n) for n in failed)),
                ops=failed,
            )
        state = _State(repo, onto)
        results: Dict[str, Any] = {}
        for i, op in enumerate(resolved_ops, start=1):
            n = op.get("n", i)
            applier = APPLIERS.get(str(op.get("op")))
            if applier is None:
                raise UsageError("op %s: unknown op %r" % (n, op.get("op")))
            results[str(n)] = applier(state, op, n)
        if state.source_rows is not None:
            refuse_damaged(onto, (sources.INDEX,))
        if state.questions_changed and state.question_problems:
            raise Refused("%s has unreadable lines %s: %s" % (
                packs.LOCAL_QUESTIONS, DAMAGE_HINT, "; ".join("line %d: %s" % lp for lp in state.question_problems[:3])))
        at = util.now_iso()
        before = store.data_hash(repo)
        touched = list(state.touched)
        for rid in touched:
            rec = state.nodes.get(rid) or state.edges.get(rid)
            kinds = _secret_kinds(rec) if rec is not None else []
            if kinds:
                raise Refused("%s would hold a credential (%s); nothing was written. Remove it and try again"
                              % (rid, ", ".join(kinds)), kinds=kinds)
        body: Dict[str, Any] = {
            "at": at,
            "type": change_type,
            "by": by if by in ("user", "agent", "kit") else "user",
            "proposal": proposal_id,
            "source": source,
            "summary": util.normalize_ws(summary)[:600],
            "ids": _dedupe(touched + state.other_ids),
            "before": before,
        }
        change_id = _change_id(repo, body)
        for rid in touched:
            rec = state.nodes.get(rid) or state.edges.get(rid)
            if rec is None:
                continue
            rec["change"] = change_id
            rec["updated"] = state.today
        registry = onto.registry
        if state.pack_changed or state.questions_changed:
            registry = _registry_for(repo, repo.manifest, state.pack, state.questions, onto.exports)
        lock = lockfile.read(repo)
        source_rows = state.source_rows if state.source_rows is not None else list(onto.sources.values())
        new = Ontology.from_rows(
            repo, repo.manifest, list(state.nodes.values()), list(state.edges.values()),
            source_rows, lock, onto.exports, registry=registry,
        )
        # a pack change can change how an import's kinds read here, so every local edge (the bridges) is checked
        checked = touched + ([e for e in onto.local_edge_ids if e not in touched] if state.pack_changed else [])
        problems = _new_problems(onto, new, checked)
        follow_ups: List[Problem] = []
        if change_type == "erase":
            # the privacy exception never waits on a risk's ratings: a P23 the erase leaves is reported as a
            # follow-up (link another control, or raise the residual), not a refusal
            follow_ups = [p for p in problems if p.code == "P23"]
            problems = [p for p in problems if p.code != "P23"]
        if dry_run:
            return {"results": results, "ids": _dedupe(touched + state.other_ids), "problems": problems}
        if problems:
            raise Refused(
                "the change would leave %d problem(s); nothing was written: %s"
                % (len(problems), "; ".join(p.text() for p in problems[:3])),
                problems=problems,
            )
        planned: List[Tuple[str, Optional[bytes]]] = []
        if state.source_rows is not None:
            planned.append((sources.INDEX, store.jsonl_bytes(state.source_rows, "id")))
        planned += sorted(state.files.items())
        if state.pack_changed:
            planned.append((packs.LOCAL_PACK, util.canonical_bytes(state.pack)))
        if state.questions_changed:
            planned.append((packs.LOCAL_QUESTIONS, store.jsonl_bytes(state.questions, "id")))
        planned.append((NODES, store.jsonl_bytes(list(state.nodes.values()), "id")))
        planned.append((EDGES, store.jsonl_bytes(list(state.edges.values()), "id")))
        if finish is not None:
            planned += list(finish(change_id, at, results))
        keep_paths = _dedupe([rel for rel, _data in planned] + [ledger.CHANGES, history.HISTORY])
        saved = {rel: _snapshot(repo.path(rel)) for rel in keep_paths}
        rewrites = [(rel, saved[rel], data) for rel, data in planned if saved[rel] != data]
        grows = {rel: saved[rel] for rel in (ledger.CHANGES, history.HISTORY)}
        grows.update({rel: data for rel, data in planned if rel in grows})  # an erase rewrites the log, then appends
        store.begin_write(repo, rewrites, sorted(grows.items()))
        try:
            for rel, data in planned:
                if data is None:
                    _restore(repo.path(rel), None)  # a planned delete
                elif saved[rel] != data:
                    store.write_bytes(repo.path(rel), data)
            after = store.data_hash(repo)
            change = dict(body, id=change_id, after=after)
            errors = records.check(change, "change")
            if errors:
                raise DataError("the change line fails its schema: %s" % "; ".join(errors[:3]))
            store.append_jsonl(ledger.changes_path(repo), change)
            history.append_point(repo, history_point(new, change_type, in_flight))
        except BaseException:
            for rel in keep_paths:
                _restore(repo.path(rel), saved[rel])
            store.end_write(repo)
            clear_graph_cache()
            raise
        store.end_write(repo)
        clear_graph_cache()
    out = {"change": change_id, "results": results, "ids": _dedupe(touched + state.other_ids), "before": before,
           "after": after}
    if follow_ups:
        out["follow_ups"] = ["%s %s" % (p.code, p.message) for p in follow_ups]
    return out


# packs ---------------------------------------------------------------------------------------------------------
def _local_clash(local_pack: Any, name: str) -> List[str]:
    """``kind risk``, ``relation mitigates``: what the topic's local pack declares under a name the built-in pack
    ``name`` declares too."""
    try:
        pack = store.read_json(os.path.join(packs.BUILTIN_DIR, "%s.pack.json" % name), {})
    except (DataError, ValueError):
        return []
    if not isinstance(local_pack, dict) or not isinstance(pack, dict):
        return []
    out = []
    for key, word in (("kinds", "kind"), ("relations", "relation")):
        theirs = set(pack.get(key) or {})
        out += ["%s %s" % (word, n) for n in sorted(set(local_pack.get(key) or {}) & theirs)]
    return out


def add_pack(repo: store.Repo, name: str, by: str = "user") -> Dict[str, Any]:
    """Turn on the built-in pack ``name``: list it in ``ontology.json`` (before ``local``) as one change (type
    ``pack``) with its history point, under the write lock and the write intent. Returns ``{pack, added, packs,
    change}``; ``added`` is False (and nothing is written) when the pack is already listed. ``Refused`` for a name
    that is not a built-in pack, and when the topic would read with new problems (a local kind or question of the
    same name, a kind the new pack would read differently)."""
    known = packs.builtin_names()
    if not isinstance(name, str) or name not in known:
        raise Refused("unknown pack %r; the built-in packs are %s (onto pack list)" % (name, ", ".join(known)),
                      available=known)
    with store.write_lock(repo):
        repo.reload()
        check_format(repo)
        listed = [n for n in (repo.manifest.get("packs") or list(packs.DEFAULT_PACKS)) if isinstance(n, str)]
        if name in listed:
            return {"pack": name, "added": False, "packs": listed, "change": None}
        new_packs = list(listed)
        new_packs.insert(new_packs.index(packs.LOCAL) if packs.LOCAL in new_packs else len(new_packs), name)
        manifest = dict(repo.manifest, packs=new_packs)
        onto = Ontology.load(repo)
        local_pack = store.read_json(repo.path(packs.LOCAL_PACK), None) if packs.LOCAL in new_packs else None
        questions, _bad = store.read_jsonl(repo.path(packs.LOCAL_QUESTIONS))
        registry = _registry_for(repo, manifest, local_pack or packs.empty_local_pack(), questions, onto.exports)
        new = Ontology.from_rows(repo, manifest, [onto.nodes[i] for i in onto.local_ids],
                                 [onto.edges[e] for e in onto.local_edge_ids], list(onto.sources.values()),
                                 lockfile.read(repo), onto.exports, registry=registry)
        problems = _new_problems(onto, new, list(onto.local_ids) + list(onto.local_edge_ids))
        if problems:
            clash = _local_clash(local_pack, name)
            way_out = ("; the local pack declares %s too, and no onto command removes a local name: with the user's "
                       "yes, take %s out of %s by hand (its records then read as the pack's), then run onto pack add "
                       "%s again" % (", ".join(clash), "it" if len(clash) == 1 else "them", packs.LOCAL_PACK, name)
                       if clash else "")
            raise Refused("adding pack %s would leave %d problem(s); nothing was written: %s%s"
                          % (name, len(problems), "; ".join(p.text() for p in problems[:3]), way_out),
                          problems=problems)
        at = util.now_iso()
        before = store.data_hash(repo)
        body: Dict[str, Any] = {"at": at, "type": "pack", "by": by if by in ("user", "agent", "kit") else "user",
                                "proposal": None, "source": None, "summary": "add pack %s" % name, "ids": [],
                                "before": before}
        change_id = _change_id(repo, body)
        data = util.canonical_bytes(manifest)
        keep = [store.MANIFEST, ledger.CHANGES, history.HISTORY]
        saved = {rel: _snapshot(repo.path(rel)) for rel in keep}
        store.begin_write(repo, [(store.MANIFEST, saved[store.MANIFEST], data)],
                          [(ledger.CHANGES, saved[ledger.CHANGES]), (history.HISTORY, saved[history.HISTORY])])
        try:
            store.write_bytes(repo.path(store.MANIFEST), data)
            repo.manifest = manifest
            change = dict(body, id=change_id, after=store.data_hash(repo))
            errors = records.check(change, "change")
            if errors:
                raise DataError("the change line fails its schema: %s" % "; ".join(errors[:3]))
            store.append_jsonl(ledger.changes_path(repo), change)
            history.append_point(repo, history_point(new, "pack"))
        except BaseException:
            for rel in keep:
                _restore(repo.path(rel), saved[rel])
            store.end_write(repo)
            clear_graph_cache()
            repo.reload()
            raise
        store.end_write(repo)
        clear_graph_cache()
    return {"pack": name, "added": True, "packs": new_packs, "change": change_id}


# init ----------------------------------------------------------------------------------------------------------
def _check_new_topic(name: str, ns: str, title: str, personal: Optional[str] = None) -> str:
    if not isinstance(ns, str) or not ids.NS_RE.match(ns) or ns in ids.RESERVED_NS:
        raise UsageError("--ns must be lower case letters, digits and '-', starting with a letter (at most 32), "
                         "and not self or imp; got %r" % (ns,))
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise UsageError("--name must be a lower case slug of letters, digits and '-' (at most 64); got %r" % (name,))
    clean = util.normalize_ws(title or "")
    if not clean or len(clean) > 200:
        raise UsageError("--title must be 1 to 200 characters")
    control = records.control_problem(clean, records.LINE)
    if control:  # the root node's name holds the title, and a name is one line of plain text
        raise UsageError("--title %s" % control)
    if personal is not None and personal not in PERSONAL_ACTIONS:
        raise UsageError("--personal must be keep, redact or refuse; got %r" % (personal,))
    return _clean_title(clean, store.personal_policy(personal))


def _clean_title(title: str, policy: Optional[Dict[str, Any]] = None) -> str:
    """The title through the rules source text follows, under the default policy a new topic starts with: a
    credential refuses it (naming only its kind), personal data the policy redacts is redacted. The title reaches
    ``ontology.json``, the init change line, every question template and the export's ``meta.title``, none of
    which an erase can reach, so it is cleaned before any of them is written."""
    kinds = sorted({kind for kind, _match in secrets.scan_str(title)})
    if kinds:
        raise Refused("--title holds credential-like text (%s); nothing was written" % ", ".join(kinds), kinds=kinds)
    try:
        clean, _found = sources.default_sanitizer()(title, policy or store.DEFAULT_POLICY)
    except Refused as exc:
        found = list(exc.extra.get("kinds") or []) if hasattr(exc, "extra") else []
        raise Refused("--title holds data that may not be stored (%s); nothing was written"
                      % (", ".join(found) or "see policy.personal"), kinds=found)
    return util.normalize_ws(clean)[:200] or title


def init_topic(path: str, name: str, ns: str, title: str, personal: Optional[str] = None) -> store.Repo:
    """Create a topic repo at ``path`` (B.2) and apply its root node ``topic:<ns>`` (confirmed, trust user, cited
    to an interview source holding the title). ``personal`` (keep, redact or refuse) sets ``policy.personal`` for
    every personal kind; None keeps the kit's defaults. Refused when ``ontology.json`` exists."""
    root = os.path.abspath(os.path.expanduser(path or os.getcwd()))
    if os.path.isfile(os.path.join(root, store.MANIFEST)):
        raise Refused("%s already holds ontology.json; this topic is set up (run onto status)" % root)
    title = _check_new_topic(name, ns, title, personal)
    made: List[str] = []

    def mkdir(folder: str) -> None:
        if not os.path.isdir(folder):
            os.makedirs(folder)
            made.append(folder)

    try:
        mkdir(root)
        for folder in store.TOPIC_DIRS + EXTRA_DIRS:
            mkdir(os.path.join(root, *folder.split("/")))
        manifest = store.new_manifest(name, ns, title, personal=personal)
        errors = records.check(manifest, "ontology_manifest")
        if errors:
            raise UsageError("ontology.json would fail its schema: %s" % "; ".join(errors[:3]))
        store.write_json(os.path.join(root, store.MANIFEST), manifest)
        store.write_json(os.path.join(root, *packs.LOCAL_PACK.split("/")), packs.empty_local_pack())
        store.write_bytes(os.path.join(root, *packs.LOCAL_QUESTIONS.split("/")), b"")
        repo = store.Repo.open(root)
        with store.write_lock(repo):
            entry, _duplicate = sources.add(repo, TOPIC_PREFIX + title + "\n", "interview", INIT_SOURCE_TITLE)
            first = sources.read(repo, entry["id"]).split("\n", 1)[0]
            stored = first[len(TOPIC_PREFIX):].strip() if first.startswith(TOPIC_PREFIX) else title
            node = {"id": "topic:%s" % ns, "kind": "topic", "name": (stored or title)[:200], "summary": ""}
            prov = [{"src": entry["id"], "loc": "L1-L1", "quote": stored[:300], "by": "user"}] if stored else \
                [{"src": entry["id"], "loc": "L1-L1", "by": "user"}]
            apply_ops(
                repo,
                [{"n": 1, "op": "add_node", "node": node, "status": "confirmed", "trust": "user", "conf": 1.0,
                  "prov": prov}],
                by="user", change_type="init", summary="topic created: %s" % title, source=entry["id"],
            )
    except BaseException:
        for rel in (store.MANIFEST, packs.LOCAL_PACK, packs.LOCAL_QUESTIONS):
            try:
                os.unlink(os.path.join(root, *rel.split("/")))
            except OSError:
                pass
        for folder in reversed(made):
            shutil.rmtree(folder, True)
        clear_graph_cache()
        raise
    return repo.reload()
