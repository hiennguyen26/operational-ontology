"""The interview: stages, the next questions and recorded answers (C.10, C.11).

Questions come from the merged question banks (``core``, ``discovery`` and ``packs/local.questions.jsonl``) and from
the gaps of the nodes (``needs``). A *gap question* asks what closes one gap of one local node; its text is the bank
template carrying ``for_gap`` (or the kind's ``expects[].ask``), filled in by ``needs``. Its id names the node after
``@``, for example ``q.gap.orphan@term:mulch`` or ``q.gap.missing_relation.owns.in@dataset:harvest-log``: pass that
id to ``answer`` as ``next`` prints it. The ``conflict`` and ``dangling_bridge`` gaps are also asked about the
imported endpoints of the local bridges (``q.gap.conflict.format@garden/dataset:harvest-log``), once per ``same_as``
class and conflicting field; nothing else is asked about an imported node, which is read-only here.

Progress is derived from ``interview/log.jsonl`` plus the graph and never stored. The latest log line of a question
(and node) wins, except that a ``skipped`` or ``later`` line never replaces an ``answered`` one (it adds only its
recency and its skip):

- ``answered``: done (a ``repeatable`` question may come back; it ranks 300 lower for 2 days);
- ``skipped``: it may come back, 30 lower per skip, and 300 lower for 2 days;
- ``later``: it may come back, 300 lower for 2 days;
- ``na``: the question's dimension is closed: left out of coverage and of later questions. For a gap question only
  that question on that node is closed;
- ``skip_stage`` (bank questions only): the question's stage counts as done and its bank questions are no longer
  asked. ``answer`` refuses it on a gap question, whose stage moves as stages finish, and names the bank question
  that skips the stage the gap question sits in.

``answer`` also refuses ``skipped`` and ``later`` on a question that is answered and not repeatable (it is never
asked again; ``na`` still closes it).

A question whose last log line is ``skipped`` or ``later`` is *put off* for 2 days: ``next`` (and the status Next
line, which reads ``next_questions``) leaves it out while any other question is open and names it only under
``held_back``, so a question the user just declined never comes back in the next turn. With nothing else open it
is listed again, flagged ``put_off: {status, until}`` in JSON and ``put off (...) until <date>`` in the text.

Stages: 0 (frame) is done when every quick-start question is handled: answered, skipped or n/a, or never asked any
more because its dimension was closed or its stage skipped through another question, or its ``until`` holds; 1 to 7
when every dimension of the stage has coverage of at least ``policy.stage_done_at`` or is closed, or a ``skip_stage``
answer exists; 8 (compose, only with imports) when ``q.compose.meet`` is answered and every import pair has a local
bridge (a bundled import counts as the direct import that brought it, as richness counts it); 9 (deepen) is never
done. Coverage here counts imported nodes, so questions a parent topic already answered are not asked again.

Sessions: log lines more than 3 hours apart start a new session (``last_session`` reads them the same way), and a
session is over once its last line is more than 3 hours old. ``q.deepen.change`` (what has changed since we last
spoke) is asked only when the log holds an earlier session than the current one, so never in the first session.

The score of a question:

    priority + 200 x (its stage is the earliest unfinished stage) + 100 x (1 - coverage of its dimension)
    + 20 x severity x log2(2 + degree of the node) x (2 when the node is within 2 hops of a goal)
    - 30 x skips - 300 x (asked or skipped within 2 days)

A question without a dimension counts as uncovered. A gap question belongs to its node's stage (8 for ``conflict``
and ``dangling_bridge`` with imports) while that stage is open and to stage 9 after it. Ties break by id. While the
quick start is open, its unanswered questions come first (``pinned``). ``q.vocab.kinds`` (the main kinds of things,
which become the topic's local kinds) comes right after them, before every other question, until it is answered,
skipped or n/a (``after_quick``; a ``later`` holds it back for 2 days, as it does a quick question); after an
answer that ends the quick start, ``best_next`` names it before any follow-up. It is not a quick question: the
quick start stays five questions and stage 0 is done without it. Put-off questions come last. The penalties only order
questions: a gap question can outscore every bank question by far even after them, so the put-off hold (not the
score) is what keeps a question the user just declined out of the next turn.

Every ``next`` item carries ``why``, ``options`` and ``follow_ups`` (empty lists when the question has none) and a
``progress`` object (``stage``, ``stage_name``, ``quick_answered``, ``quick_total``, ``answered``, ``skipped``), so an
agent can show the progress line each turn. ``about=<node id>`` lists that node's gap questions first, then the open
follow-ups of the questions whose answers recorded it (its ``Q:<question id>`` provenance); the rest keep their
order. After an answer the result names the best question to ask now (``next``, with a ``reason``): an open
follow-up of the answered question, else a gap question on a node the answer added, else the top-ranked question.

``answer`` stores the answer text as an ``interview`` source (sanitized and hashed like any source), appends the log
line and saves the ops as a pending proposal (``pipeline.prepare``). In the ops, a provenance entry citing the answer
may leave out ``src`` (or write ``$answer``): the kit fills in the answer's source id, ``loc`` ``Q:<question id>``
and ``by: user``; an ``add_node``, ``add_edge`` or update op without provenance gets that entry (without a quote).
An op whose provenance cites an ingested source and no interview source is refused: it carries that source's
content, which goes in through ``onto propose`` so a draft keeps its untrusted mark.
With ``apply`` the proposal is reviewed by the user and committed in the same call: an op with ``basis: "stated"``
whose quotes from the answer are all found in the stored answer is accepted (confirmed, trust ``user``); every other
op is drafted, and a quote from the answer that is not found is dropped so the op goes in as a draft. Ops that
cannot be drafted (merges, archives, pack changes, new questions, updates of confirmed records) are accepted, but
only once confirmed: ``cmd_answer`` answers an ``apply`` holding any of them with a preview and writes nothing until
``confirm=true`` (``confirm_ops``). The preview runs the proposal checks without saving (``dry_run``), so it never
promises an apply that the confirmed call refuses: with any problem ``cmd_answer`` returns the refusal instead. A
refused answer (at the preview or at the real call) is a result with ``error: "refused"``, exit code 1 and every
problem by op number, as ``onto propose`` returns a refused proposal; nothing is written.
An ``answered`` conflict question (``q.gap.conflict.<field>@<node>``) also records a decision in the same call: the
answer text is its ``chosen_text`` and its scope is the member plus the field (``needs.decide_scope``), so the conflict
is settled by a recorded choice and never by the log line alone (``result["decision"]``). A failed decision is a
note; the conflict then stays marked answered for the user to record.
A status other than ``answered`` appends only a log line. Calling ``answer`` again
with the same question and text (and the same ops) as its latest answer writes nothing and returns the recorded
result, so a retried call never adds the same facts or the same log line twice.
"""

from __future__ import annotations

import copy
import math
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import ids, ledger, mutate, needs, pipeline, records, render, sources, store, util
from .commands import Context, optional
from .errors import OntoError, Refused, UsageError
from .graph import Ontology

LOG = "interview/log.jsonl"
STATUSES = ("answered", "skipped", "na", "later", "skip_stage")
HANDLED = ("answered", "skipped", "na", "skip_stage")  # quick questions that no longer hold the quick start open
WAITS = ("skipped", "later")  # statuses that put a question off; they never replace an answer
MAX_N = 20  # the most questions one onto_next call lists (the COMMANDS maximum of n)
STAGES = {0: "frame", 1: "people", 2: "data", 3: "vocabulary", 4: "process", 5: "constraints", 6: "deliverables",
          7: "questions", 8: "compose", 9: "deepen"}
QUICK = ("q.frame.you", "q.frame.goal", "q.frame.deliverable", "q.people.key", "q.data.where")
KINDS = "q.vocab.kinds"  # the topic's own kinds of things: asked right after the quick start (``after_quick``)
COMPOSE = 8
DEEPEN = 9
MEET = "q.compose.meet"
CATCH_ALL = "q.deepen.more"
CHANGE = "q.deepen.change"  # what changed since the last session: asked only once an earlier session exists
GAP_PREFIX = "q.gap."
QID_RE = re.compile(r"^q\.[a-z0-9][a-z0-9._-]{0,79}\Z")
KEYED_GAPS = ("archived_premise", "draft_link")  # their question ids carry the premise or the set of links
DETAIL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}\Z")

# the score (WP4)
STAGE_BONUS = 200
COVERAGE_WEIGHT = 100
GAP_WEIGHT = 20
GOAL_FACTOR = 2
GOAL_HOPS = 2
SKIP_PENALTY = 30
RECENT_PENALTY = 300
RECENT_DAYS = 2
GAP_PRIORITY = 30  # a gap question without a bank template
SESSION_GAP_HOURS = 3  # log lines further apart start a new session

# node gaps that a question can close (the others are ingest or review actions: see ``onto gaps``)
# the why line of a gap question whose type has no bank template: words for the user, never a gap type or an id
GAP_WHY = "Records like this one usually have it; until they do, the gap stays open."
ASKED_GAPS = ("contradiction", "conflict", "unconfirmed_hub", "missing_field", "missing_relation", "orphan", "thin",
              "dangling_bridge", "low_confidence", "open_question", "archived_premise", "draft_link")
COMPOSE_GAPS = ("conflict", "dangling_bridge")  # also asked about imported nodes joined by a local bridge
PACK_CHANGES = ("add_kind", "add_relation", "add_field", "map_kinds", "add_question")
UNDRAFTABLE = ("merge", "archive") + PACK_CHANGES
AUTO_PROV = ("add_node", "add_edge", "update_node", "update_edge")
ANSWER_REFS = ("$answer", "answer")


# the log ---------------------------------------------------------------------------------------------------------
def handle(q: str, node: Optional[str] = None) -> str:
    """The id a question is asked and answered under: ``q``, or ``q@node`` for a gap question."""
    return "%s@%s" % (q, node) if node else q


def split_handle(text: Any) -> Tuple[str, Optional[str]]:
    """``(question id, node or None)`` of an id as ``next`` prints it."""
    raw = str(text or "").strip()
    qid, sep, node = raw.partition("@")
    return qid, (node if sep else None)


def read_log(repo: Optional[store.Repo]) -> List[Dict[str, Any]]:
    """The readable lines of ``interview/log.jsonl`` in file order (bad lines are left to ``validate``)."""
    if repo is None:
        return []
    rows, _problems = store.read_jsonl(repo.path(LOG))
    return _valid_rows(rows)


def _valid_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [r for r in rows if isinstance(r.get("q"), str) and r.get("status") in STATUSES]


def _parse_at(row: Dict[str, Any]) -> Optional[datetime]:
    try:
        return util.parse_ts(str(row.get("at")))
    except ValueError:
        return None


class _Log(object):
    """The log folded per (question, node): the latest line, the skip count and the last time asked. A ``skipped``
    or ``later`` line never replaces an ``answered`` one: the answer stands, and the line adds only its recency (and
    its skip)."""

    def __init__(self, rows: Sequence[Dict[str, Any]]) -> None:
        self.rows = list(rows)
        self.latest: Dict[Tuple[str, Optional[str]], Dict[str, Any]] = {}
        self.skips: Dict[Tuple[str, Optional[str]], int] = {}
        self.last_at: Dict[Tuple[str, Optional[str]], datetime] = {}
        self.last_status: Dict[Tuple[str, Optional[str]], str] = {}  # the status of the line at ``last_at``
        for row in self.rows:
            key = (str(row["q"]), row.get("node") or None)
            prev = self.latest.get(key)
            if not (row.get("status") in WAITS and prev is not None and prev.get("status") == "answered"):
                self.latest[key] = row
            if row.get("status") == "skipped":
                self.skips[key] = self.skips.get(key, 0) + 1
            at = _parse_at(row)
            if at is not None and (key not in self.last_at or at >= self.last_at[key]):
                self.last_at[key] = at
                self.last_status[key] = str(row.get("status"))
        self._answered = {k[0] for k, row in self.latest.items() if row.get("status") == "answered"}

    def status(self, qid: str, node: Optional[str] = None) -> Optional[str]:
        row = self.latest.get((qid, node))
        return row.get("status") if row else None

    def answered(self, qid: str) -> bool:
        """True when the latest status of ``qid`` (on any node) is ``answered``."""
        return qid in self._answered


