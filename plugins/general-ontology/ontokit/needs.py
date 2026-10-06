"""Per-node needs: the one computation behind cards, briefs, gaps and richness.

``needs(onto, id)`` lists what a node lacks (its gaps, most severe first) and the facts the richness measures read
(expected and filled fields, provenance, sources, quotes, degree, confirmation). Topic-level gaps
(``missing_dimension``, ``unbridged_import``, ``uncited_source``, ``duplicate``, ``pending_backlog``) are computed by
the richness module; their types and severities are declared here so every surface ranks gaps the same way.

Each gap is ``{type, severity}`` plus, where it applies, ``field``, ``rel``, ``dir``, ``note`` and ``ask`` (the
question or suggestion that closes it). An ``ask`` comes from a question-bank entry with ``for_gap`` when one exists,
else from the kind's ``expects[].ask`` or a default below; ``{name}`` and ``{topic}`` are filled in.

Background edges never count: not toward a relation the kind expects, not toward degree, and never as a gap.

A ``question`` node is open until an ``answers`` edge reaches it or an active decision's scope names it. A question
raised in the last ``RECENT_DAYS`` (created then, or asked and answered "still open" then) keeps its
``open_question`` gap, marked ``held``: gaps, context and briefs still list it, but the interview does not ask the
user back what they just said is undecided.

``bridge_end_gaps`` gives the gaps of the imported ends of bridges, local or brought by an import (only what the
bridges made), shared by the interview (stage 8) and ``onto gaps``. ``inherited_bridge_gaps`` gives a ``read_only``
``dangling_bridge`` gap for each active bridge an import brings whose other end the pins leave missing or archived;
``onto gaps`` lists it, but the interview never asks it, since only keeping the pin the owner was released with (or
the owner re-pointing the bridge and releasing again) fixes it.

A ``conflict`` (the members of a ``same_as`` class disagree on an attribute) is settled, and no longer a gap, only
by what was recorded for that conflict: an active decision whose scope names a member with the field
(``garden/process:repair#attrs.cadence``, as ``decide_scope`` spells it) and, when the decision stored the values it
settled (``settles``, filled by ``ledger.decide``), the same set of disagreeing values; or its gap question set n/a on
a member. A decision about anything else never settles it, and a new disagreeing value (an import update) opens it
again. The imported sides are read-only, so the disagreement itself never goes away. An answer to the gap question
records no choice by itself: the gap stays, marked ``answered`` with the answer's source, until a decision records
which value holds. ``settled_conflicts`` lists the settled ones with what settled them (a decision id, or ``n/a``:
the user left the conflict open), for a brief to show next to the values. A ``dangling_bridge`` to an end that upstream merged away names the replacement (``archived
upstream, now garden/crop:x``).

Node gaps settle too. A gap whose question (``gap_question_id``, as ``onto next`` prints it) was last answered or set
n/a leaves the needs: ``missing_field``, ``missing_relation``, ``orphan``, ``thin``, ``low_confidence``,
``unconfirmed_hub`` and the gaps recorded with ``add_gap`` (``open_question`` with a field). A recorded gap also
settles once its field is filled (``owner`` or ``attrs.owner``) or, when it names a relation, once the node has such
a link; ``update_node`` with ``unset: ["gaps.<field>"]`` removes it. The ``open_question`` of a question node keeps
its own rule (above).

A stale source names the tools that refresh it: its own ``refresh_with`` edges, the ``via`` tool that produced it,
and the ``refresh_with`` tools of the nodes that cite it (``dataset:x refresh_with tool:y``). A source a later one
supersedes is never stale (``stale_sources``, shared with ``onto status``).
"""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from . import ledger, store
from . import sources as sources_mod
from . import util

RECENT_DAYS = 2  # the interview's window: what was just asked or raised is not asked again at once
LOG = "interview/log.jsonl"
OPEN_QUESTION_Q = "q.gap.open_question"
GAP_PREFIX = "q.gap."
CLOSED = ("answered", "na")
WAITS = ("skipped", "later")  # a line that never replaces an answer (as in the interview's log fold)
# node gaps an answer (or n/a) to their gap question settles; conflicts, contradictions, bridges and the open
# question of a question node keep their own rules
ANSWER_SETTLES = ("missing_field", "missing_relation", "orphan", "thin", "low_confidence", "unconfirmed_hub",
                  "archived_premise")
PREMISE_KIND = "premise"
RESTS_ON = "rests_on"
QID_RE = re.compile(r"^q\.[a-z0-9][a-z0-9._-]{0,79}\Z")  # interview.QID_RE
DETAIL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}\Z")  # interview.DETAIL_RE