# the derived state -----------------------------------------------------------------------------------------------
class _State(object):
    """Everything ``progress`` and ``next_questions`` derive from the graph and the log, computed once per call."""

    def __init__(self, onto: Ontology) -> None:
        self.onto = onto
        self.registry = onto.registry
        self.repo = onto.repo
        policy = dict(onto.repo.policy) if onto.repo is not None else {}
        self.threshold = float(policy.get("stage_done_at") if policy.get("stage_done_at") is not None else 0.6)
        self.log = _Log(read_log(onto.repo))
        self.now = util.now()
        self.bank: Dict[str, Dict[str, Any]] = {q["id"]: q for q in self.registry.questions()}
        self.has_imports = bool(onto.imports)
        self.closed = self._closed_dimensions()
        self.coverage = needs.dimension_coverage(onto, closed=self.closed, count_imports=True)
        self.skipped_stages = self._skipped_stages()
        self._counts: Dict[Tuple[str, Optional[str]], int] = {}
        self._done: Dict[int, bool] = {}
        self._computing: Set[int] = set()
        self._near: Optional[Set[str]] = None
        self._templates: Optional[Dict[str, Dict[str, Any]]] = None
        self._earlier: Optional[bool] = None

    def earlier_session(self) -> bool:
        """True when the log holds a session before the current one. Walking back from now, a pause of more than
        ``SESSION_GAP_HOURS`` before a line (or between the last line and now) ends an earlier session; with no such
        pause every line belongs to the current session, which is then the first."""
        if self._earlier is None:
            gap = timedelta(hours=SESSION_GAP_HOURS)
            times = sorted((at for at in (_parse_at(r) for r in self.log.rows) if at is not None), reverse=True)
            found, later = False, self.now
            for at in times:
                if later - at > gap:
                    found = True
                    break
                later = at
            self._earlier = found
        return self._earlier

    # questions and stages ----------------------------------------------------------------------------------------
    def bank_stage(self, question: Dict[str, Any]) -> int:
        if isinstance(question.get("stage"), int):
            return int(question["stage"])
        dim = question.get("dimension")
        stage = self.registry.stage_of(dim) if dim else None
        return int(stage) if stage is not None else DEEPEN

    def node_stage(self, node_id: str) -> Tuple[Optional[str], int]:
        """``(dimension, stage)`` of a node's kind (stage 9 when the kind has no dimension)."""
        dim = self.registry.dimension_of(self.onto.kind_of(node_id))
        stage = self.registry.stage_of(dim) if dim else None
        return dim, (int(stage) if stage is not None else DEEPEN)

    def gap_stage(self, gap_type: str, node_id: str) -> Tuple[Optional[str], int]:
        """``(dimension, stage)`` a gap question sits in: its node kind's stage (8 for ``conflict`` and
        ``dangling_bridge`` when the topic has imports) while that stage is open, and 9 (deepen) once it is done."""
        dim, stage = self.node_stage(node_id)
        if gap_type in COMPOSE_GAPS and self.has_imports:
            stage = COMPOSE
        if stage != DEEPEN and self.stage_done(stage):
            stage = DEEPEN  # the stage is done: its gaps belong to the open-ended deepening
        return dim, stage

    def stage_question(self, n: int) -> Optional[str]:
        """The bank question that stands for stage ``n`` (highest priority, then id): the one to answer with
        ``skip_stage`` to skip that stage."""
        found = [q for q in self.bank.values() if not q.get("for_gap") and self.bank_stage(q) == n]
        found.sort(key=lambda q: (-int(q.get("priority") or 0), q["id"]))
        return found[0]["id"] if found else None

    def templates(self) -> Dict[str, Dict[str, Any]]:
        """``{gap type: bank template}``: the lowest id carrying ``for_gap`` (the rule ``needs`` uses)."""
        if self._templates is None:
            found: Dict[str, Dict[str, Any]] = {}
            for qid in sorted(self.bank):
                gap = self.bank[qid].get("for_gap")
                if gap and gap not in found:
                    found[gap] = self.bank[qid]
            self._templates = found
        return self._templates

    def _closed_dimensions(self) -> List[str]:
        out: Set[str] = set()
        for (qid, node), row in self.log.latest.items():
            if node or row.get("status") != "na":
                continue
            dim = (self.bank.get(qid) or {}).get("dimension")
            if dim:
                out.add(str(dim))
        return sorted(out)

    def _skipped_stages(self) -> List[int]:
        """Stages closed by a ``skip_stage`` answer to a bank question. A ``skip_stage`` line on a gap question
        (which ``answer`` refuses) closes only that question on that node: the stage a gap question sits in moves as
        stages finish, so it cannot name a stage."""
        out: Set[int] = set()
        for (qid, node), row in self.log.latest.items():
            if row.get("status") != "skip_stage" or node or qid not in self.bank:
                continue
            out.add(self.bank_stage(self.bank[qid]))
        out.discard(DEEPEN)
        return sorted(out)

    def quick(self) -> List[Dict[str, Any]]:
        return [q for qid, q in sorted(self.bank.items()) if q.get("quick")]

    def quick_handled(self, question: Dict[str, Any]) -> bool:
        """True when the quick question no longer holds the quick start open: its latest status is answered,
        skipped, na or skip_stage; its dimension was closed (n/a) or its stage skipped through another question (so
        it is never asked); or its ``until`` holds."""
        if self.log.status(question["id"]) in HANDLED:
            return True
        dim = question.get("dimension")
        if dim and dim in self.closed:
            return True
        if self.bank_stage(question) in self.skipped_stages:
            return True
        until = question.get("until") or []
        return bool(until) and self.all_hold(until)

    def stage_dimensions(self, n: int) -> List[str]:
        return sorted(d for d, decl in self.registry.dimensions().items() if decl.get("stage") == n)

    def groups(self) -> Dict[str, str]:
        """``{import ns: the direct import that brought it}`` (a direct import maps to itself), as richness reads
        the bridge pairs."""
        entries = {e["ns"]: e for e in self.onto.imports if isinstance(e.get("ns"), str)}
        out: Dict[str, str] = {}
        for ns in entries:
            cur, seen = ns, set()
            while True:
                via = entries.get(cur, {}).get("via")
                if not via or via not in entries or cur in seen:
                    break
                seen.add(cur)
                cur = via
            out[ns] = cur
        return out

    def pairs(self) -> List[Tuple[str, str]]:
        """Import pairs a bridge must join: every pair of direct imports, or ``self`` with the one direct import."""
        direct = sorted(set(self.groups().values()))
        if len(direct) >= 2:
            return [(a, b) for i, a in enumerate(direct) for b in direct[i + 1:]]
        return [tuple(sorted(("self", ns))) for ns in direct]  # type: ignore[misc]

    def local_bridges(self) -> List[str]:
        """Active, non-background local edges between namespaces (C.16: a bridge is a local edge)."""
        onto = self.onto
        return [eid for eid in onto.local_edge_ids
                if eid in onto.bridges and onto.active(eid) and not (onto.edges.get(eid) or {}).get("background")]

    def bridged(self) -> Set[Tuple[str, str]]:
        """The import pairs joined by at least one local bridge (a bundled import counts as its direct import)."""
        groups = self.groups()
        out: Set[Tuple[str, str]] = set()
        for eid in self.local_bridges():
            edge = self.onto.edges[eid]
            ends = []
            for end in (str(edge.get("src")), str(edge.get("dst"))):
                ns = self.onto.ns_of(end)
                ends.append("self" if ns == "self" else groups.get(ns, ns))
            if ends[0] != ends[1]:
                out.add(tuple(sorted(ends)))  # type: ignore[arg-type]
        return out

    def stage_coverage(self, n: int) -> float:
        if n == 0:
            quick = self.quick()
            return 1.0 if not quick else sum(1 for q in quick if self.quick_handled(q)) / float(len(quick))
        if n == COMPOSE:
            pairs = self.pairs()
            if not pairs:
                return 1.0
            bridged = self.bridged()
            return sum(1 for p in pairs if p in bridged) / float(len(pairs))
        if n == DEEPEN:
            values = list(self.coverage.values())
            return sum(values) / len(values) if values else 0.0
        dims = [d for d in self.stage_dimensions(n) if d not in self.closed]
        return min(self.coverage.get(d, 0.0) for d in dims) if dims else 1.0

    def stage_done(self, n: int) -> bool:
        if n in self._done:
            return self._done[n]
        if n in self._computing:  # a predicate that asks about the stage being computed reads it as open
            return False
        self._computing.add(n)
        try:
            if n == DEEPEN:
                done = False
            elif n in self.skipped_stages:
                done = True
            elif n == 0:
                done = all(self.quick_handled(q) for q in self.quick())
            elif n == COMPOSE:
                if not self.has_imports:
                    done = True
                else:
                    bridged = self.bridged()
                    done = self.log.answered(MEET) and all(p in bridged for p in self.pairs())
            else:
                done = all(d in self.closed or self.coverage.get(d, 0.0) >= self.threshold
                           for d in self.stage_dimensions(n))
        finally:
            self._computing.discard(n)
        self._done[n] = done
        return done

    def earliest(self) -> int:
        """The earliest unfinished stage (8 counts only with imports); 9 when every other stage is done."""
        for n in range(0, DEEPEN):
            if n == COMPOSE and not self.has_imports:
                continue
            if not self.stage_done(n):
                return n
        return DEEPEN

    # predicates --------------------------------------------------------------------------------------------------
    def _kind_matches(self, key: str, want: str, qualified: bool) -> bool:
        if qualified:
            return key == want
        return key == want or key.split("/")[-1] == want.split("/")[-1]

    def count(self, kind: str, ns: Optional[str]) -> int:
        cache_key = (kind, ns)
        if cache_key not in self._counts:
            want = self.registry.kind_key(kind) or kind
            qualified = "/" in kind
            n = 0
            for nid, node in self.onto.nodes.items():
                if nid in self.onto.virtual or node.get("status") == "archived":
                    continue
                if ns and self.onto.ns_of(nid) != ns:
                    continue
                if self._kind_matches(self.onto.kind_of(nid), want, qualified):
                    n += 1
            self._counts[cache_key] = n
        return self._counts[cache_key]

    def missing_field(self, kind: str, field: str) -> bool:
        want = self.registry.kind_key(kind) or kind
        for nid in self.onto.local_nodes(active_only=True):
            if not self._kind_matches(self.onto.kind_of(nid), want, "/" in kind):
                continue
            value = ((self.onto.node(nid) or {}).get("attrs") or {}).get(field)
            if value is None or (isinstance(value, (str, list, dict)) and not value):
                return True
        return False

    def _of_kind(self, kind: str) -> List[str]:
        want = self.registry.kind_key(kind) or kind
        return [nid for nid in self.onto.local_nodes(active_only=True)
                if self._kind_matches(self.onto.kind_of(nid), want, "/" in kind)]

    def all_filled(self, kind: str, field: str) -> bool:
        """At least one active node of ``kind``, and every one of them has ``attrs.<field>``."""
        found = self._of_kind(kind)
        return bool(found) and not self.missing_field(kind, field)

    def all_linked(self, kind: str, rel: str, direction: str) -> bool:
        """At least one active node of ``kind``, and every one of them has an active ``rel`` edge in
        ``direction`` (``out`` or ``in``)."""
        found = self._of_kind(kind)
        return bool(found) and all(
            any(e["edge"].get("rel") == rel for e in self.onto.edges_of(nid, direction=direction, rels=[rel]))
            for nid in found)

    def holds(self, pred: Any) -> bool:
        """One predicate (C.10). An unknown predicate never holds."""
        if not isinstance(pred, dict):
            return False
        if "count" in pred:
            spec = pred["count"] or {}
            n = self.count(str(spec.get("kind") or ""), spec.get("ns"))
            if "lt" in spec and not n < int(spec["lt"]):
                return False
            if "gte" in spec and not n >= int(spec["gte"]):
                return False
            return "lt" in spec or "gte" in spec
        if "missing_field" in pred:
            spec = pred["missing_field"] or {}
            return self.missing_field(str(spec.get("kind") or ""), str(spec.get("field") or ""))
        if "all_filled" in pred:
            spec = pred["all_filled"] or {}
            return self.all_filled(str(spec.get("kind") or ""), str(spec.get("field") or ""))
        if "all_linked" in pred:
            spec = pred["all_linked"] or {}
            return self.all_linked(str(spec.get("kind") or ""), str(spec.get("rel") or ""),
                                   "in" if spec.get("dir") == "in" else "out")
        if "answered" in pred:
            return self.log.answered(str(pred["answered"]))
        if "not_answered" in pred:
            return not self.log.answered(str(pred["not_answered"]))
        if "stage_done" in pred:
            return self.stage_done(int(pred["stage_done"]))
        if "has_imports" in pred:
            return self.has_imports == bool(pred["has_imports"])
        return False

    def all_hold(self, preds: Optional[Iterable[Any]]) -> bool:
        return all(self.holds(p) for p in preds or [])

    # scoring -----------------------------------------------------------------------------------------------------
    def near_goal(self) -> Set[str]:
        """Active nodes within 2 hops of an active goal (over active links that are not background)."""
        if self._near is None:
            onto = self.onto
            goals = [nid for nid, node in onto.nodes.items()
                     if nid not in onto.virtual and node.get("status") != "archived"
                     and onto.kind_of(nid).split("/")[-1] == "goal"]
            seen = set(goals)
            frontier = sorted(goals)
            for _hop in range(GOAL_HOPS):
                reached: List[str] = []
                for nid in frontier:
                    for e in onto.edges_of(nid):
                        if e["edge"].get("background"):
                            continue
                        other = e["other"]
                        if other not in seen:
                            seen.add(other)
                            reached.append(other)
                frontier = sorted(reached)
            self._near = seen
        return self._near

    def recent(self, key: Tuple[str, Optional[str]]) -> bool:
        at = self.log.last_at.get(key)
        return at is not None and self.now - at < timedelta(days=RECENT_DAYS)

    def put_off(self, key: Tuple[str, Optional[str]]) -> Optional[Dict[str, str]]:
        """``{status, until}`` when the last line of the question is ``skipped`` or ``later`` and less than
        ``RECENT_DAYS`` old (a repeatable question answered and then skipped counts too), else None."""
        status = self.log.last_status.get(key)
        if status not in WAITS or not self.recent(key):
            return None
        return {"status": str(status), "until": util.fmt_ts(self.log.last_at[key] + timedelta(days=RECENT_DAYS))}

    def score(self, cand: Dict[str, Any], earliest: int) -> float:
        value = float(cand["priority"])
        if cand["stage"] == earliest:
            value += STAGE_BONUS
        dim = cand.get("dimension")
        covered = self.coverage.get(dim, 0.0) if dim else 0.0
        value += COVERAGE_WEIGHT * (1.0 - covered)
        node = cand.get("node")
        if node:
            factor = GOAL_FACTOR if node in self.near_goal() else 1
            value += GAP_WEIGHT * cand["severity"] * math.log(2 + self.onto.degree(node), 2) * factor
        key = (cand["q"], node)
        value -= SKIP_PENALTY * self.log.skips.get(key, 0)
        if self.recent(key):
            value -= RECENT_PENALTY
        return round(value, 2)