GAP_TYPES: Dict[str, Dict[str, Any]] = {
    "missing_dimension": {"severity": 9, "scope": "topic", "action": "ask the stage question"},
    "contradiction": {"severity": 8, "scope": "node", "action": "ask which is right"},
    "conflict": {"severity": 8, "scope": "node", "action": "ask which value is right"},
    "unconfirmed_hub": {"severity": 7, "scope": "node", "action": "ask to confirm"},
    "archived_premise": {"severity": 7, "scope": "node", "action": "ask whether it still holds"},
    "missing_field": {"severity": 6, "scope": "node", "action": "ask for the field"},
    "missing_relation": {"severity": 6, "scope": "node", "action": "ask the expected relation"},
    "unbridged_import": {"severity": 6, "scope": "topic", "action": "ask the stage C question"},
    "orphan": {"severity": 5, "scope": "node", "action": "ask how it relates"},
    "thin": {"severity": 5, "scope": "node", "action": "ask what it is"},
    "dangling_bridge": {"severity": 5, "scope": "node", "action": "review the bridge again"},
    "no_provenance": {"severity": 5, "scope": "node", "action": "ingest a source"},
    "single_source": {"severity": 4, "scope": "node", "action": "ingest another source"},
    "low_confidence": {"severity": 4, "scope": "node", "action": "ask to confirm"},
    "draft": {"severity": 3, "scope": "node", "action": "review the draft"},
    "draft_link": {"severity": 3, "scope": "node", "action": "ask to confirm the drafted links"},
    "stale_source": {"severity": 3, "scope": "node", "action": "refresh the source with its tool"},
    "open_question": {"severity": 3, "scope": "node", "action": "ask the question"},
    "uncited_source": {"severity": 3, "scope": "topic", "action": "extract from the source"},
    "duplicate": {"severity": 3, "scope": "topic", "action": "ask to confirm the merge"},
    "pending_backlog": {"severity": 2, "scope": "topic", "action": "review pending proposals"},
}

DEFAULT_ASKS = {
    "contradiction": "Which is right: {name} or {other}?",
    "conflict": "Which value of {field} is right for {name}: {note}?",
    "unconfirmed_hub": "Is {name} right as drafted? Many records in {topic} link to it.",
    "missing_field": "What is the {field} of {name}?",
    "orphan": "How does {name} connect to other things in {topic}?",
    "thin": "What should a newcomer know about {name}? One or two sentences are enough.",
    "dangling_bridge": "Is the link from {name} to {other} still right?",
    "no_provenance": "Where does what we know about {name} come from?",
    "single_source": "What else confirms {name}?",
    "low_confidence": "How sure are you about {name}?",
    "draft": "Review the draft {name}: keep, change or drop it?",
    "draft_link": "Are these drafted links of {name} right: {note}?",
    "stale_source": "Refresh {other}, which {name} cites{tool}.",
    "open_question": "{note}",
    "archived_premise": "{name} rests on {other}, which was archived. Does {name} still hold?",
}
LOW_CONFIDENCE = 0.5
HUB_DEGREE = 3
DRAFT_LINKS_SHOWN = 3


def _draft_link_items(onto: Any, node_id: str) -> List[Tuple[str, str]]:
    """``[(edge id, "<relation> <other name>")]`` of the active draft (``proposed``) local edges this node asks
    about: the edges it is the source of, and those whose source is not a local node."""
    local = onto._cache.get("local_edge_set")
    if local is None:
        local = onto._cache["local_edge_set"] = set(onto.local_edge_ids)
    out: List[Tuple[str, str]] = []
    for item in onto.edges_of(node_id):
        edge = item["edge"]
        if edge.get("status") != "proposed" or edge.get("id") not in local or _quiet(edge):
            continue
        src_local = onto.is_local(edge.get("src"))
        if item["direction"] == "out" or not src_local:
            other = onto.node(item["other"]) or {}
            out.append((str(edge.get("id")), "%s %s" % (item["label"].replace("_", " "), _plain_name(
                str(other.get("name") or item["other"])))))
    return out


def draft_links(onto: Any, node_id: str) -> List[str]:
    """``"<relation> <other name>"`` for each draft link of the node (``_draft_link_items``). Every set of draft
    links is asked about once: the question id carries a key of the set (``draft_link_key``), so a link drafted
    after an answer brings the question back and an inferred link never stays a draft that no question asks."""
    return sorted(text for _eid, text in _draft_link_items(onto, node_id))


def draft_link_key(edge_ids: Iterable[str]) -> str:
    """The question id detail of a set of draft links: 8 hex of sha256 over the sorted edge ids."""
    return hashlib.sha256("\n".join(sorted(set(edge_ids))).encode("utf-8")).hexdigest()[:8]


def id_detail(record_id: str) -> str:
    """A question id detail naming one record: its id in lower case with every other character a ``-``
    (``premise:well`` gives ``premise-well``), or ``h`` and 10 hex of its sha256 when that does not fit."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", str(record_id).lower()).strip("-")
    if DETAIL_RE.match(slug):
        return slug
    return "h" + hashlib.sha256(str(record_id).encode("utf-8")).hexdigest()[:10]


def _fill(template: str, **values: Any) -> str:
    out = template
    for key, value in values.items():
        out = out.replace("{%s}" % key, str(value if value is not None else ""))
    return util.normalize_ws(out)


def _gap_asks(onto: Any) -> Dict[str, str]:
    """``{gap type: ask}`` from question-bank entries carrying ``for_gap`` (the lowest id wins)."""
    cached = onto._cache.get("gap_asks")
    if cached is None:
        cached = {}
        for q in onto.registry.questions():
            gap = q.get("for_gap")
            if gap and gap not in cached and q.get("ask"):
                cached[gap] = q["ask"]
        onto._cache["gap_asks"] = cached
    return cached


def _plain_name(name: str) -> str:
    """A name as it reads inside an ask: a trailing ``?`` dropped (a question node's name is a question), so
    ``What else confirms {name}?`` never prints ``??``."""
    return name.rstrip().rstrip("?").rstrip() or name


def _ask(onto: Any, gap_type: str, name: str, **values: Any) -> str:
    template = _gap_asks(onto).get(gap_type) or DEFAULT_ASKS.get(gap_type) or "{name}"
    return _fill(template, name=_plain_name(name), topic=onto.title(), **values)


# decided and recently raised questions ------------------------------------------------------------------------
def decided_ids(onto: Any) -> Set[str]:
    """The ids an active decision's scope names (own-namespace spellings read as local): a question node there is
    decided, so it is no longer an open question."""
    cached = onto._cache.get("decided_ids")
    if cached is None:
        cached = set()
        decisions = onto._cache.get("decisions")
        if decisions is None and onto.repo is not None:
            try:
                decisions = ledger.all_decisions(onto.repo)
            except Exception:  # an unreadable decision is validate's to report (P21); needs must not fail
                decisions = {}
        for dec in (decisions or {}).values():
            if isinstance(dec, dict) and dec.get("status") == "active":
                for item in dec.get("scope") or []:
                    if isinstance(item, str):
                        cached.add(onto.own_local(item))
        onto._cache["decided_ids"] = cached
    return cached


def _open_question_answers(onto: Any) -> Dict[str, Any]:
    """``{node id: latest time}`` of interview answers (any status) to the open-question gap of a node."""
    cached = onto._cache.get("open_question_answers")
    if cached is None:
        cached = {}
        asks = {OPEN_QUESTION_Q} | {str(q.get("id")) for q in onto.registry.questions()
                                    if q.get("for_gap") == "open_question"}
        rows: List[Dict[str, Any]] = []
        if onto.repo is not None:
            rows, _problems = store.read_jsonl(onto.repo.path(LOG))
        for row in rows:
            q, node = str(row.get("q") or ""), row.get("node")
            if not isinstance(node, str) or not any(q == a or q.startswith(a + ".") for a in asks):
                continue
            try:
                at = util.parse_ts(str(row.get("at")))
            except ValueError:
                continue
            if node not in cached or at > cached[node]:
                cached[node] = at
        onto._cache["open_question_answers"] = cached
    return cached


def _gap_log(onto: Any) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """``{(question id, node): latest line}`` of the questions asked about a node, folded as the interview folds its
    log: a ``skipped`` or ``later`` line never replaces an ``answered`` one."""
    cached = onto._cache.get("gap_log")
    if cached is None:
        cached = {}
        rows: List[Dict[str, Any]] = []
        if onto.repo is not None:
            rows, _problems = store.read_jsonl(onto.repo.path(LOG))
        for row in rows:
            q, node = str(row.get("q") or ""), row.get("node")
            if not isinstance(node, str) or not q:
                continue
            prev = cached.get((q, node))
            if row.get("status") in WAITS and prev is not None and prev.get("status") == "answered":
                continue
            cached[(q, node)] = row
        onto._cache["gap_log"] = cached
    return cached


def _gap_templates(onto: Any) -> Dict[str, str]:
    """``{gap type: question id}``: the lowest bank id carrying ``for_gap`` (the interview's rule)."""
    cached = onto._cache.get("gap_templates")
    if cached is None:
        cached = {}
        for q in sorted(onto.registry.questions(), key=lambda x: str(x.get("id"))):
            gap = q.get("for_gap")
            if gap and gap not in cached and q.get("id"):
                cached[gap] = str(q["id"])
        onto._cache["gap_templates"] = cached
    return cached


def _detail(gap: Dict[str, Any]) -> str:
    """The id suffix that tells two gaps of one kind on one node apart (``interview._detail``)."""
    t = gap.get("type")
    if t in ("missing_field", "conflict") or (t == "open_question" and gap.get("field")):
        parts = [str(gap.get("field") or "").split(".")[-1]]
    elif t == "missing_relation":
        parts = [str(gap.get("rel") or ""), str(gap.get("dir") or "out")]
    elif t == "archived_premise" and gap.get("note"):  # one question per premise: a later one is asked too
        parts = [id_detail(str(gap["note"]))]
    elif t == "draft_link" and gap.get("key"):  # one question per set of draft links
        parts = [str(gap["key"])]
    else:
        return ""
    return "." + ".".join(parts) if all(DETAIL_RE.match(p) for p in parts) else ""


def gap_question_id(onto: Any, gap: Dict[str, Any]) -> str:
    """The question id of a node gap: its bank template's id (or ``q.gap.<type>``) plus the field or relation, as
    ``onto next`` prints it before ``@<node>``."""
    base = _gap_templates(onto).get(str(gap.get("type"))) or GAP_PREFIX + str(gap.get("type"))
    qid = base + _detail(gap)
    return qid if QID_RE.match(qid) else base


def gap_answer(onto: Any, node_id: str, gap: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The latest interview line that answered (or set n/a) this gap's question on ``node_id``, or None."""
    row = _gap_log(onto).get((gap_question_id(onto, gap), node_id))
    return row if row is not None and row.get("status") in CLOSED else None


def decide_scope(node_id: str, field: str) -> str:
    """The decision scope entry that settles a conflict: the member id and the field (``ns/kind:x#attrs.price``)."""
    return "%s#%s" % (node_id, field if str(field).startswith("attrs.") else "attrs.%s" % field)


def _split_scope(item: str) -> Tuple[str, str]:
    """``(id, short field)`` of a ``<id>#<field>`` scope entry (``price`` for ``attrs.price``); ``("", "")`` else."""
    rid, sep, field = item.partition("#")
    if not sep or not rid or not field:
        return "", ""
    return rid.strip(), field.strip().split(".")[-1]


def _field_decisions(onto: Any) -> List[Tuple[str, str, Optional[List[str]], str]]:
    """``[(record id, short field, the values it settled or None, decision id)]`` of the active decisions whose scope
    names a record with a field (``decide_scope``)."""
    cached = onto._cache.get("field_decisions")
    if cached is None:
        cached = []
        decisions = onto._cache.get("decisions")
        if decisions is None and onto.repo is not None:
            try:
                decisions = ledger.all_decisions(onto.repo)
            except Exception:  # an unreadable decision is validate's to report (P21); needs must not fail
                decisions = {}
        for dec in (decisions or {}).values():
            if not isinstance(dec, dict) or dec.get("status") != "active":
                continue
            settles = {}
            for item in dec.get("settles") or []:
                if isinstance(item, dict) and isinstance(item.get("values"), list):
                    key = (onto.own_local(str(item.get("id"))), str(item.get("field") or "").split(".")[-1])
                    settles[key] = sorted(str(v) for v in item["values"])
            for item in dec.get("scope") or []:
                rid, short = _split_scope(item) if isinstance(item, str) else ("", "")
                if rid:
                    rid = onto.own_local(rid)
                    cached.append((rid, short, settles.get((rid, short)), str(dec.get("id"))))
        onto._cache["field_decisions"] = cached
    return cached


def conflict_settler(onto: Any, node_id: str, field: str, values: Optional[Iterable[str]] = None) -> Optional[str]:
    """What settles the conflict on ``field`` of ``node_id``'s ``same_as`` class: the id of an active decision whose
    scope names a member with this field (``decide_scope``) and, when it stored the values it settled, whose values
    are still the disagreeing ``values``; ``"n/a"`` when the gap question was set n/a on a member (the user left the
    conflict open); else None. A decision about anything else, or an answer that recorded no choice, never settles
    it."""
    members = set(onto.members(node_id)) | {node_id}
    keys = members | {onto.own_local(m) for m in members}
    short = str(field or "").split(".")[-1]
    now = sorted(str(v) for v in values) if values is not None else None
    for rid, dec_field, settled, dec_id in _field_decisions(onto):
        if rid in keys and dec_field == short and (settled is None or now is None or settled == now):
            return dec_id
    gap = {"type": "conflict", "field": "attrs." + short}
    if any((_gap_log(onto).get((gap_question_id(onto, gap), m)) or {}).get("status") == "na" for m in members):
        return "n/a"
    return None


def conflict_settled(onto: Any, node_id: str, field: str, values: Optional[Iterable[str]] = None) -> bool:
    """True when ``conflict_settler`` names what settles the conflict."""
    return conflict_settler(onto, node_id, field, values) is not None


def settled_conflicts(onto: Any, node_id: str) -> List[Dict[str, Any]]:
    """``[{field, note, by}]`` for the settled conflicts of ``node_id``'s class, which ``needs`` leaves out: ``by`` is
    the decision that records which value holds, or ``"n/a"`` (the user left the conflict open), so a brief can say
    so next to the disagreeing values."""
    out = []
    for c in _conflicts(onto, node_id):
        by = conflict_settler(onto, node_id, c["field"], c["values"])
        if by is not None:
            out.append({"field": c["field"], "note": c["note"], "by": by})
    return out


def conflict_values(onto: Any, node_id: str, field: str) -> Optional[List[str]]:
    """The disagreeing values (canonical JSON) of ``field`` in ``node_id``'s ``same_as`` class, or None when its
    members agree (what ``ledger.decide`` stores in ``settles``)."""
    for c in _conflicts(onto, node_id):
        if c["field"].split(".")[-1] == str(field or "").split(".")[-1]:
            return list(c["values"])
    return None


def _conflict_answer(onto: Any, node_id: str, field: str) -> Optional[Dict[str, Any]]:
    """The latest line that answered a class's conflict question (status answered) on any member, or None."""
    gap = {"type": "conflict", "field": field}
    rows = [(_gap_log(onto).get((gap_question_id(onto, gap), m)) or {}) for m in sorted(onto.members(node_id))]
    found = [r for r in rows if r.get("status") == "answered"]
    return max(found, key=lambda r: str(r.get("at") or "")) if found else None


def question_held(onto: Any, node_id: str, node: Dict[str, Any]) -> bool:
    """True for a question raised in the last ``RECENT_DAYS``: the node was created then, or its open-question gap
    was asked and answered then (for example "still undecided")."""
    now = util.now()
    window = timedelta(days=RECENT_DAYS)
    try:
        if now - util.parse_ts(str(node.get("created"))) < window:
            return True
    except ValueError:
        pass
    last = _open_question_answers(onto).get(node_id)
    return last is not None and now - last < window


# bridge ends ---------------------------------------------------------------------------------------------------
def bridge_end_gaps(onto: Any) -> List[Tuple[str, Dict[str, Any]]]:
    """``[(imported node, its needs with only the bridge gaps)]`` for the active imported endpoints of the local
    bridges (active, non-background local edges between namespaces) and of the bridges an import brings (a composed
    topic's ``same_as``), sorted by id. The imported side is read-only, so only what the bridges made counts: the
    node's ``conflict`` gaps (a local decision settles one, whichever bridge formed the class; one an import's bridge
    formed carries that import as ``owner``) and the ``dangling_bridge`` gaps of the local bridges (those of an
    import's bridges are ``inherited_bridge_gaps``). ``interview._gap_targets`` asks by it in stage 8 and
    ``richness.ranked_gaps`` lists it, so ``gaps`` lists what ``next`` asks and what ``brief`` shows per node; nodes
    with no such gap are left out. Cached on the graph object."""
    cached = onto._cache.get("richness_bridge_ends")
    if cached is not None:
        return cached
    ends: Dict[str, Set[str]] = {}
    owners: Dict[str, str] = {}  # an end only an import's bridge reaches -> that import
    for eid in sorted(onto.bridges):
        edge = onto.edges.get(eid) or {}
        if not onto.active(eid) or edge.get("background"):
            continue
        local = eid in onto.lines
        origin = onto.edge_origin.get(eid)
        if not local and origin is None:
            continue
        src, dst = str(edge.get("src")), str(edge.get("dst"))
        for end, other in ((src, dst), (dst, src)):
            if onto.ns_of(end) == "self" or end in onto.virtual or not onto.active(end):
                continue
            if local:
                ends.setdefault(end, set()).add(other)
                owners.pop(end, None)
            elif end not in ends:
                owners.setdefault(end, str(origin[0]))  # type: ignore[index]
    out = []
    for nid in sorted(set(ends) | set(owners)):
        need = needs(onto, nid)
        mine = ends.get(nid, set())
        kept = [g for g in need.get("gaps") or [] if g.get("type") == "conflict" or (
            g.get("type") == "dangling_bridge" and str(g.get("note") or "").split(" ")[0] in mine)]
        if nid in owners:
            kept = [dict(g, owner=owners[nid]) for g in kept if g.get("type") == "conflict"]
        if kept:
            out.append((nid, dict(need, gaps=kept)))
    onto._cache["richness_bridge_ends"] = out
    return out


def inherited_bridge_gaps(onto: Any) -> List[Tuple[str, Dict[str, Any]]]:
    """``[(surviving end, {gaps, degree})]`` for the active bridges the imports bring (read-only here) whose other end
    is missing or archived under the current pins: one ``dangling_bridge`` gap each, marked ``read_only`` with its
    ``owner`` (the import that brings it). Sorted by end id; cached on the graph object."""
    cached = onto._cache.get("inherited_bridge_gaps")
    if cached is not None:
        return cached
    found: Dict[str, List[Dict[str, Any]]] = {}
    for eid in sorted(onto.bridges):
        origin = onto.edge_origin.get(eid)
        edge = onto.edges.get(eid) or {}
        if origin is None or eid in onto.lines or edge.get("status") == "archived" or _quiet(edge):
            continue
        owner = origin[0]
        src, dst = str(edge.get("src")), str(edge.get("dst"))
        for end, other in ((src, dst), (dst, src)):
            other_rec = onto.node(other)
            if other_rec is not None and other_rec.get("status") != "archived":
                continue
            if end in onto.virtual or onto.node(end) is None or not onto.active(end):
                continue
            why = "absent upstream" if other_rec is None else "archived upstream"
            block = (other_rec or {}).get("archived")
            now_ids = [str(x) for x in (block.get("superseded_by") or [])
                       if isinstance(x, str)] if isinstance(block, dict) else []
            if now_ids:
                why += ", now %s" % ", ".join(now_ids)
            note = "%s %s (bridge of %s, read-only: keep the pin %s was released with)" % (other, why, owner, owner)
            ask = ("The link from %s to %s comes with %s and is read-only here: keep the pin %s was released with, "
                   "or have %s re-point it and release again." % (onto.label(end) or end, other, owner, owner, owner))
            gap = _gap("dangling_bridge", rel=edge.get("rel"), note=note, ask=ask)
            gap.update(read_only=True, owner=owner, edge=eid)
            found.setdefault(end, []).append(gap)
    out = [(nid, {"id": nid, "gaps": found[nid], "degree": len(onto.edges_of(nid))}) for nid in sorted(found)]
    onto._cache["inherited_bridge_gaps"] = out
    return out


# stale sources -------------------------------------------------------------------------------------------------
def refresh_tool_items(onto: Any, src: str) -> List[Dict[str, Any]]:
    """``[{id, untrusted?, draft?}]``: the tools that refresh a source, most direct first (its own ``refresh_with``
    edges, the ``via`` tool that produced it, then the ``refresh_with`` tools of the active nodes that cite it). A
    tool is marked ``untrusted`` or ``draft`` when its node, or the link that names it, is (C.7)."""
    found: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []

    def add(tool: str, link: Optional[Dict[str, Any]]) -> None:
        if tool not in found:
            found[tool] = {"id": tool}
            order.append(tool)
        for rec in (onto.node(tool), link):
            if not rec:
                continue
            if rec.get("trust") == "untrusted":
                found[tool]["untrusted"] = True
            if rec.get("status") == "proposed":
                found[tool]["draft"] = True

    for e in onto.edges_of(src, "out", rels=["refresh_with"]):
        add(e["other"], e["edge"])
    via = (onto.sources.get(src) or {}).get("via")
    if isinstance(via, str) and via:
        add(via, None)
    for rid, _loc in sorted(set(onto.prov_index.get(src, []))):
        if rid in onto.virtual or not onto.active(rid) or rid not in onto.nodes:
            continue
        for e in onto.edges_of(rid, "out", rels=["refresh_with"]):
            add(e["other"], e["edge"])
    return [found[t] for t in order]


def refresh_tools(onto: Any, src: str) -> List[str]:
    """The ids of ``refresh_tool_items``."""
    return [t["id"] for t in refresh_tool_items(onto, src)]


def stale_sources(onto: Any, now: Any = None) -> List[Dict[str, Any]]:
    """``[{id, captured_at, stale_after_days, refresh_with, tools}]`` for the stale sources no later source
    supersedes; ``tools`` is ``refresh_tool_items`` (the ids with their markers)."""
    when = now or util.now()
    replaced = sources_mod.superseded(onto.sources)
    out = []
    for sid in sorted(onto.sources):
        entry = onto.sources[sid]
        if sid in replaced or not sources_mod.stale(entry, when):
            continue
        items = refresh_tool_items(onto, sid)
        out.append({"id": sid, "captured_at": entry.get("captured_at"),
                    "stale_after_days": entry.get("stale_after_days"), "refresh_with": [t["id"] for t in items],
                    "tools": items})
    return out


def _gap(gap_type: str, **fields: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": gap_type, "severity": GAP_TYPES[gap_type]["severity"]}
    for key in ("field", "rel", "dir", "note", "ask"):
        if fields.get(key) not in (None, ""):
            out[key] = fields[key]
    return out


def _filled(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool(value)
    return True


def _is_summary(text: Any) -> bool:
    body = str(text or "").strip()
    return bool(body) and not body.upper().startswith("TODO")


def _quiet(edge: Dict[str, Any]) -> bool:
    return bool(edge.get("background"))


def _class_edges(onto: Any, node_id: str) -> List[Dict[str, Any]]:
    """Active non-background edges of the node and of its ``same_as`` class members (the ``same_as`` links
    themselves left out)."""
    out = []
    seen = set()
    for member in onto.members(node_id):
        for e in onto.edges_of(member):
            edge = e["edge"]
            if _quiet(edge) or edge.get("id") in seen:
                continue
            if edge.get("rel") == "same_as" and onto.same_as.get(e["other"]) == onto.same_as.get(node_id):
                continue
            seen.add(edge.get("id"))
            out.append(dict(e, member=member))
    return out


def _expect_count(onto: Any, node_id: str, exp: Dict[str, Any], edges: List[Dict[str, Any]]) -> int:
    rel = exp.get("rel")
    want = exp.get("dir") or "out"
    count = 0
    for e in edges:
        if e["edge"].get("rel") != rel:
            continue
        if e["direction"] == want or onto.symmetric(e["edge"]):  # read with the edge's own declaration
            count += 1
    return count


def substantive(onto: Any, node_id: str) -> bool:
    """Summary non-empty and not ``TODO``, at least one provenance entry, and at least one edge."""
    node = onto.node(node_id)
    if node is None:
        return False
    return _is_summary(node.get("summary")) and bool(node.get("prov")) and onto.degree(node_id) >= 1


def _conflicts(onto: Any, node_id: str) -> List[Dict[str, Any]]:
    """Attribute keys on which the members of the node's ``same_as`` class disagree."""
    members = onto.members(node_id)
    if len(members) < 2:
        return []
    values: Dict[str, Dict[str, List[str]]] = {}
    for m in members:
        node = onto.node(m) or {}
        attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
        for key, value in sorted(attrs.items()):
            if not _filled(value):
                continue
            values.setdefault(key, {}).setdefault(util.canonical_line(value), []).append(m)
    out = []
    for key, by_value in sorted(values.items()):
        if len(by_value) < 2:
            continue
        parts = []
        for raw, ids in sorted(by_value.items()):
            spaces = sorted({onto.ns_of(i) for i in ids})
            parts.append("%s (%s)" % (raw.strip('"'), ", ".join(spaces)))
        out.append({"field": "attrs." + key, "note": " vs ".join(parts), "values": sorted(by_value)})
    return out


def needs(onto: Any, node_id: str) -> Dict[str, Any]:
    """What one node lacks, and the facts the richness measures read."""
    node = onto.node(node_id) or {}
    registry = onto.registry
    kind = onto.kind_of(node_id)
    name = str(node.get("name") or node_id)
    # a malformed record (attrs not an object) is P02 for validate, never a crash here
    attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
    gaps: List[Dict[str, Any]] = []
    expected = registry.expected(kind)
    filled = 0
    for field in expected:
        if _filled(attrs.get(field)):
            filled += 1
        else:
            gaps.append(_gap("missing_field", field="attrs." + field,
                             ask=_ask(onto, "missing_field", name, field=field.replace("_", " "))))
    edges = _class_edges(onto, node_id)
    met = required = 0
    for exp in registry.expects(kind):
        need = int(exp.get("min") or 1)
        have = _expect_count(onto, node_id, exp, edges)
        required += need
        met += min(have, need)
        if have < need:
            template = exp.get("ask") or _gap_asks(onto).get("missing_relation") or "What does {name} %s?" % exp.get("rel")
            gaps.append(_gap("missing_relation", rel=exp.get("rel"), dir=exp.get("dir") or "out",
                             note="%d of %d" % (have, need),
                             ask=_fill(template, name=_plain_name(name), topic=onto.title())))
    degree = onto.degree(node_id)
    status = node.get("status")
    prov = [p for p in (node.get("prov") if isinstance(node.get("prov"), list) else []) if isinstance(p, dict)]
    distinct = sorted({str(p.get("src")) for p in prov if p.get("src")})
    quotes = sum(1 for p in prov if str(p.get("quote") or "").strip())
    if not edges and not registry.is_hub(kind):
        gaps.append(_gap("orphan", ask=_ask(onto, "orphan", name)))
    if not _is_summary(node.get("summary")) and not any(_filled(v) for v in attrs.values()):
        gaps.append(_gap("thin", ask=_ask(onto, "thin", name)))
    if status == "proposed" and degree >= HUB_DEGREE:
        gaps.append(_gap("unconfirmed_hub", note="%d links" % degree, ask=_ask(onto, "unconfirmed_hub", name)))
    if status == "proposed" and not prov:
        gaps.append(_gap("no_provenance", ask=_ask(onto, "no_provenance", name)))
    if len(distinct) == 1:
        gaps.append(_gap("single_source", note=distinct[0], ask=_ask(onto, "single_source", name)))
    conf = node.get("conf")
    if isinstance(conf, (int, float)) and not isinstance(conf, bool) and conf < LOW_CONFIDENCE:
        gaps.append(_gap("low_confidence", note="conf %s" % conf, ask=_ask(onto, "low_confidence", name)))
    if status == "proposed":
        gaps.append(_gap("draft", ask=_ask(onto, "draft", name)))
    link_items = _draft_link_items(onto, node_id)
    if link_items:
        links = sorted(text for _eid, text in link_items)
        note = "; ".join(links[:DRAFT_LINKS_SHOWN]) + (" (+%d more)" % (len(links) - DRAFT_LINKS_SHOWN)
                                                      if len(links) > DRAFT_LINKS_SHOWN else "")
        gap = _gap("draft_link", note=note, ask=_ask(onto, "draft_link", name, note=note))
        gap["key"] = draft_link_key(eid for eid, _text in link_items)
        gaps.append(gap)
    now = util.now()
    replaced = _superseded(onto)
    for src in distinct:
        entry = onto.sources.get(src)
        if entry and src not in replaced and sources_mod.stale(entry, now):
            tools = refresh_tool_items(onto, src)
            tool = ""
            if tools:  # the tool keeps its markers (C.7)
                first = tools[0]
                tool = " with %s%s%s" % ("[untrusted] " if first.get("untrusted") else "", first["id"],
                                         " (draft)" if first.get("draft") else "")
            gaps.append(_gap("stale_source", note=src, ask=_ask(onto, "stale_source", name, other=src, tool=tool)))
    for g in node.get("gaps") if isinstance(node.get("gaps"), list) else []:
        if isinstance(g, dict):
            note = str(g.get("note") or "")
            gap = _gap("open_question", field=g.get("field"), note=note,
                       ask=_ask(onto, "open_question", name, note="%s: %s" % (g.get("field"), note)))
            if not _recorded_gap_met(onto, node_id, kind, str(g.get("field") or ""), attrs, edges) \
                    and gap_answer(onto, node_id, gap) is None:
                gaps.append(gap)
    if (kind == "question" and status != "archived" and not onto.edges_of(node_id, "in", rels=["answers"])
            and onto.own_local(node_id) not in decided_ids(onto)):
        gap = _gap("open_question", note="unanswered", ask=_fill("{name}", name=name))
        if question_held(onto, node_id, node):
            gap["held"] = True  # still open, so still listed; the interview does not ask it back yet
        gaps.append(gap)
    for e in edges:
        edge = e["edge"]
        if edge.get("rel") == "contradicts":
            gaps.append(_gap("contradiction", note=e["other"],
                             ask=_ask(onto, "contradiction", name, other=onto.label(e["other"]) or e["other"])))
    gaps.extend(_premise_gaps(onto, node_id, name))
    for c in _conflicts(onto, node_id):
        if conflict_settled(onto, node_id, c["field"], c["values"]):  # decided for it: the decision is shown
            continue
        gap = _gap("conflict", field=c["field"], note=c["note"],
                   ask=_ask(onto, "conflict", name, field=c["field"], note=c["note"]))
        gap["decide_scope"] = decide_scope(node_id, c["field"])
        answered = _conflict_answer(onto, node_id, c["field"])
        if answered is not None:  # the user said which value holds, but no decision records it yet
            gap["answered"] = answered.get("src")
            # the trace goes first, so a cut ask still shows it: brief and context print the start of the ask
            gap["ask"] = "Answered in %s but not recorded: record which value holds as a decision with scope %s. %s" % (
                answered.get("src") or answered.get("id"), gap["decide_scope"], gap["ask"])
        gaps.append(gap)
    for e in onto.edges_of(node_id, include_archived=True):
        edge = e["edge"]
        if edge.get("status") == "archived" or _quiet(edge) or not onto.is_bridge(edge):
            continue
        other = onto.node(e["other"])
        if other is None or other.get("status") == "archived":
            why = "absent upstream" if other is None else "archived upstream"
            ask = _ask(onto, "dangling_bridge", name, other=e["other"])
            block = (other or {}).get("archived")
            now_ids = [str(x) for x in (block.get("superseded_by") or [])
                       if isinstance(x, str)] if isinstance(block, dict) else []
            if now_ids:  # an upstream merge: name the replacement (qualified at load), so the bridge can be re-pointed
                why += ", now %s" % ", ".join(now_ids)
                ask = "%s Upstream replaced it with %s." % (ask, ", ".join(now_ids))
            gaps.append(_gap("dangling_bridge", rel=edge.get("rel"), note="%s %s" % (e["other"], why), ask=ask))
    gaps = [g for g in gaps if g["type"] not in ANSWER_SETTLES or gap_answer(onto, node_id, g) is None]
    gaps.sort(key=lambda g: (-g["severity"], g["type"], str(g.get("field") or g.get("rel") or ""),
                             str(g.get("note") or "")))
    return {
        "id": node_id,
        "gaps": gaps,
        "completeness": (float(filled) / len(expected)) if expected else 1.0,
        "expected": len(expected),
        "filled": filled,
        "expects_met": met,
        "expects_required": required,
        "prov": len(prov),
        "sources": len(distinct),
        "quotes": quotes,
        "degree": degree,
        "confirmed": status == "confirmed",
        "conf": float(conf) if isinstance(conf, (int, float)) and not isinstance(conf, bool) else 0.0,
        "substantive": _is_summary(node.get("summary")) and bool(prov) and degree >= 1,
    }


def archived_premises(onto: Any, node_id: str) -> List[str]:
    """The archived premises ``node_id`` still rests on: a ``rests_on`` link to a premise that is archived, where the
    link is active or was archived with the premise (``_archived_with``), and no active ``rests_on`` link of the
    node reaches a premise that replaced it (``archived.superseded_by``). A link archived on its own was dealt with."""
    out: List[str] = []
    edges = onto.edges_of(node_id, "out", rels=[RESTS_ON], include_archived=True)
    live = {e["other"] for e in edges if e["edge"].get("status") != "archived" and onto.active(e["other"])}
    for e in edges:
        other = e["other"]
        premise = onto.node(other)
        if premise is None or premise.get("status") != "archived" or other in out:
            continue
        if onto.kind_of(other).split("/")[-1] != PREMISE_KIND:
            continue
        block = premise.get("archived") if isinstance(premise.get("archived"), dict) else {}
        edge = e["edge"]
        if edge.get("status") == "archived" and not _archived_with(edge, other, block):
            continue
        replaced = {onto.own_local(str(x)) for x in block.get("superseded_by") or [] if isinstance(x, str)}
        if replaced & live:
            continue
        out.append(other)
    return sorted(out)


def _archived_with(edge: Dict[str, Any], node_id: str, block: Dict[str, Any]) -> bool:
    """True when an archived edge was archived together with ``node_id`` (``mutate`` gives it the node's block with
    the reason ``archived with <id>: ...``)."""
    mine = edge.get("archived") if isinstance(edge.get("archived"), dict) else {}
    return (all(mine.get(k) == block.get(k) for k in ("on", "decision", "superseded_by"))
            and str(mine.get("reason") or "").startswith("archived with %s:" % node_id))


def _premise_gaps(onto: Any, node_id: str, name: str) -> List[Dict[str, Any]]:
    """An ``archived_premise`` gap per archived premise the node still rests on (``archived_premises``)."""
    out = []
    for premise in archived_premises(onto, node_id):
        out.append(_gap("archived_premise", rel=RESTS_ON, note=premise,
                        ask=_ask(onto, "archived_premise", name,
                                 other=_plain_name(str((onto.node(premise) or {}).get("name") or premise)))))
    return out


def _recorded_gap_met(onto: Any, node_id: str, kind: str, field: str, attrs: Dict[str, Any],
                      edges: List[Dict[str, Any]]) -> bool:
    """True when what a recorded gap (``add_gap``) names is there now: its field is filled (``owner`` or
    ``attrs.owner``), or it names a relation (``owns``, ``owns.in``) the node's class has an active link of."""
    if not field:
        return False
    short = field[6:] if field.startswith("attrs.") else field
    if _filled(attrs.get(short)):
        return True
    rel, _sep, want = field.partition(".")
    if field.startswith("attrs.") or onto.registry.relation(rel) is None:
        return False
    return any(e["edge"].get("rel") == rel and (want not in ("in", "out") or e["direction"] == want
                                                 or onto.symmetric(e["edge"])) for e in edges)


def all_needs(onto: Any) -> Dict[str, Dict[str, Any]]:
    """``needs`` for every active local node, cached on the graph object."""
    cached = onto._cache.get("needs")
    if cached is None:
        cached = {nid: needs(onto, nid) for nid in onto.local_nodes(active_only=True)}
        onto._cache["needs"] = cached
    return cached


def _superseded(onto: Any) -> Set[str]:
    cached = onto._cache.get("superseded_sources")
    if cached is None:
        cached = sources_mod.superseded(onto.sources)
        onto._cache["superseded_sources"] = cached
    return cached


def cited_sources(onto: Any) -> List[str]:
    """Sources cited by at least one active local record."""
    out = []
    for src in sorted(onto.sources):
        if any(onto.active(rid) and onto.ns_of(rid) == "self" for rid, _loc in onto.prov_index.get(src, [])):
            out.append(src)
    return out


def evidence_sources(onto: Any) -> List[str]:
    """Cited sources that are evidence: not interview answers (nor the ``Topic title`` source ``onto init`` writes,
    which is one), since the interview transcript is how the topic was told, not data about it."""
    return [s for s in cited_sources(onto) if (onto.sources.get(s) or {}).get("kind") != "interview"]


def dimension_coverage(onto: Any, closed: Iterable[str] = (), count_imports: bool = False) -> Dict[str, float]:
    """``{dimension: min(1, substantive nodes of its kinds / target)}`` for every declared dimension not in
    ``closed`` (closed dimensions are left out). Imported nodes count only with ``count_imports`` (the interview
    uses it, richness does not). The ``data`` dimension also counts evidence sources (``evidence_sources``: cited at
    least once, interview answers left out)."""
    closed_set = set(closed)
    registry = onto.registry
    counts: Dict[str, int] = {}
    ids = onto.local_nodes(active_only=True)
    if count_imports:
        ids = ids + sorted(n for n in onto.nodes if onto.ns_of(n) != "self" and onto.active(n))
    for nid in ids:
        dim = registry.dimension_of(onto.kind_of(nid))
        if dim and substantive(onto, nid):
            counts[dim] = counts.get(dim, 0) + 1
    counts["data"] = counts.get("data", 0) + len(evidence_sources(onto))
    out: Dict[str, float] = {}
    for dim, decl in sorted(registry.dimensions().items()):
        if dim in closed_set:
            continue
        target = decl.get("target") or 0
        out[dim] = 1.0 if not target else min(1.0, float(counts.get(dim, 0)) / float(target))
    return out


def gap_action(gap_type: str) -> Optional[str]:
    info = GAP_TYPES.get(gap_type)
    return info["action"] if info else None