def _fill(template: Any, topic: str) -> str:
    return util.normalize_ws(str(template or "").replace("{topic}", topic))


# progress ----------------------------------------------------------------------------------------------------------
def _last_session(rows: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The last run of log lines less than 3 hours apart: when it started and ended and what it recorded."""
    if not rows:
        return None
    session = [rows[-1]]
    later = _parse_at(rows[-1])
    for row in reversed(rows[:-1]):
        at = _parse_at(row)
        if at is None or later is None or later - at > timedelta(hours=SESSION_GAP_HOURS):
            break
        session.insert(0, row)
        later = at
    counts: Dict[str, int] = {}
    for row in session:
        counts[str(row["status"])] = counts.get(str(row["status"]), 0) + 1
    return {
        "started": session[0].get("at"),
        "ended": session[-1].get("at"),
        "lines": len(session),
        "by_status": dict(sorted(counts.items())),
        "last": [handle(str(r["q"]), r.get("node")) for r in session[-5:]],
    }


def _progress(state: _State) -> Dict[str, Any]:
    has_imports = state.has_imports
    stages = []
    for n in range(0, DEEPEN + 1):
        if n == COMPOSE and not has_imports:
            continue
        stages.append({"n": n, "name": STAGES[n], "done": state.stage_done(n),
                       "coverage": round(state.stage_coverage(n), 3)})
    latest = state.log.latest
    answered = sum(1 for row in latest.values() if row.get("status") == "answered")
    skipped = sum(1 for row in latest.values() if row.get("status") == "skipped")
    quick = state.quick()
    out: Dict[str, Any] = {
        "stage": state.earliest(),
        "stages": stages,
        "answered": answered,
        "skipped": skipped,
        "last_session": _last_session(state.log.rows),
        "quick": {"total": len(quick), "handled": sum(1 for q in quick if state.quick_handled(q)),
                  "open": [q["id"] for q in quick if not state.quick_handled(q)]},
        "closed_dimensions": list(state.closed),
        "skipped_stages": list(state.skipped_stages),
        "coverage": {k: round(v, 3) for k, v in sorted(state.coverage.items())},
    }
    if has_imports:
        bridged = state.bridged()
        out["import_pairs"] = [{"pair": "%s|%s" % p, "bridged": p in bridged} for p in state.pairs()]
    return out


def progress(onto: Ontology) -> Dict[str, Any]:
    """``{stage, stages: [{n, name, done, coverage}], answered, skipped, last_session}`` plus the quick start, the
    closed dimensions, the skipped stages and the coverage the interview reads (imports counted)."""
    return _progress(_State(onto))


# next questions ----------------------------------------------------------------------------------------------------
def _bank_candidates(state: _State, stage: Optional[int]) -> List[Dict[str, Any]]:
    topic = state.onto.title()
    out = []
    for qid in sorted(state.bank):
        q = state.bank[qid]
        if q.get("for_gap"):
            continue  # a template, asked about one node at a time
        q_stage = state.bank_stage(q)
        if q_stage == COMPOSE and not state.has_imports:
            continue
        if stage is not None and q_stage != stage:
            continue
        dim = q.get("dimension")
        if dim and dim in state.closed:
            continue
        if q_stage in state.skipped_stages:
            continue
        status = state.log.status(qid)
        if status in ("na", "skip_stage") or (status == "answered" and not q.get("repeatable")):
            continue
        if qid == CHANGE and not state.earlier_session():
            continue  # nothing has changed "since we last spoke" while the first session is still going
        if not state.all_hold(q.get("when")):
            continue
        until = q.get("until") or []
        if until and state.all_hold(until):
            continue
        cand: Dict[str, Any] = {
            "id": qid, "q": qid, "ask": _fill(q.get("ask"), topic), "why": _fill(q.get("why"), topic),
            "fills": copy.deepcopy(q.get("fills") or {}), "stage": q_stage, "dimension": dim,
            "priority": int(q.get("priority") or 0), "quick": bool(q.get("quick")),
            "repeatable": bool(q.get("repeatable")), "node": None, "severity": 0,
        }
        if q.get("options"):
            cand["options"] = copy.deepcopy(q["options"])
        if q.get("follow_ups"):
            cand["follow_ups"] = list(q["follow_ups"])
        out.append(cand)
    return out


def _detail(gap: Dict[str, Any]) -> str:
    """The id suffix that tells two gaps of one kind on one node apart (``needs._detail``, the one rule)."""
    return needs._detail(gap)


def gap_question_id(state_or_onto: Any, gap: Dict[str, Any]) -> str:
    """The question id of a node gap: its bank template's id (or ``q.gap.<type>``) plus the field or relation."""
    state = state_or_onto if isinstance(state_or_onto, _State) else _State(state_or_onto)
    template = state.templates().get(str(gap.get("type")))
    base = template["id"] if template else GAP_PREFIX + str(gap.get("type"))
    qid = base + _detail(gap)
    return qid if QID_RE.match(qid) else base


def _gap_targets(state: _State) -> List[Tuple[str, List[Dict[str, Any]]]]:
    """``[(node, gaps)]``: every active local node with its ``needs`` gaps, then the active imported endpoints of the
    local bridges with only their ``conflict`` gaps and the ``dangling_bridge`` gaps of those local bridges (the
    imported side is read-only, so only what the bridges made is asked)."""
    onto = state.onto
    table = needs.all_needs(onto)
    out = [(nid, list(table[nid].get("gaps") or [])) for nid in sorted(table)]
    out += [(nid, list(need["gaps"])) for nid, need in needs.bridge_end_gaps(onto)]
    return out


def _gap_candidates(state: _State, stage: Optional[int]) -> List[Dict[str, Any]]:
    onto = state.onto
    topic = onto.title()
    templates = state.templates()
    out = []
    classes: Set[Tuple[str, str]] = set()  # a same_as class is asked about one conflict once
    for nid, gaps in _gap_targets(state):
        dim = state.node_stage(nid)[0]
        if dim and dim in state.closed:
            continue
        node = onto.node(nid) or {}
        seen: Set[str] = set()
        for gap in gaps:
            gap_type = str(gap.get("type"))
            if gap_type not in ASKED_GAPS or gap.get("held"):  # a question raised just now is not asked back
                continue
            qid = gap_question_id(state, gap)
            if qid in seen:
                continue
            seen.add(qid)
            if gap_type == "conflict":
                class_key = (str(onto.same_as.get(nid, nid)), qid)
                if class_key in classes:
                    continue
                classes.add(class_key)
            q_stage = state.gap_stage(gap_type, nid)[1]
            if stage is not None and q_stage != stage:
                continue
            if q_stage in state.skipped_stages:
                continue
            if state.log.status(qid, nid) in ("answered", "na", "skip_stage"):
                continue
            template = templates.get(gap_type) or {}
            why = template.get("why") or GAP_WHY
            fills: Dict[str, Any] = {"kinds": [onto.kind_of(nid)]}
            if gap.get("field"):
                fills["fields"] = [str(gap["field"]).split(".")[-1]]
            cand: Dict[str, Any] = {
                "id": handle(qid, nid), "q": qid, "ask": util.normalize_ws(str(gap.get("ask") or node.get("name"))),
                "why": _fill(why, topic), "fills": fills, "stage": q_stage, "dimension": dim,
                "priority": int(template.get("priority") if template.get("priority") is not None else GAP_PRIORITY),
                "quick": False, "repeatable": False, "node": nid, "severity": int(gap.get("severity") or 0),
                "gap": {k: gap[k] for k in ("type", "severity", "field", "rel", "dir", "note") if k in gap},
            }
            cand.update(render.flags(node))  # C.7: the item that names the node carries its markers
            out.append(cand)
    return out


def _ranked(state: _State, stage: Optional[int] = None) -> List[Dict[str, Any]]:
    earliest = state.earliest()
    quick_open = not state.stage_done(0)
    best: Dict[str, Dict[str, Any]] = {}
    for cand in _bank_candidates(state, stage) + _gap_candidates(state, stage):
        cand["score"] = state.score(cand, earliest)
        key = (cand["q"], cand.get("node"))
        cand["pinned"] = bool(quick_open and cand["quick"] and not state.quick_handled(state.bank[cand["q"]])
                              and not state.recent(key))
        # the main kinds come right after the open quick start, before every other question, until handled
        cand["after_quick"] = bool(cand["q"] == KINDS and not cand.get("node")
                                   and not state.quick_handled(state.bank[KINDS]) and not state.recent(key))
        cand["put_off"] = state.put_off(key)
        found = best.get(cand["id"])
        if found is None or cand["score"] > found["score"]:
            best[cand["id"]] = cand
    return sorted(best.values(), key=lambda c: (not c["pinned"], not c["after_quick"], bool(c["put_off"]),
                                                -c["score"], c["id"]))


def _split(ranked: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """``(listed, held back)``: a question put off in the last ``RECENT_DAYS`` is held back while any other question
    is open; with nothing else open the put-off questions are listed (each carries its ``put_off`` flag)."""
    fresh = [c for c in ranked if not c["put_off"]]
    held = [c for c in ranked if c["put_off"]]
    return (fresh, held) if fresh else (held, [])


def item_progress(prog: Dict[str, Any]) -> Dict[str, Any]:
    """The progress line every ``next`` item carries: ``{stage, stage_name, quick_answered, quick_total, answered,
    skipped}`` (``quick_answered`` counts the quick questions handled: answered, skipped or n/a)."""
    quick = prog.get("quick") or {}
    stage = prog.get("stage")
    return {"stage": stage, "stage_name": STAGES.get(stage, "") if isinstance(stage, int) else "",
            "quick_answered": int(quick.get("handled") or 0), "quick_total": int(quick.get("total") or 0),
            "answered": int(prog.get("answered") or 0), "skipped": int(prog.get("skipped") or 0)}


def _public(cand: Dict[str, Any], progress: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A ranked candidate as ``next`` lists it. ``why``, ``options`` and ``follow_ups`` are always there (empty
    when the question has none), and so is ``progress`` when given."""
    out = {k: cand[k] for k in ("id", "q", "ask", "fills", "score", "stage", "dimension") if k in cand}
    out["why"] = str(cand.get("why") or "")
    out["options"] = copy.deepcopy(cand.get("options") or [])
    out["follow_ups"] = list(cand.get("follow_ups") or [])
    for key in ("node", "gap"):
        if cand.get(key):
            out[key] = cand[key]
    for key in ("untrusted", "draft", "archived", "quick", "repeatable", "pinned", "after_quick", "about"):
        if cand.get(key):
            out[key] = True
    if cand.get("put_off"):
        out["put_off"] = dict(cand["put_off"])
    if progress is not None:
        out["progress"] = dict(progress)
    return out


def origin_questions(onto: Ontology, node_id: str) -> List[str]:
    """The questions whose answers recorded ``node_id``: the ``Q:<question id>`` locations of its provenance."""
    node = onto.node(node_id) or {}
    out: List[str] = []
    for p in node.get("prov") if isinstance(node.get("prov"), list) else []:
        loc = str(p.get("loc") or "") if isinstance(p, dict) else ""
        qid = loc[2:].split("@", 1)[0] if loc.startswith("Q:") else ""
        if qid and qid not in out:
            out.append(qid)
    return out


def _about_first(state: _State, listed: List[Dict[str, Any]], node_id: str) -> List[Dict[str, Any]]:
    """``listed`` with the questions about ``node_id`` first: its gap questions, then the open follow-ups of the
    questions whose answers recorded it (each in rank order, marked ``about``); the rest keep their order."""
    follow: Set[str] = set()
    for qid in origin_questions(state.onto, node_id):
        follow.update(str(f) for f in (state.bank.get(qid) or {}).get("follow_ups") or [])
    gaps = [c for c in listed if c.get("node") == node_id]
    ups = [c for c in listed if not c.get("node") and c["q"] in follow]
    first = gaps + ups
    taken = {c["id"] for c in first}
    for c in first:
        c["about"] = True
    return first + [c for c in listed if c["id"] not in taken]


def _about_node(onto: Ontology, about: Any) -> Optional[str]:
    """The node ``about`` names (loose ids resolve), or None when not given; ``NotFound`` when unknown."""
    text = str(about or "").strip()
    if not text:
        return None
    found = onto.require(text, accept=lambda i: i in onto.nodes and i not in onto.virtual)
    if found not in onto.nodes or found in onto.virtual:  # an exact edge or source id resolves before the filter
        raise UsageError("about takes a node id; %s is not a node (an edge or a source); give one of its nodes"
                         % found, about=found)
    return found


def next_questions(onto: Ontology, n: int = 3, stage: Optional[int] = None,
                   include_put_off: bool = False, about: Optional[str] = None) -> List[Dict[str, Any]]:
    """The ``n`` best questions to ask now: ``[{id, ask, why, options, follow_ups, fills, node?, score, stage,
    progress}]`` (see the module docstring for the ranking). ``stage`` keeps only the questions of one stage.
    ``about`` (a node id) lists that node's gap questions and the follow-ups of the questions that recorded it
    first. A question put off in the last 2 days is left out while any other question is open
    (``include_put_off`` lists it last instead); an item that is put off carries ``put_off: {status, until}``."""
    if stage is not None and (not isinstance(stage, int) or isinstance(stage, bool) or not 0 <= stage <= DEEPEN):
        raise UsageError("stage must be a number from 0 to 9, got %r" % (stage,))
    node_id = _about_node(onto, about)
    state = _State(onto)
    ranked = _ranked(state, stage)
    listed = ranked if include_put_off else _split(ranked)[0]
    if node_id:
        listed = _about_first(state, listed, node_id)
    count = max(0, int(n)) if n is not None else 3
    progress = item_progress(_progress(state))
    return [_public(c, progress) for c in listed[:count]]


def best_next(onto: Ontology, answered: Optional[str] = None, added: Sequence[str] = (),
              exclude: Sequence[str] = ()) -> Optional[Dict[str, Any]]:
    """The best question to ask after an answer, or None when nothing is open: the main kinds (``q.vocab.kinds``)
    once the quick start is done and while it is still open, then an open follow-up of the ``answered`` question,
    then a gap question on a node the answer just ``added``, else the top-ranked question. ``exclude`` lists item
    ids never picked (the question just answered). Carries ``reason``."""
    state = _State(onto)
    listed = [c for c in _split(_ranked(state))[0] if c["id"] not in set(exclude)]
    if not listed:
        return None
    follow = [str(f) for f in ((state.bank.get(answered) or {}).get("follow_ups") or [])] if answered else []
    pick, reason = None, "the top-ranked question"
    if listed[0].get("after_quick") and state.stage_done(0):
        pick, reason = listed[0], "the main kinds of things, asked right after the quick start"
    if pick is None:
        for c in listed:
            if not c.get("node") and c["q"] in follow:
                pick, reason = c, "a follow-up of %s" % answered
                break
    if pick is None and added:
        new = set(added)
        for c in listed:
            if c.get("node") in new:
                pick, reason = c, "a gap on %s, which this answer added" % c["node"]
                break
    if pick is None:
        pick = listed[0]
    out = _public(pick, item_progress(_progress(state)))
    out["reason"] = reason
    return out


# answering -------------------------------------------------------------------------------------------------------
def _gap_type_of(qid: str, bank: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """The gap type a gap question id asks about (bank template ids first, then ``q.gap.<type>``), else None."""
    best: Optional[Tuple[int, str]] = None
    for bid, question in bank.items():
        gap = question.get("for_gap")
        if gap and (qid == bid or qid.startswith(bid + ".")) and (best is None or len(bid) > best[0]):
            best = (len(bid), str(gap))
    if best is not None:
        return best[1]
    if qid.startswith(GAP_PREFIX):
        rest = qid[len(GAP_PREFIX):]
        for gap_type in sorted(needs.GAP_TYPES, key=len, reverse=True):
            if rest == gap_type or rest.startswith(gap_type + "."):
                return gap_type
    return None


def resolve_question(onto: Ontology, q: Any) -> Dict[str, Any]:
    """What ``q`` names: ``{id, q, node, question, gap, resolved}``. A bank question takes no node; a gap question
    needs its node after ``@``: a local node, or for ``conflict`` and ``dangling_bridge`` also an imported one (a
    loose node id resolves and is reported in ``resolved``)."""
    qid, node_ref = split_handle(q)
    if not QID_RE.match(qid):
        raise UsageError("not a question id: %r (ids look like q.data.where; onto next prints them)" % (q,))
    bank = {x["id"]: x for x in onto.registry.questions()}
    question = bank.get(qid)
    gap_type = None
    if question is None or question.get("for_gap"):
        gap_type = _gap_type_of(qid, bank)
        if gap_type is None:
            raise UsageError("unknown question %s; answer an id that onto next printed, or %s for anything else"
                             % (qid, CATCH_ALL), question=qid)
    if gap_type is None:
        if node_ref is not None:
            raise UsageError("%s is not asked about a node; drop the @ part" % qid)
        return {"id": qid, "q": qid, "node": None, "question": question, "gap": None, "resolved": None}
    if not node_ref:
        raise UsageError("%s is asked about one node: give %s@<node id> as onto next prints it" % (qid, qid))
    resolved = None
    ref = node_ref[5:] if node_ref.startswith("self/") else node_ref
    accept = onto.is_local
    scope: Optional[str] = "self"
    if gap_type in COMPOSE_GAPS:  # asked about imported nodes too (the endpoints of local bridges)
        def accept(i: str) -> bool:
            return i in onto.nodes and i not in onto.virtual

        scope = None
    if accept(ref):
        node = ref
    else:
        node = onto.require(node_ref, accept=accept, ns=scope)
        res = onto.resolve(node_ref, accept=accept, ns=scope)
        resolved = {"query": node_ref, "id": node, "also": [c for c in res.get("candidates") or [] if c != node]}
    if gap_type in KEYED_GAPS and qid in (GAP_PREFIX + gap_type, needs._gap_templates(onto).get(gap_type)):
        # asked once per premise or per set of draft links: the bare id names the one open question, if one
        open_ids = sorted({needs.gap_question_id(onto, g) for g in needs.needs(onto, node)["gaps"]
                           if g.get("type") == gap_type})
        if len(open_ids) > 1:
            raise UsageError("%s@%s is asked once per %s; answer one of %s as onto next prints it" % (
                qid, node, "premise" if gap_type == "archived_premise" else "set of drafted links",
                ", ".join(handle(i, node) for i in open_ids)), question=qid)
        if open_ids:
            qid = open_ids[0]
    return {"id": handle(qid, node), "q": qid, "node": node, "question": question, "gap": gap_type,
            "resolved": resolved}


def _ops_list(ops: Any) -> List[Dict[str, Any]]:
    if ops is None:
        return []
    if not isinstance(ops, list) or not all(isinstance(op, dict) for op in ops):
        raise UsageError("ops must be a list of op objects (see onto-ingest references/extraction.md)")
    return copy.deepcopy(ops)


def _fill_prov(ops: List[Dict[str, Any]], src: str, qid: str) -> List[Dict[str, Any]]:
    """Point the answer's provenance at its source: ``src`` missing or ``$answer`` becomes ``src``, with ``loc``
    ``Q:<qid>`` and ``by: user`` when missing; an op that records facts but carries no provenance gets that entry."""
    loc = "Q:" + qid
    for op in ops:
        prov = op.get("prov")
        if isinstance(prov, list):
            for p in prov:
                if isinstance(p, dict) and (not p.get("src") or p.get("src") in ANSWER_REFS):
                    p["src"] = src
                    p.setdefault("loc", loc)
                    p.setdefault("by", "user")
        if op.get("op") in AUTO_PROV and not prov:
            op["prov"] = [{"src": src, "loc": loc, "by": "user"}]
    return ops


def _answer_quotes(op: Dict[str, Any], src: str) -> List[Dict[str, Any]]:
    return [p for p in op.get("prov") or [] if isinstance(p, dict) and p.get("src") == src and p.get("quote")]


def _stated(op: Dict[str, Any], src: str, text: str) -> bool:
    """``basis: stated`` with at least one quote from the answer, every one of them found in the answer text."""
    if op.get("basis") != "stated":
        return False
    quotes = _answer_quotes(op, src)
    return bool(quotes) and all(sources.quote_found(text, str(p["quote"]), str(p.get("loc") or "")) for p in quotes)


def _drop_missing_quotes(ops: List[Dict[str, Any]], src: str, text: str) -> List[int]:
    """Remove the answer quotes that are not in the answer text; returns the op numbers touched. A touched op is no
    longer ``stated`` (its basis becomes ``inferred``), so it goes in as a draft and never earns trust ``user``."""
    touched = []
    for n, op in enumerate(ops, start=1):
        for p in _answer_quotes(op, src):
            if not sources.quote_found(text, str(p["quote"]), str(p.get("loc") or "")):
                p.pop("quote", None)
                if n not in touched:
                    touched.append(n)
        if n in touched and op.get("basis") == "stated":
            op["basis"] = "inferred"
    return touched


def _target_confirmed(onto: Ontology, ref: Any) -> bool:
    if not isinstance(ref, str) or ref.startswith("$"):
        return False
    rec = onto.record(ref[5:] if ref.startswith("self/") else ref) or {}
    return rec.get("status") == "confirmed"


def destructive_ops(onto: Ontology, ops: Optional[Sequence[Dict[str, Any]]]) -> List[int]:
    """Op numbers (1-based) that merge, archive, or update a confirmed record: the ones ``pipeline.destructive``
    names, found before anything is stored."""
    out = []
    for n, op in enumerate(ops or [], start=1):
        kind = op.get("op") if isinstance(op, dict) else None
        if kind in ("merge", "archive"):
            out.append(n)
        elif kind in ("update_node", "update_edge") and _target_confirmed(onto, op.get("id")):
            out.append(n)
    return out


def confirm_ops(onto: Ontology, ops: Optional[Sequence[Dict[str, Any]]]) -> List[int]:
    """Op numbers an ``apply`` answer holds until ``confirm=true``: the destructive ones, plus pack changes and new
    questions. None of these can be drafted, so ``apply`` would accept them with no stated basis and no quote (E.3
    accepts at once only stated, quoted facts); the confirm is the user's accept. An update that ``verdicts_for``
    drafts (an inferred one, or one that sets an ``invoke`` line no quote holds) changes nothing until review, so
    it is not held."""
    held = set(destructive_ops(onto, ops))
    return [n for n, op in enumerate(ops or [], start=1)
            if (n in held and not (isinstance(op, dict) and _drafted_update(op)))
            or (isinstance(op, dict) and op.get("op") in PACK_CHANGES)]


def _invoke_set(op: Dict[str, Any]) -> Optional[str]:
    """The ``invoke`` line an op writes (``node.attrs.invoke`` of an add, ``attrs.invoke`` of an update's set), or
    None."""
    if op.get("op") == "add_node":
        attrs = (op.get("node") or {}).get("attrs") if isinstance(op.get("node"), dict) else None
        value = attrs.get("invoke") if isinstance(attrs, dict) else None
    elif op.get("op") == "update_node" and isinstance(op.get("set"), dict):
        value = op["set"].get("attrs.invoke")
    else:
        value = None
    return value if isinstance(value, str) and value.strip() else None


def _unquoted_invoke(op: Dict[str, Any]) -> bool:
    """True when the op writes an ``invoke`` line that none of its quotes holds: a command the user did not say goes
    in as a draft (never run until confirmed), however the op is marked."""
    line = _invoke_set(op)
    if line is None:
        return False
    want = util.normalize_ws(line)
    quotes = [util.normalize_ws(str(p.get("quote") or "")) for p in op.get("prov") or [] if isinstance(p, dict)]
    return not any(want in q for q in quotes)


def _drafted_update(op: Dict[str, Any]) -> bool:
    """An update of a confirmed record that goes in as a draft: inferred, or setting an unquoted ``invoke`` line."""
    return op.get("op") in ("update_node", "update_edge") and (op.get("basis") == "inferred" or _unquoted_invoke(op))


def foreign_ops(onto: Ontology, ops: Sequence[Dict[str, Any]], src: str) -> List[Tuple[int, List[str]]]:
    """``[(op number, cited sources)]`` for ops whose provenance cites an ingested (non-interview) source and no
    interview source. Such an op carries another source's content, not the answer's: it belongs in a proposal of
    that source (``onto_propose``), where a draft is marked untrusted."""
    out = []
    for n, op in enumerate(ops, start=1):
        cited = sorted({str(p.get("src")) for p in op.get("prov") or [] if isinstance(p, dict) and p.get("src")})
        answered = [s for s in cited if s == src or (onto.sources.get(s) or {}).get("kind") == "interview"]
        other = [s for s in cited if s.startswith("src-") and s not in answered]
        if other and not answered:
            out.append((n, other))
    return out


def verdicts_for(onto: Ontology, ops: Sequence[Dict[str, Any]], src: str, text: str) -> Dict[str, str]:
    """The automatic verdicts of ``apply``: stated and quoted facts are accepted, ops that cannot be drafted are
    accepted (``cmd_answer`` holds those until ``confirm=true``, see ``confirm_ops``), everything else is drafted.
    An update of a confirmed record is accepted on the confirm unless it is ``inferred``; an op that writes an
    ``invoke`` line its quotes do not hold is always drafted."""
    out: Dict[str, str] = {}
    for n, op in enumerate(ops, start=1):
        kind = op.get("op")
        if kind in UNDRAFTABLE:
            verdict = "accept"
        elif _unquoted_invoke(op):
            verdict = "draft"  # a command the user did not say stays a draft tool, which never runs unconfirmed
        elif kind in ("update_node", "update_edge") and _target_confirmed(onto, op.get("id")):
            # the confirm is the user's accept of a stated change; a guess stays a draft for review
            verdict = "draft" if op.get("basis") == "inferred" else "accept"
        elif _stated(op, src, text):
            verdict = "accept"
        else:
            verdict = "draft"
        out[str(n)] = verdict
    return out


def _draft(key: str, src: str, ops: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The proposal draft an answer's ops go in as."""
    return {"source": src, "summary": "answer to %s: %d op%s" % (key, len(ops), "" if len(ops) == 1 else "s"),
            "ops": ops}


def _unstored_problems(ops: Sequence[Dict[str, Any]], src: str, text: str) -> List[Dict[str, Any]]:
    """The provenance problems of the ops that cite the answer, checked against the text it would store (the checks
    of ``pipeline.prepare`` cannot read a source that is not stored yet): a location the answer cannot hold
    (``P11``) and a quote that is not in it (``quote``), worded as those checks word them."""
    entry = {"kind": "interview", "lines": text.count("\n")}
    out: List[Dict[str, Any]] = []
    for n, op in enumerate(ops, start=1):
        for p in op.get("prov") or []:
            if not isinstance(p, dict) or p.get("src") != src:
                continue
            loc = str(p.get("loc") or "")
            where = sources.loc_problem(src, loc, entry, entry["lines"], source_text=text)
            if where:
                out.append({"n": n, "code": "P11", "message": "provenance %s: %s" % (src, where)})
            elif p.get("quote") is not None and not sources.quote_found(text, str(p["quote"]), loc):
                out.append({"n": n, "code": "quote", "message": "quote not found in %s %s: %r"
                            % (src, loc, util.normalize_ws(str(p["quote"]))[:80])})
    return out


def about_ops(exc: Refused) -> bool:
    """True when a refusal lists problems of single ops (the proposal checks, ops citing another source), which a
    refused answer lists one by one. A refusal of the whole call keeps its own message: credentials or data the
    policy refuses (it carries ``kinds``), damaged files (``Problem`` records)."""
    return (bool(exc.problems) and "kinds" not in exc.extra
            and all(isinstance(p, dict) and p.get("code") for p in exc.problems))


def dry_run(repo: store.Repo, onto: Ontology, key: str, ops: Sequence[Dict[str, Any]], src: str, text: str,
            apply: bool) -> List[Dict[str, Any]]:
    """Every problem the proposal checks would refuse the answer's ops with, found without writing anything: the
    ops go through ``pipeline.prepare`` with ``save=False`` as the real call sends them (with ``apply``, answer
    quotes that are not in the text dropped first). The answer is not stored yet, so the checks' complaint that its
    source is unknown is left out and its locations and quotes are checked here (``_unstored_problems``). A preview
    that lists no problem is one the confirmed call applies; a refusal that is not about the ops (damaged files, say)
    is raised as the real call would raise it. With ``apply``, ops the checks pass go on through the final graph
    check of the commit (``pipeline.trial_problems``, with the verdicts ``apply`` gives), so a change the commit
    would refuse (a P23: a reviewed or approved risk rated past its controls) shows in the preview, before the user
    is asked to confirm it."""
    trial = copy.deepcopy(list(ops))
    if not trial:
        return []
    if apply:
        _drop_missing_quotes(trial, src, text)
    stored = src in onto.sources
    problems = [] if stored else _unstored_problems(trial, src, text)
    prepared: Optional[Dict[str, Any]] = None
    try:
        prepared = pipeline.prepare(repo, _draft(key, src, trial), by="agent", save=False)
        found: List[Dict[str, Any]] = []
    except Refused as exc:
        if not about_ops(exc):
            raise  # a refusal of the whole call (credentials, damaged files): the real call raises it too
        found = list(exc.problems)
        prepared = exc.extra.get("proposal") if isinstance(exc.extra.get("proposal"), dict) else None
    unknown = ("source %s is not in the sources index" % src, "provenance %s is not in the sources index" % src)
    for p in found:
        if not stored and p.get("code") == "ref" and p.get("message") in unknown:
            continue  # the confirmed call stores the answer first
        problems.append(p)
    if apply and prepared is not None and not problems:
        verdicts = verdicts_for(onto, prepared.get("ops") or [], src, text)
        problems += pipeline.trial_problems(repo, prepared, verdicts, by="user", change_type="answer",
                                            unstored=None if stored else src)
    # op order, as the checks report them (the answer's own provenance first within an op, the draft's last)
    return sorted(problems, key=lambda p: p["n"] if isinstance(p.get("n"), int) else float("inf"))


def _clean_text(repo: store.Repo, text: str) -> Tuple[str, str]:
    """``(stored text, source id)`` the answer would get, without storing it (``Refused`` on credentials)."""
    body, _redactions = sources.default_sanitizer()(sources.normalize_text(text), repo.policy)
    body = sources.normalize_text(body)
    return body, "src-" + util.sha256_text(body)[:12]


def _answer_id(key: str, text: str, at: str, taken: Set[str]) -> str:
    """``ans-YYYYMMDD-<6 hex>`` of sha256(q + text + at), extended while taken; a repeat at the same second hashes a
    counter too, so every log line keeps its own id."""
    body = key + text + at
    for n in range(1, 1000):
        try:
            return ids.record_id("ans", body if n == 1 else "%s\x1f%d" % (body, n), date=at[:10], taken=taken)
        except OntoError:
            continue
    raise UsageError("cannot make a unique answer id for %s" % key)


def _append_log(repo: store.Repo, key: str, qid: str, node: Optional[str], status: str, src: Optional[str],
                proposal: Optional[str], text: str, at: str) -> Dict[str, Any]:
    path = repo.path(LOG)
    rows, _problems = store.read_jsonl(path)
    taken = {str(r.get("id")) for r in rows if r.get("id")}
    row = {"id": _answer_id(key, text, at, taken), "q": qid, "at": at, "status": status, "src": src,
           "proposal": proposal, "node": node}
    errors = records.check(row, "answer")
    if errors:
        raise UsageError("the log line fails its schema: %s" % "; ".join(errors[:3]), problems=errors)
    store.append_jsonl(path, row)
    return row


def _refuse_calibration(repo: store.Repo, exc: Refused, log_before: Optional[bytes], prop_id: str,
                        pending_before: Set[str], src: str, discard: bool, index_before: Optional[bytes]) -> None:
    """The calibration check refused an answer's change (a P23: a risk would be reviewed or approved past its
    controls). Unlike other commit refusals, whose proposal waits for review and an edit, the answer is not kept:
    the question stays open and no pending proposal waits on a choice the user has not made yet. Raises
    ``Refused`` with the problems as dicts, so the answer result lists them."""
    _undo_answer(repo, log_before, prop_id, pending_before, src, discard, index_before)
    problems = [p if isinstance(p, dict) else {"code": getattr(p, "code", "refused"),
                                               "message": p.text() if hasattr(p, "text") else str(p)}
                for p in (exc.problems or [])] or [{"code": "refused", "message": exc.message}]
    raise Refused("answer refused: %s" % exc.message, problems=problems)


def _calibration_only(exc: Refused) -> bool:
    """True when every problem of a commit refusal is a P23 of the assessment pack's calibration check."""
    codes = [getattr(p, "code", None) if not isinstance(p, dict) else p.get("code") for p in exc.problems or []]
    return bool(codes) and all(code == "P23" for code in codes)


def _new_calibration_warnings(before: Ontology, repo: store.Repo) -> List[str]:
    """The calibration warnings (W09, draft risks) a change added, as messages; empty when the assessment pack is
    off. A failure to compute them never fails the answer."""
    from . import assessment

    def messages(onto: Ontology) -> Set[str]:
        return {p.message for p in assessment.calibration(onto, lambda rid: ("", 0))[1]}

    try:
        old = messages(before)
        return sorted(messages(Ontology.load(repo)) - old)
    except Exception:
        return []


def _undo_answer(repo: store.Repo, log_before: Optional[bytes], prop_id: str, pending_before: Set[str], src: str,
                 discard: bool, index_before: Optional[bytes]) -> None:
    """Undo an answer whose ops the commit refused: the log line, the proposal this call saved (one that was open
    before is kept) and, with ``discard``, the source this call stored (a text stored before stays)."""
    if log_before is None:
        if os.path.exists(repo.path(LOG)):
            os.unlink(repo.path(LOG))
    else:
        store.write_bytes(repo.path(LOG), log_before)
    if prop_id not in pending_before:
        path = repo.path("%s/%s.json" % (pipeline.PENDING, prop_id))
        if os.path.exists(path):
            os.unlink(path)
    if discard:
        _discard_source(repo, src, index_before)
    store.clear_cache()


def _discard_source(repo: store.Repo, src: str, index_before: Optional[bytes]) -> None:
    """Undo a source stored by this call (the index bytes as they were, the text file removed)."""
    index_path = repo.path(sources.INDEX)
    if index_before is None:
        if os.path.exists(index_path):
            os.unlink(index_path)
    else:
        store.write_bytes(index_path, index_before)
    text_path = sources.text_path(repo, src)
    if os.path.exists(text_path):
        os.unlink(text_path)
    store.clear_cache()


def _store_answer(repo: store.Repo, body: str, title: str) -> Tuple[Dict[str, Any], bool]:
    """``sources.add`` of the answer as one all-or-nothing write (``ingest.plan_sources``, then
    ``ingest.write_planned``): a process killed half way leaves a write intent the next writer rolls back, never a
    text file without its index line. Call it under the write lock."""
    from . import ingest

    added, planned = ingest.plan_sources(repo, [dict(text=body, kind="interview", title=title)])
    if planned:
        ingest.write_planned(repo, planned, (), lambda: None)
        store.clear_cache()
    return added[0]


def _read_bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def _richness(onto: Ontology) -> Optional[Any]:
    try:
        rich = optional("richness")
        if rich is None or not hasattr(rich, "summary"):
            return None
        return (rich.summary(onto) or {}).get("score")
    except Exception:  # a measure must never fail an answer that was already written
        return None


LOCAL_RECORD = "a local record"  # names a node kept out of exports (a person) in a title that may be exported


def _title(question: Dict[str, Any], qid: str, node: Optional[str], ans_id: str, hidden: bool = False) -> str:
    """The stored answer's source title. A source title goes out with every export that cites it, so a node kept
    out of exports (``visibility: local``, such as a person) is never named in it: ``hidden`` writes
    ``a local record`` in its place."""
    title = "Answer to %s" % qid
    if node:
        title += " on %s" % (LOCAL_RECORD if hidden else node)
    if question and question.get("repeatable"):
        title += " (%s)" % ans_id  # each answer adds to the others; none replaces an earlier one
    return title


def _repeat(repo: store.Repo, latest: Optional[Dict[str, Any]], src: str, ops: Sequence[Dict[str, Any]],
            apply: bool) -> Optional[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]]:
    """``(log line, proposal or None)`` when this call repeats ``latest``, the latest answer to the question: the
    same text (source) and, with ops, the same ops and a proposal that is applied (or, without ``apply``, still
    open); without ops, a line that has no proposal either. A retried call then writes nothing, so it never adds the
    same facts or the same log line twice."""
    if not latest or latest.get("status") != "answered" or latest.get("src") != src:
        return None
    if not ops:
        return (latest, None) if not latest.get("proposal") else None
    if not latest.get("proposal"):
        return None
    prop_id = str(latest["proposal"])
    # the stored proposal's id hashes its ops after prepare cleaned them (the sanitizer's redactions, invisible
    # characters out of names): clean a copy of these ops the same way, so a retried call is still recognised
    cleaned = {"ops": copy.deepcopy(list(ops))}
    try:
        pipeline.sanitize_draft(repo, cleaned, "draft")
        pipeline.strip_invisible_names(cleaned, "draft")
    except OntoError:
        return None
    author = {"by": "agent", "source": src, "ops": [pipeline._normalized(o) for o in cleaned["ops"]]}
    try:
        base = ids.record_id("prop", author, date=prop_id[5:13])
        prop = pipeline.load(repo, prop_id)
    except OntoError:
        return None
    if not prop_id.startswith(base) or prop.get("source") != src:
        return None
    if prop.get("status") == "applied" or (not apply and prop.get("status") in pipeline.OPEN):
        return latest, prop
    return None


def _answer_prefix(key: str, proposal: Dict[str, Any], verdicts: Any) -> str:
    """The start of an applied answer's change summary: the question and the names of the nodes it adds (at most
    3), so ``onto log`` says which question was answered with what. ``onto erase`` scrubs names from summaries."""
    names = []
    for op in proposal.get("ops") or []:
        verdict = (verdicts or {}).get(str(op.get("n"))) if isinstance(verdicts, dict) else None
        if op.get("op") == "add_node" and verdict != "reject":
            name = render.plain((op.get("node") or {}).get("name"), 60)
            if name and name not in names:
                names.append(name)
    added = ", added %s%s" % (", ".join(names[:3]), " +%d more" % (len(names) - 3) if len(names) > 3 else "") \
        if names else ""
    return "%s%s: " % (key, added)


def _proposal_view(proposal: Dict[str, Any]) -> Dict[str, Any]:
    ops = proposal.get("ops") or []
    matches = []
    for op in ops:
        for m in (op.get("annot") or {}).get("matches") or []:
            if m.get("score", 0) >= 0.5:
                matches.append({"n": op.get("n"), "id": m.get("id"), "score": m.get("score"), "why": m.get("why")})
    checks = proposal.get("checks") or {}
    return {"id": proposal.get("id"), "status": proposal.get("status"), "priority": proposal.get("priority"),
            "ops": len(ops), "warnings": checks.get("warnings") or [],
            "new_terms": list(proposal.get("new_terms") or []), "matches": matches, "note": proposal.get("note")}


def _added(proposal: Dict[str, Any], applied: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``[{n, id, name, status}]``: the nodes an applied proposal created (its ``add_node`` ops that were not
    rejected), in op order."""
    results = applied.get("results") or {}
    out = []
    for op in proposal.get("ops") or []:
        if op.get("op") != "add_node":
            continue
        res = results.get(str(op.get("n"))) or {}
        if res.get("id") and not res.get("skipped"):
            out.append({"n": op.get("n"), "id": res["id"], "name": (op.get("node") or {}).get("name"),
                        "status": res.get("status")})
    return out


def _settle(result: Dict[str, Any], proposal: Dict[str, Any], applied: Dict[str, Any]) -> None:
    """After an apply, name the nodes it created in ``applied.added`` and keep in the proposal's ``new_terms`` only
    the terms that are still not in the ontology (an op rejected, say): the created ones are in it now."""
    added = _added(proposal, applied)
    result["applied"]["added"] = added
    view = result.get("proposal")
    if view is not None:
        names = {util.name_key(str(a.get("name") or "")) for a in added}
        view["new_terms"] = [t for t in view.get("new_terms") or [] if util.name_key(str(t)) not in names]


def _check_status(onto: Ontology, target: Dict[str, Any], status: str) -> None:
    """Refuse a status that the log could not read as meant: ``skip_stage`` on a gap question (the stage it sits in
    moves as stages finish; skip a stage through one of its bank questions), and ``skipped`` or ``later`` on a
    question that is answered and not repeatable (it is never asked again; ``na`` still closes it)."""
    qid, node = target["q"], target["node"]
    if status == "skip_stage" and target["gap"]:
        state = _State(onto)
        stage = state.gap_stage(str(target["gap"]), str(node))[1]
        stand_in = state.stage_question(stage) if stage != DEEPEN else None
        if stand_in is None:
            raise UsageError("%s sits in stage %d (%s), which is never skipped; use status na to stop asking it "
                             "about %s" % (target["id"], stage, STAGES[stage], node))
        raise UsageError("skip_stage skips a whole stage: answer %s with status skip_stage to skip stage %d (%s), "
                         "or use status na to stop asking %s about %s only"
                         % (stand_in, stage, STAGES[stage], qid, node))
    if status in WAITS:
        question = target["question"] or {}
        if question.get("repeatable") and not target["gap"]:
            return
        row = _Log(read_log(onto.repo)).latest.get((qid, node))
        if row is not None and row.get("status") == "answered":
            raise UsageError("%s is already answered (%s) and is not asked again; give a new answer, or use status "
                             "na to close it" % (target["id"], row.get("id")))


DECISION_TEXT_MAX = 600  # a decision's chosen_text (records.schema.json)


def _conflict_gap(onto: Ontology, target: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The open ``conflict`` gap the question names on its node (``needs``), or None."""
    if target.get("gap") != "conflict" or not target.get("node"):
        return None
    try:
        gaps = needs.needs(onto, target["node"]).get("gaps") or []
    except Exception:  # a broken graph is validate's to report; the answer is still stored
        return None
    return next((g for g in gaps if g.get("type") == "conflict" and g.get("decide_scope")
                 and needs.gap_question_id(onto, g) == target["q"]), None)


def _record_conflict(repo: store.Repo, gap: Optional[Dict[str, Any]], text: str, src: Optional[str],
                     result: Dict[str, Any], notes: List[str]) -> None:
    """Record which value holds as a decision scoped to the member and the field (``needs.decide_scope``). The
    question names the field, not the node, so a decision id (a slug of it, never erased) holds no name."""
    text = util.normalize_ws(str(text or ""))
    if gap is None or not text:
        return
    if len(text) > DECISION_TEXT_MAX:  # the whole answer stays in its source, which the rationale names
        text = text[:DECISION_TEXT_MAX - 3].rstrip() + "..."
    field = str(gap.get("field") or "")
    try:
        dec = ledger.decide(repo, "Which value holds where linked records disagree on %s?" % field, [], "other",
                            chosen_text=text, rationale="answered in %s" % src if src else "",
                            scope=[str(gap["decide_scope"])], decided_by="user")
    except OntoError as exc:
        notes.append("the conflict on %s stays open: the decision could not be recorded (%s); record it with "
                     "onto decide --scope %s" % (field, exc.message, gap["decide_scope"]))
        return
    result["decision"] = dec.get("id")
    notes.append("recorded decision %s: %s holds for %s" % (dec.get("id"), render.quote(dec.get("chosen_text") or ""),
                                                            gap["decide_scope"]))


def answer(repo: store.Repo, q: str, text: Optional[str], status: str = "answered",
           ops: Optional[Sequence[Dict[str, Any]]] = None, apply: bool = False,
           preview: bool = False) -> Dict[str, Any]:
    """Record one answer (see the module docstring). Returns ``{question, status, answer: {id, src, duplicate},
    proposal, verdicts, applied, preview, richness_change, follow_ups, notes}``; once applied, ``applied.added`` names
    the nodes the answer created and the proposal's ``new_terms`` keeps only the names still not in the ontology.
    ``preview`` computes what would happen and writes nothing. Raises ``Refused`` on credentials in the text, on ops
    that cite another source and not the answer, or on ops the proposal checks refuse (nothing is kept then);
    ``UsageError`` on a status the log could not read as meant (``_check_status``)."""
    if status not in STATUSES:
        raise UsageError("status must be one of %s" % ", ".join(STATUSES))
    onto = Ontology.load(repo)
    target = resolve_question(onto, q)
    qid, node, key = target["q"], target["node"], target["id"]
    question = target["question"] or {}
    body = str(text or "")
    op_list = _ops_list(ops)
    notes: List[str] = []
    result: Dict[str, Any] = {
        "question": {"id": key, "q": qid, "node": node, "gap": target["gap"]},
        "status": status, "answer": None, "proposal": None, "verdicts": None, "applied": None, "preview": None,
        "richness_change": None, "follow_ups": list(question.get("follow_ups") or []), "notes": notes,
    }
    if target["resolved"]:
        result["resolved"] = target["resolved"]
    if status != "answered":
        if op_list:
            raise UsageError("ops need status answered; a %s answer appends only a log line" % status)
        _check_status(onto, target, status)
        if body.strip():
            notes.append("the text is not stored for status %s; only the log line is" % status)
        if preview:
            result["preview"] = {"log": {"q": qid, "node": node, "status": status}}
            return result
        with store.write_lock(repo):
            mutate.check_format(repo.reload())
            row = _append_log(repo, key, qid, node, status, None, None, "", util.now_iso())
        result["answer"] = {"id": row["id"], "src": None, "duplicate": False}
        return _with_next(repo, result)
    if not body.strip():
        raise UsageError("status answered needs the answer text (or use status skipped, na or later)")
    clean, src = _clean_text(repo, body)
    conflict = _conflict_gap(onto, target)
    op_list = _fill_prov(op_list, src, qid)
    foreign = foreign_ops(onto, op_list, src)
    if foreign:
        problems = [{"n": n, "code": "foreign_source", "message": "cites %s and not the answer; facts from that "
                     "source go in through propose" % ", ".join(cited)} for n, cited in foreign]
        raise Refused("op%s %s cite%s another source and not the answer; propose facts from %s with %s" % (
            "" if len(foreign) == 1 else "s", ", ".join(str(n) for n, _c in foreign),
            "s" if len(foreign) == 1 else "", ", ".join(sorted({s for _n, c in foreign for s in c})),
            render.call(False, "propose")), problems=problems)
    destructive = destructive_ops(onto, op_list)
    held = confirm_ops(onto, op_list)
    planned = verdicts_for(onto, op_list, src, clean) if apply else None
    if preview:
        problems = dry_run(repo, onto, key, op_list, src, clean, apply)
        result["preview"] = {
            "source": src, "apply": bool(apply), "ops": [
                {"n": n, "op": op.get("op"), "verdict": (planned or {}).get(str(n))} for n, op in
                enumerate(op_list, start=1)],
            "destructive": destructive, "confirm": held, "problems": problems,
        }
        result["verdicts"] = planned
        return result
    before = _richness(onto)
    with store.write_lock(repo):
        mutate.check_format(repo.reload())
        at = util.now_iso()
        log_rows, _problems = store.read_jsonl(repo.path(LOG))
        trial = copy.deepcopy(op_list)
        if apply:
            _drop_missing_quotes(trial, src, clean)
        found = _repeat(repo, _Log(_valid_rows(log_rows)).latest.get((qid, node)), src, trial, apply)
        if found is not None:
            row, prop = found
            result["answer"] = {"id": row["id"], "src": src, "duplicate": True}
            if prop is not None:
                result["proposal"] = _proposal_view(prop)
                result["verdicts"] = ((prop.get("review") or {}).get("verdicts") or None) if apply else None
                if prop.get("status") == "applied":
                    done = prop.get("applied") or {}
                    result["applied"] = {"change": done.get("change"), "results": done.get("results") or {},
                                         "ids": [r["id"] for _n, r in sorted((done.get("results") or {}).items(),
                                                                             key=lambda kv: int(kv[0])) if "id" in r],
                                         "note": "already applied"}
                    _settle(result, prop, done)
            notes.append("already recorded as %s with the same text%s; nothing new was written"
                         % (row["id"], " and ops" if op_list else ""))
            # a retry records the decision a killed call may have left out (none once the conflict is settled)
            _record_conflict(repo, conflict, clean, src, result, notes)
            return _with_next(repo, result)
        taken = {str(r.get("id")) for r in log_rows if r.get("id")}
        ans_id = _answer_id(key, body, at, taken)
        index_before = _read_bytes(repo.path(sources.INDEX))
        hidden = bool(node) and onto.is_local(node) and (onto.node(node) or {}).get("visibility", "shared") != "shared"
        entry, duplicate = _store_answer(repo, body, _title(question, qid, node, ans_id, hidden))
        if entry["id"] != src:  # the stored text differs from the preview's reading; cite what was stored
            src = entry["id"]
            op_list = _fill_prov(_ops_list(ops), src, qid)
        stored = sources.read(repo, src)
        proposal = None
        pending_before = set(pipeline._read_all(repo))
        if op_list:
            if apply:
                dropped = _drop_missing_quotes(op_list, src, stored)
                if dropped:
                    notes.append("quotes not found in the answer were dropped from op%s %s, which go in as drafts"
                                 % ("" if len(dropped) == 1 else "s", ", ".join(str(n) for n in dropped)))
            try:
                proposal = pipeline.prepare(repo, _draft(key, src, op_list), by="agent")
            except BaseException:
                if not duplicate:
                    _discard_source(repo, src, index_before)
                raise
        log_before = _read_bytes(repo.path(LOG))
        row = _append_log(repo, key, qid, node, "answered", src, proposal["id"] if proposal else None, body, at)
        result["answer"] = {"id": row["id"], "src": src, "duplicate": bool(duplicate)}
        if proposal is not None:
            result["proposal"] = _proposal_view(proposal)
        committed = None
        if proposal is not None and apply:
            verdicts = verdicts_for(onto, proposal.get("ops") or [], src, stored)
            late = [n for n in pipeline.destructive(proposal) if n not in destructive]
            if late:  # never apply a destructive op the caller did not see; leave it for review
                notes.append("op%s %s turned out destructive; the proposal waits for onto review"
                             % ("" if len(late) == 1 else "s", ", ".join(str(n) for n in late)))
            else:
                result["verdicts"] = verdicts
                try:
                    committed = pipeline.commit(repo, proposal["id"], verdicts=verdicts, by="user",
                                                change_type="answer", summary_prefix=_answer_prefix(key, proposal,
                                                                                                    verdicts))
                except OntoError as exc:
                    if not (isinstance(exc, Refused) and _calibration_only(exc)):
                        result["apply_error"] = exc.to_json()
                        notes.append("the answer is stored, but its ops could not be applied: %s; the proposal "
                                     "waits for onto review" % exc.message)
                    else:
                        _refuse_calibration(repo, exc, log_before, proposal["id"], pending_before, src,
                                            not duplicate, index_before)
        if committed is not None:
            for message in _new_calibration_warnings(onto, repo):
                # a draft risk only warns (W09); say so now, not first when an owner tries to approve it
                notes.append("W09 %s; an owner can approve the risk once this is fixed" % message)
            result["applied"] = {k: committed.get(k) for k in ("change", "results", "ids", "note") if k in committed}
            if result["proposal"] is not None:
                result["proposal"]["status"] = "applied"
            _settle(result, proposal, committed)
        else:
            summary = "answer to %s recorded as %s" % (key, src)
            if proposal is not None:
                summary += "; proposal %s is pending" % proposal["id"]
            ledger.append_change(repo, "answer", "user", [src], summary,
                                 proposal=proposal["id"] if proposal else None, source=src)
        _record_conflict(repo, conflict, stored or clean, src, result, notes)
    after = _richness(Ontology.load(repo)) if before is not None else None
    if before is not None and after is not None:
        result["richness_change"] = {"before": before, "after": after, "delta": after - before}
    return _with_next(repo, result)


def _with_next(repo: store.Repo, result: Dict[str, Any]) -> Dict[str, Any]:
    """``result`` with ``next``: the best question to ask now (``best_next``), or None. A failure to rank is a note,
    never a failed answer: the answer is already stored."""
    question = result.get("question") or {}
    added = [str(a.get("id")) for a in ((result.get("applied") or {}).get("added") or []) if a.get("id")]
    try:
        result["next"] = best_next(Ontology.load(repo), question.get("q") if result.get("status") == "answered"
                                   else None, added, [str(question.get("id") or "")])
    except OntoError as exc:
        result["next"] = None
        result["notes"].append("the next question could not be ranked: %s" % exc.message)
    return result


# commands ----------------------------------------------------------------------------------------------------------
def cmd_next(ctx: Context, args: Dict[str, Any]) -> Dict[str, Any]:
    onto = ctx.onto()
    stage = args.get("stage")
    if stage is not None:
        try:
            stage = int(stage)
        except (TypeError, ValueError):
            raise UsageError("stage must be a number from 0 to 9, got %r" % (stage,))
        if not 0 <= stage <= DEEPEN:
            raise UsageError("stage must be a number from 0 to 9, got %r" % (stage,))
    n = args.get("n")
    n = 3 if n is None else int(n)
    if n < 1:
        raise UsageError("n must be 1 or more")
    node_id = _about_node(onto, args.get("about"))
    state = _State(onto)
    listed, held = _split(_ranked(state, stage))
    if node_id:
        listed = _about_first(state, listed, node_id)
    prog = _progress(state)
    item = item_progress(prog)
    by_stage: Dict[str, int] = {}
    for cand in listed:
        by_stage[str(cand["stage"])] = by_stage.get(str(cand["stage"]), 0) + 1
    out = {"questions": [_public(c, item) for c in listed[:n]], "stage": prog["stage"], "progress": prog,
           "available": len(listed), "by_stage": dict(sorted(by_stage.items(), key=lambda kv: int(kv[0])))}
    if node_id:
        out["about"] = {"id": node_id, "first": sum(1 for c in listed if c.get("about"))}
    if held:  # put off in the last 2 days: named, never listed while another question is open
        out["held_back"] = {"count": len(held), "ids": [c["id"] for c in held[:MAX_N]], "days": RECENT_DAYS}
    if stage is not None:
        out["only_stage"] = stage
    return out


def _stage_text(n: Any) -> str:
    return "stage %s %s" % (n, STAGES.get(n, "")) if n is not None else "stage n/a"


def _more_line(result: Dict[str, Any], questions: List[Dict[str, Any]], more: int, ctx: Context) -> str:
    """``+N more`` with calls that reach every question left out: one ``n`` call when it can list them all (the CLI
    takes any n; MCP stops at 20), else the stage filter per stage, and ``onto gaps`` past 20 in one stage."""
    available = len(questions) + more
    only = result.get("only_stage")
    if not ctx.mcp or available <= MAX_N:
        return "+%d more (%s)" % (more, ctx.call("next", n=available, stage=only))
    shown: Dict[str, int] = {}
    for q in questions:
        shown[str(q.get("stage"))] = shown.get(str(q.get("stage")), 0) + 1
    by_stage = result.get("by_stage") or {}
    rest = ["%s (%d)" % (s, c - shown.get(str(s), 0)) for s, c in sorted(by_stage.items(), key=lambda kv: int(kv[0]))
            if c - shown.get(str(s), 0) > 0]
    if only is not None:  # one stage already: n=20 is as far as the call reaches
        line = "+%d more: %s lists %d of them" % (more, ctx.call("next", n=MAX_N, stage=only), MAX_N - len(questions))
    else:
        line = "+%d more: %s lists %d of them; by stage (%s): %s" % (
            more, ctx.call("next", n=MAX_N), max(0, MAX_N - len(questions)), ctx.call("next", stage="<n>", n=MAX_N),
            ", ".join(rest))
    if any(int(c) > MAX_N for c in by_stage.values()):
        line += "; past %d in one stage: %s" % (MAX_N, ctx.call("gaps"))
    return line


def _ask_line(i: int, q: Dict[str, Any]) -> str:
    ask = render.quote(render.plain(q.get("ask")))  # never cut: these are the words the agent asks
    marks = render.flags(q)
    if marks.get("untrusted"):
        ask = "[untrusted] " + ask  # the text names a node drafted from an ingested source
    where = "stage %s" % q.get("stage")
    if q.get("gap"):
        where = "gap %s on %s, %s" % (q["gap"].get("type"), render.mark(dict(marks, id=q.get("node"))), where)
    elif q.get("quick"):
        where = "quick start, " + where
    elif q.get("after_quick"):
        where = "right after the quick start, " + where
    put_off = q.get("put_off")
    if isinstance(put_off, dict):  # listed only because nothing else is open
        where += ", put off (%s) until %s" % (put_off.get("status"), str(put_off.get("until") or "")[:10])
    return "%d. %s  %s  (%s, score %s)" % (i, q.get("id"), ask, where, render.fmt(q.get("score")))


def _held_line(held: Dict[str, Any]) -> str:
    ids_ = [str(i) for i in held.get("ids") or []]
    count = int(held.get("count") or len(ids_))
    shown = ", ".join(ids_[:5]) + (", ..." if count > 5 else "")
    return "held back: %d put off (skipped or later) in the last %s days, not asked while other questions are open: " \
           "%s" % (count, held.get("days", RECENT_DAYS), shown)


def render_next(result: Dict[str, Any], mode: str, ctx: Context) -> List[str]:
    prog = result.get("progress") or {}
    quick = prog.get("quick") or {}
    head = "%s | quick start %s/%s | answered %s, skipped %s" % (
        _stage_text(result.get("stage")), quick.get("handled", 0), quick.get("total", 0), prog.get("answered", 0),
        prog.get("skipped", 0))
    if prog.get("closed_dimensions"):
        head += " | n/a: %s" % ", ".join(prog["closed_dimensions"])
    about = result.get("about")
    if isinstance(about, dict):
        first = int(about.get("first") or 0)
        head += " | about %s: %s" % (about.get("id"), "%d first" % first if first else
                                     "nothing open, the usual order")
    lines = [head]
    questions = result.get("questions") or []
    if not questions:
        lines.append("no open questions; feed it material (onto-ingest) or read the gaps (%s)" % ctx.call("gaps"))
        return lines
    if questions[0].get("put_off"):  # put-off questions are listed only when nothing else is open
        lines.append("only questions put off in the last %d days are left: ask one only if the user wants to come "
                     "back to it" % RECENT_DAYS)
    for i, q in enumerate(questions, start=1):
        lines.append(_ask_line(i, q))
        if q.get("options"):
            labels = [o.get("label") if isinstance(o, dict) else str(o) for o in q["options"]]
            ids_ = [o.get("id") if isinstance(o, dict) else str(o) for o in q["options"]]
            lines.append("   options: %s" % "; ".join("%s=%s" % (a, render.plain(b)) for a, b in zip(ids_, labels)))
        if q.get("why"):
            lines.append("   why: %s" % render.plain(q.get("why")))
        if q.get("follow_ups"):
            lines.append("   follow-ups: %s" % ", ".join(str(f) for f in q["follow_ups"]))
        fills = q.get("fills") or {}
        if mode == "text":
            if fills.get("kinds") or fills.get("fields"):
                lines.append("   fills: %s" % "; ".join("%s %s" % (k, ", ".join(v)) for k, v in sorted(fills.items())
                                                        if v))
        elif fills.get("fields") and not q.get("gap"):
            # the fields the answer should set (attrs), or the kit asks for them again as gap questions
            lines.append("   sets: %s (attrs of the record the answer adds)" % ", ".join(fills["fields"]))
    more = int(result.get("available") or 0) - len(questions)
    if more > 0:
        lines.append(_more_line(result, questions, more, ctx))
    if result.get("held_back"):
        lines.append(_held_line(result["held_back"]))
    first = questions[0].get("id")
    lines.append("Answer: %s%s; or status=skipped, later, na or skip_stage" % (
        answer_call(ctx, first), "" if ctx.mcp else " (with the user's words saved in %s first)" % ANSWER_FILE))
    return lines


ANSWER_FILE = ".onto/answer.txt"  # gitignored; the CLI reads the answer from it, never through a shell


def answer_call(ctx: Context, q: Any) -> str:
    """The call that records an answer to ``q``: over MCP with the text as an argument, on the CLI from
    ``ANSWER_FILE`` (``--text-file``), since a shell line reads ``$``, backticks and quotes in the user's words."""
    if ctx.mcp:
        return ctx.call("answer", q=q, text="<the user's words>", apply=True)
    return ctx.call("answer", q=q, text_file=ANSWER_FILE, apply=True)


def cmd_answer(ctx: Context, args: Dict[str, Any]) -> Dict[str, Any]:
    status = args.get("status") or "answered"
    ops = args.get("ops")
    apply = bool(args.get("apply"))
    gate = False
    if status == "answered" and apply and ops:
        gate = bool(confirm_ops(ctx.onto(), _ops_list(ops))) and not args.get("confirm")
    ctx.preview = gate  # only an apply holding destructive or pack ops waits for confirm=true; the rest writes at once
    try:
        result = answer(ctx.repo, args.get("q"), args.get("text"), status=status, ops=ops, apply=apply, preview=gate)
    except Refused as exc:
        if not about_ops(exc):
            raise  # not about the ops (damaged files, credentials): its message says it all
        ctx.preview = False
        return _refusal(args, status, list(exc.problems), None)
    problems = (result.get("preview") or {}).get("problems") or []
    if gate and problems:  # the confirmed call would be refused: say so now, not after the user said yes
        ctx.preview = False
        refused = _refusal(args, status, problems, result["preview"])
        refused["question"] = result.get("question")
        return refused
    return result


def _refusal(args: Dict[str, Any], status: str, problems: List[Dict[str, Any]],
             preview: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The result of a refused answer, as ``onto propose`` returns a refused proposal: ``error: "refused"``, exit
    code 1 and every problem by op number. ``preview`` is set when the confirm gate's dry run found the problems."""
    count = len(problems)
    return {
        "question": {"id": str(args.get("q") or "")}, "status": status, "error": "refused",
        "message": "answer refused: %d problem%s; nothing was written. Fix every problem and answer again"
                   % (count, "" if count == 1 else "s"),
        "exit_code": 1, "problems": problems, "answer": None, "proposal": None, "verdicts": None, "applied": None,
        "preview": preview, "richness_change": None, "follow_ups": [], "notes": [],
    }


def _problem_line(p: Dict[str, Any]) -> str:
    where = "op %s " % p["n"] if p.get("n") is not None else ""
    return "problem: %s%s %s" % (where, p.get("code"), render.plain(p.get("message")))


def _refusal_lines(result: Dict[str, Any]) -> List[str]:
    problems = result.get("problems") or []
    count = len(problems)
    head = "refused: %d problem%s; nothing was written" % (count, "" if count == 1 else "s")
    if result.get("preview") is not None:
        head += ", and confirm=true would be refused the same way"
    return [head + ". Fix every problem below and answer again."] + [_problem_line(p) for p in problems]


def _results_line(applied: Dict[str, Any]) -> str:
    counts: Dict[str, int] = {}
    for res in (applied.get("results") or {}).values():
        label = res.get("status") or ("rejected" if res.get("skipped") else "done")
        label = {"confirmed": "confirmed", "proposed": "draft"}.get(label, label)
        counts[label] = counts.get(label, 0) + 1
    return ", ".join("%d %s" % (v, k) for k, v in sorted(counts.items()))


def render_answer(result: Dict[str, Any], mode: str, ctx: Context) -> List[str]:
    question = result.get("question") or {}
    lines: List[str] = []
    if result.get("resolved"):
        r = result["resolved"]
        lines.append("resolved %s -> %s" % (render.quote(str(r.get("query"))), r.get("id")))
    if result.get("error"):
        return lines + _refusal_lines(result)
    preview = result.get("preview")
    if preview is not None:
        if "log" in preview:
            lines.append("preview: would log %s as %s" % (question.get("id"), result.get("status")))
            return lines
        by_verdict: Dict[str, List[str]] = {}
        for op in preview.get("ops") or []:
            by_verdict.setdefault(str(op.get("verdict") or "pending"), []).append(str(op.get("n")))
        parts = ["%s %s" % (v, ",".join(ns)) for v, ns in sorted(by_verdict.items())]
        problems = preview.get("problems") or []
        if problems:  # the dry run found what the proposal checks refuse: nothing would be stored
            lines.append("preview for %s: would be refused: %d problem%s; fix every problem below and answer again"
                         % (question.get("id"), len(problems), "" if len(problems) == 1 else "s"))
            return lines + [_problem_line(p) for p in problems]
        lines.append("preview for %s: would store the answer as %s and %s %d op(s)%s" % (
            question.get("id"), preview.get("source"), "apply" if preview.get("apply") else "propose",
            len(preview.get("ops") or []), (": " + "; ".join(parts)) if parts else ""))
        kinds = {str(op.get("n")): op.get("op") for op in preview.get("ops") or []}
        flag = "" if getattr(ctx, "mcp", False) else ": pass --confirm"  # the CLI's flag for MCP's confirm=true
        held = preview.get("confirm") or []
        shown = [n for n in preview.get("destructive") or [] if n in held]
        if shown:
            lines.append("destructive: %s need confirm=true%s" % (", ".join(
                "%s (%s)" % (n, kinds.get(str(n))) for n in shown), flag))
        changes = [n for n in held if n not in (preview.get("destructive") or [])]
        if changes:
            lines.append("pack or question changes: %s need confirm=true%s" % (", ".join(
                "%s (%s)" % (n, kinds.get(str(n))) for n in changes), flag))
        return lines
    ans = result.get("answer") or {}
    if ans.get("src"):
        lines.append("answer %s to %s (%s) stored as %s%s" % (
            ans.get("id"), question.get("id"), result.get("status"), ans.get("src"),
            " (same text as before)" if ans.get("duplicate") else ""))
    else:
        lines.append("answer %s to %s: %s (log only)" % (ans.get("id"), question.get("id"), result.get("status")))
    prop = result.get("proposal")
    applied = result.get("applied")
    if prop:
        text = "proposal %s: %d op(s), %s" % (prop.get("id"), prop.get("ops") or 0, prop.get("status"))
        if applied:
            text += " as %s: %s" % (applied.get("change"), _results_line(applied))
        lines.append(text)
        if applied and applied.get("ids"):
            shown = list(applied["ids"])[:8]
            lines.append("ids: %s%s" % (", ".join(shown), " " + render.more(len(applied["ids"]), len(shown))
                                        if len(applied["ids"]) > len(shown) else ""))
        added = (applied or {}).get("added") or []
        if added:  # the new names are in the ontology now: the ops that created them were applied
            shown = [render.mark({"status": a.get("status")}, render.trunc(a.get("name") or a.get("id"), 40))
                     for a in added[:8]]
            lines.append("added: %s%s" % (", ".join(shown), " " + render.more(len(added), len(shown))
                                          if len(added) > len(shown) else ""))
        for m in (prop.get("matches") or [])[:5]:
            lines.append("match: op %s may be %s (%s, %s)" % (m.get("n"), m.get("id"), render.fmt(m.get("score")),
                                                             render.trunc(m.get("why"), 60)))
        if prop.get("new_terms"):
            lines.append("not in the ontology yet: %s" % ", ".join(render.trunc(t, 40) for t in prop["new_terms"][:8]))
        if mode == "text":
            for w in prop.get("warnings") or []:
                lines.append("warning: op %s %s %s" % (w.get("n"), w.get("code"), render.plain(w.get("message"))))
        if not applied:
            lines.append("review: %s" % ctx.call("review", id=prop.get("id")))
    change = result.get("richness_change")
    if change:
        lines.append("richness %s -> %s (%+d)" % (change.get("before"), change.get("after"), int(change.get("delta"))))
    for note in result.get("notes") or []:
        lines.append("note: %s" % note)
    if result.get("follow_ups") and mode == "text":
        lines.append("follow-ups: %s" % ", ".join(result["follow_ups"]))
    best = result.get("next")
    if isinstance(best, dict):
        # never cut: the agent asks these words (as onto next prints them)
        lines.append("ask next: %s  %s  (%s)" % (best.get("id"), render.quote(render.plain(best.get("ask"))),
                                                 best.get("reason")))
    lines.append("Next: %s" % ctx.call("next"))
    return lines
