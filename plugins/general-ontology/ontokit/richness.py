"""Richness and gaps: the measures, the score, the topic-level gaps, the ranked gap list, source suggestions,
calibration and the tools check.

**Measures** (over active local records; background edges never count):

- ``coverage``: the mean, over the dimensions not closed by an ``na`` answer, of min(1, substantive nodes of the
  dimension's kinds / target) (``needs.dimension_coverage``), plus a ``bridges`` dimension when the topic has
  imports: the mean over import pairs of min(1, local bridges between the pair / 2). An import pair is a pair of
  direct imports (a bundled import belongs to the direct import that brought it); with one direct import the pair
  is this topic and that import.
- ``completeness``: filled expected fields / expected fields (1 if none).
- ``connectivity``: (met ``expects`` minimums / required minimums, or 1 if none) x (1 - orphan share).
- ``evidence``: the mean over nodes and edges of 0 (no provenance), 0.5 (one distinct source) or 1 (two or more).
- ``confirmation``: confirmed / (confirmed + proposed), or 1 if none.
- ``depth`` (a scale, not a weighted part): how much sourced material the topic holds, 1 - 2^(-evidence sum /
  target sum). The evidence sum adds up the evidence of every record above (a single-source record counts 0.5, a
  corroborated one 1); the target sum adds up the targets of the measured dimensions (plus ``BRIDGE_TARGET`` per
  import pair). Depth is 0.5 when the two are equal (a topic that just meets every target, one source per record)
  and keeps growing, more slowly, with every sourced record or added source. It is null, with a reason, when there
  is no target to add up (every dimension closed).

The five parts are ratios, so a new single-source record, a draft or a record short of a link pulls one down
however much it adds. Depth is the one measure that grows with the material, so the score keeps rising as reviewed,
sourced material comes in, while the parts still say how well kept it is.

Every per-node fact comes from ``needs`` (the one per-node computation). A value that cannot be measured is null,
with the reason under ``missing``.

**Score** = round(sum of w x m / sum of w x 100) with ``policy.weights``, over the parts that could be measured.
Coverage counts in full; the other four parts count in proportion to coverage x depth (m x coverage x depth). A
part measured over a handful of records says little about a topic that has not covered its dimensions yet, or holds
little material for them: scaled by coverage alone, a topic whose small targets a short interview met scored its
plain weighted mean ("deep" at once) and then stood still or fell as material came in; scaled by nothing, a fresh
topic (one root node, confirmed, nothing expected of it) would score about 60, "rich". A null depth scales nothing.
A coverage weight of 0 means coverage does not count at all: it is left out of the sum and scales nothing (depth
neither), so the score is the plain weighted mean of the other parts (``SCALE_BY_COVERAGE = False`` gives that
plain mean in every case). Bands: seed < 20, sketch < 40, working < 60, rich < 80, deep >= 80. With the default
weights and one source per record (every other part at 1), a topic that just meets every target scores 58
(working), and "deep" takes an evidence sum of about 2.5 times the target sum. The score is a heuristic; the parts
are the facts.

**Topic gaps** (the node gaps come from ``needs``):

- ``missing_dimension``: a dimension (or ``bridges``) below ``policy.stage_done_at``. Imported nodes count here, as
  in the interview, so a dimension the imports already cover is not asked about again; its ask is the dimension's
  top unanswered bank question, and its ``stage`` is that question's own stage (the ``next`` call that offers it).
- ``unbridged_import``: an imported kind (per namespace) related to a goal with no node touched by a bridge. A kind
  is related when one of its nodes lies within 2 hops of an active goal node (in any namespace). When no node of
  the namespace does (the import is not linked to any goal yet), or when there is no goal at all, every kind of
  the namespace counts.
- ``uncited_source``: a source that no active local record and no open proposal cites (erased and superseded
  sources are left out).
- ``duplicate``: every likely duplicate pair from ``entities.duplicates``. Above 2,000 active local nodes the pairs
  are listed only when ``type=duplicate`` or ``node`` asks for them (the pair search grows with the topic), and
  the unfiltered result carries a note naming that call.
- ``pending_backlog``: open proposals waiting for review.

``ranked_gaps`` merges both kinds and sorts them by severity (``needs.GAP_TYPES``), then topic gaps before node
gaps, then nodes near a goal, then by degree, then by id. The node gaps are those of every active local node, plus
the ``conflict`` and local ``dangling_bridge`` gaps of the imported ends of local bridges (``bridge_end_gaps``, the
rule the interview asks by), so a bridge between two imports shows its gaps here as it does in ``next``.

**Follow-up calls** name only tools the caller has: every command on the CLI, the active profile's tools over MCP.
The read-only query profile has no ``next`` and no ``review``, so there a stage question or a backlog reads "ask
the user", with a read call where one helps.

**Calibration** is the Brier score of the confidence of reviewed proposal ops against their verdicts (accept 1,
edit or draft 0.5, reject 0); an op whose schema declares ``conf`` (``add_node``, ``add_edge``, ``update_node``,
``update_edge``) and leaves it out forecasts the default 0.7.

**In-flight proposal.** ``point(onto, kind, in_flight=proposal)`` measures the point of an apply or answer as it
will stand once the proposal is saved: the proposal no longer counts as pending, and its review (as ``in_flight``
carries it) counts in the Brier score in place of the stored copy.
"""

from __future__ import annotations

import glob
import json
import math
import os
import re
import shlex
import shutil
from itertools import combinations
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import __version__, entities, history, pipeline, records, render, store, util
from . import commands as commands_mod
from . import ids as idmod
from . import needs as needs_mod
from . import sources as sources_mod
from .errors import DataError, NotFound, UsageError

PARTS = ("coverage", "completeness", "connectivity", "evidence", "confirmation")
SHORT = {"coverage": "cov", "completeness": "comp", "connectivity": "conn", "evidence": "evid", "confirmation": "conf",
         "depth": "depth"}
DEFAULT_WEIGHTS = {"coverage": 30, "completeness": 20, "connectivity": 15, "evidence": 20, "confirmation": 15}
BANDS = ((20, "seed"), (40, "sketch"), (60, "working"), (80, "rich"))
TOP_BAND = "deep"
BRIDGE_TARGET = 2
GOAL_HOPS = 2
STAGE_DONE_AT = 0.6
COMPOSE_STAGE = 8
COMPOSE_QUESTION = "q.compose.meet"
LOG = "interview/log.jsonl"
TOOLS_REPORT = ".onto/tools-check.json"
OUTCOMES = {"accept": 1.0, "edit": 0.5, "draft": 0.5, "reject": 0.0}
DEFAULT_CONF = 0.7
POINT_KINDS = ("init", "apply", "answer", "ingest", "import", "decide", "erase", "release", "migrate", "checkpoint",
               "pack")
SECTIONS = ("gaps", "summary", "history", "calibration")
DUPLICATE_MAX_NODES = 2000
SUGGEST_PER_GAP = 3
SUGGEST_MAX_GAPS = 50
MIN_PHRASE = 3
SCALE_BY_COVERAGE = True
HEURISTIC = "The score is a heuristic; the parts are the facts."
# a gap's action where the call that closes it is not in the caller's MCP profile (the read-only query profile)
READ_ONLY_ACTIONS = {
    "missing_dimension": "ask the user the question",
    "unbridged_import": "read the example, then ask the user the question",
    "pending_backlog": "ask the user to review the pending proposals",
}
READ_ONLY_NOTE = ("this profile reads only: record answers and reviews from a session inside the topic repo "
                  "or with $ONTO_REPO pointing at it (full profile)")
FORMULA = ("score = sum(w x m) / sum(w) x 100 over the measured parts; coverage counts in full, the other parts "
           "in proportion to coverage x depth (a coverage weight of 0 leaves coverage out and scales nothing)")
DEPTH_RULE = ("depth = 1 - 2^(-evidence sum / target sum): each record adds 0 (no source), 0.5 (one) or 1 (two or "
              "more), and the target sum adds up the measured dimensions' targets; 0.5 when the two are equal, and it "
              "keeps growing with every sourced record")
PLAIN_FORMULA = "score = sum(w x m) / sum(w) x 100 over the measured parts"
SCORING = ("accept 1, edit or draft 0.5, reject 0; brier = mean (conf - outcome)^2 over reviewed ops that can carry "
           "a confidence (add_node, add_edge, update_node and update_edge; 0.7 when left out); lower is better, and "
           "a constant 0.5 forecast scores 0.25 at best")
_COUNT_RE = re.compile(r"^\s*([0-9]+) of ([0-9]+)\s*$")
_COMMAND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_CAL_CACHE: Dict[str, Tuple[Tuple[Any, ...], Dict[str, List[Dict[str, Any]]]]] = {}


# small helpers -------------------------------------------------------------------------------------------------
def _round(value: Optional[float], digits: int = 4) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def _half_up(value: float) -> int:
    return int(math.floor(round(value, 6) + 0.5))


def _fill(template: str, **values: Any) -> str:
    out = str(template or "")
    for key, value in values.items():
        out = out.replace("{%s}" % key, str(value if value is not None else ""))
    return util.normalize_ws(out)


def _bare(kind: str) -> str:
    return str(kind or "").split("/")[-1]


def _stat(path: str) -> Optional[Tuple[int, int]]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _policy(onto: Any) -> Dict[str, Any]:
    policy = dict(store.DEFAULT_POLICY)
    policy.update((onto.manifest or {}).get("policy") or {})
    return policy


def _weights(onto: Any) -> Dict[str, float]:
    """``policy.weights`` with the defaults for missing or unusable entries (negative weights count as 0)."""
    raw = _policy(onto).get("weights") or {}
    out: Dict[str, float] = {}
    for key in PARTS:
        value = raw.get(key, DEFAULT_WEIGHTS[key]) if isinstance(raw, dict) else DEFAULT_WEIGHTS[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            value = DEFAULT_WEIGHTS[key]
        out[key] = max(0.0, float(value))
    return out


def _stage_done_at(onto: Any) -> float:
    value = _policy(onto).get("stage_done_at", STAGE_DONE_AT)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return STAGE_DONE_AT
    return float(value)


def _bank(onto: Any) -> List[Dict[str, Any]]:
    cached = onto._cache.get("richness_bank")
    if cached is None:
        cached = [q for q in onto.registry.questions() if isinstance(q, dict) and q.get("id")]
        onto._cache["richness_bank"] = cached
    return cached


def _log_rows(onto: Any) -> List[Dict[str, Any]]:
    if onto.repo is None:
        return []
    rows, _problems = store.read_jsonl(onto.repo.path(LOG))
    return rows


def _latest_answers(onto: Any) -> Dict[str, str]:
    """``{question id: latest status}`` for the questions asked of the topic as a whole (no ``node``)."""
    ordered = sorted(enumerate(_log_rows(onto)), key=lambda pair: (str(pair[1].get("at") or ""), pair[0]))
    out: Dict[str, str] = {}
    for _n, row in ordered:
        if row.get("node") or not isinstance(row.get("q"), str):
            continue
        out[row["q"]] = str(row.get("status") or "")
    return out


def closed_dimensions(onto: Any) -> List[str]:
    """The dimensions closed by an ``na`` answer: a question whose latest answer is ``na`` closes its dimension
    (C.11). The interview reads the same log, so it can call this to agree with richness."""
    bank = {q["id"]: q for q in _bank(onto)}
    closed: Set[str] = set()
    for qid, status in _latest_answers(onto).items():
        dim = (bank.get(qid) or {}).get("dimension")
        if status == "na" and isinstance(dim, str) and dim:
            closed.add(dim)
    return sorted(closed)


# bridges and goals ---------------------------------------------------------------------------------------------
def _groups(onto: Any) -> Dict[str, str]:
    """``{import ns: the direct import that brought it}`` (a direct import maps to itself)."""
    entries = {e["ns"]: e for e in onto.imports if isinstance(e.get("ns"), str)}
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


def _pairs(onto: Any) -> List[Tuple[str, str]]:
    direct = sorted(set(_groups(onto).values()))
    if len(direct) >= 2:
        return [tuple(sorted(p)) for p in combinations(direct, 2)]  # type: ignore
    if direct:
        return [tuple(sorted(("self", direct[0])))]  # type: ignore
    return []


def _local_bridges(onto: Any) -> List[str]:
    return [eid for eid in onto.local_edge_ids
            if eid in onto.bridges and onto.active(eid) and not onto.edges[eid].get("background")]


def bridge_counts(onto: Any) -> Dict[str, int]:
    """``{"a|b": count}`` of active local bridges by the namespaces they join (``self`` for this topic)."""
    out: Dict[str, int] = {}
    for eid in _local_bridges(onto):
        edge = onto.edges[eid]
        key = "|".join(sorted((onto.ns_of(str(edge.get("src"))), onto.ns_of(str(edge.get("dst"))))))
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def _bridge_pairs(onto: Any) -> Dict[str, int]:
    """``{"a|b": local bridges}`` for every import pair (a bundled import counts as its direct import)."""
    groups = _groups(onto)
    counts = {"|".join(p): 0 for p in _pairs(onto)}
    for eid in _local_bridges(onto):
        edge = onto.edges[eid]
        ends = []
        for end in (str(edge.get("src")), str(edge.get("dst"))):
            ns = onto.ns_of(end)
            ends.append("self" if ns == "self" else groups.get(ns, ns))
        key = "|".join(sorted(ends))
        if key in counts:
            counts[key] += 1
    return counts


def _bridges_value(pair_counts: Dict[str, int]) -> Optional[float]:
    if not pair_counts:
        return None
    return sum(min(1.0, float(n) / BRIDGE_TARGET) for n in pair_counts.values()) / len(pair_counts)


def _goal_reach(onto: Any, hops: int = GOAL_HOPS) -> Tuple[Set[str], bool]:
    """``(nodes within hops of an active goal node, whether any goal exists)``. A ``same_as`` class is one node;
    hubs are reached but not expanded; background edges are not followed."""
    cached = onto._cache.get("richness_goal_reach")
    if cached is not None:
        return cached
    starts = sorted(n for n in onto.nodes
                    if n not in onto.virtual and onto.active(n) and _bare(onto.kind_of(n)) == "goal")
    dist: Dict[str, int] = {}
    frontier: List[str] = []
    for start in starts:
        for member in onto.members(start):
            if member not in dist:
                dist[member] = 0
                frontier.append(member)
    for depth in range(1, hops + 1):
        nxt: List[str] = []
        for nid in frontier:
            if onto.registry.is_hub(onto.kind_of(nid)) and depth > 1:
                continue
            for e in onto.edges_of(nid):
                if e["edge"].get("background") or e["other"] in onto.virtual:
                    continue
                for member in onto.members(e["other"]):
                    if member not in dist:
                        dist[member] = depth
                        nxt.append(member)
        frontier = nxt
    result = (set(dist), bool(starts))
    onto._cache["richness_goal_reach"] = result
    return result


# the measures --------------------------------------------------------------------------------------------------
def _expects_counts(onto: Any, nid: str, need: Dict[str, Any]) -> Tuple[int, int]:
    """``(met, required)`` minimums of the kind's ``expects`` for one node, read from its ``needs``."""
    if isinstance(need.get("expects_required"), int) and isinstance(need.get("expects_met"), int):
        return int(need["expects_met"]), int(need["expects_required"])
    required = 0
    for exp in onto.registry.expects(onto.kind_of(nid)):
        required += int(exp.get("min") or 1)
    short = 0
    for gap in need.get("gaps") or []:
        if gap.get("type") != "missing_relation":
            continue
        m = _COUNT_RE.match(str(gap.get("note") or ""))
        if m:
            short += max(0, int(m.group(2)) - int(m.group(1)))
        else:
            short += 1
    return max(0, required - short), required


def _prov_score(prov: Any) -> float:
    distinct = {str(p.get("src")) for p in prov or [] if isinstance(p, dict) and p.get("src")}
    return 0.0 if not distinct else (0.5 if len(distinct) == 1 else 1.0)


def _depth(mass: float, scale: int) -> Optional[float]:
    """1 - 2^(-mass / scale): 0 with no sourced record, 0.5 when the evidence sum equals the target sum, then
    growing with diminishing returns toward 1 (every sourced record still counts); None without a scale."""
    if scale <= 0:
        return None
    return 1.0 - math.pow(2.0, -float(mass) / float(scale))


def _core(onto: Any) -> Dict[str, Any]:
    """The five parts with their denominators, the dimension values and the reasons for nulls; cached on the
    graph object, keyed by the interview log (an ``na`` answer changes coverage without changing the graph)."""
    stamp = _stat(onto.repo.path(LOG)) if onto.repo is not None else None
    cached = onto._cache.get("richness_core")
    if cached is not None and cached[0] == stamp:
        return cached[1]
    all_needs = needs_mod.all_needs(onto)
    node_ids = sorted(all_needs)
    edge_ids = [eid for eid in onto.local_edge_ids if onto.active(eid) and not onto.edges[eid].get("background")]
    values: Dict[str, Optional[float]] = {}
    denominators: Dict[str, Optional[int]] = {}
    missing: Dict[str, str] = {}

    closed = closed_dimensions(onto)
    dims = dict(needs_mod.dimension_coverage(onto, closed))
    pair_counts = _bridge_pairs(onto) if onto.imports else {}
    bridges = _bridges_value(pair_counts)
    if bridges is not None:
        dims["bridges"] = bridges
    denominators["coverage"] = len(dims)
    if dims:
        values["coverage"] = sum(dims.values()) / len(dims)
    else:
        values["coverage"] = None
        missing["coverage"] = "no dimension to measure (none declared, or every one closed as n/a)"

    expected = sum(int(n.get("expected") or 0) for n in all_needs.values())
    filled = sum(int(n.get("filled") or 0) for n in all_needs.values())
    values["completeness"] = float(filled) / expected if expected else 1.0
    denominators["completeness"] = expected

    met = required = orphans = 0
    for nid in node_ids:
        need = all_needs[nid]
        m, r = _expects_counts(onto, nid, need)
        met += m
        required += r
        if any(g.get("type") == "orphan" for g in need.get("gaps") or []):
            orphans += 1
    share = float(orphans) / len(node_ids) if node_ids else 0.0
    values["connectivity"] = (float(met) / required if required else 1.0) * (1.0 - share)
    denominators["connectivity"] = required

    scores = [{0: 0.0, 1: 0.5}.get(int(all_needs[n].get("sources") or 0), 1.0) for n in node_ids]
    scores += [_prov_score(onto.edges[eid].get("prov")) for eid in edge_ids]
    denominators["evidence"] = len(scores)
    if scores:
        values["evidence"] = sum(scores) / len(scores)
    else:
        values["evidence"] = None
        missing["evidence"] = "no active local records"

    statuses = [str((onto.nodes[n] or {}).get("status")) for n in node_ids]
    statuses += [str(onto.edges[eid].get("status")) for eid in edge_ids]
    confirmed = statuses.count("confirmed")
    proposed = statuses.count("proposed")
    values["confirmation"] = float(confirmed) / (confirmed + proposed) if confirmed + proposed else 1.0
    denominators["confirmation"] = confirmed + proposed

    # depth: the evidence sum against the targets of the measured dimensions (see the module docstring)
    targets = onto.registry.dimensions()
    scale = sum(int((targets.get(dim) or {}).get("target") or 0) for dim in dims if dim != "bridges")
    if "bridges" in dims:
        scale += BRIDGE_TARGET * len(pair_counts)
    mass = sum(scores)
    depth = _depth(mass, scale)
    denominators["depth"] = scale if depth is not None else None
    if depth is None:
        missing["depth"] = "no dimension target to scale it by"

    result = {
        "values": {k: _round(values[k]) for k in PARTS},
        "denominators": denominators,
        "depth": {"value": _round(depth), "mass": _round(mass, 2), "scale": scale},
        "dimensions": {k: _round(v) for k, v in sorted(dims.items())},
        "bridge_pairs": pair_counts,
        "closed": closed,
        "missing": missing,
        "orphans": orphans,
        "nodes": len(node_ids),
    }
    onto._cache["richness_core"] = (stamp, result)
    return result


def band(value: Optional[int]) -> Optional[str]:
    """seed < 20, sketch < 40, working < 60, rich < 80, deep >= 80; None for None."""
    if value is None:
        return None
    for limit, name in BANDS:
        if value < limit:
            return name
    return TOP_BAND


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def score(values: Dict[str, Any], weights: Optional[Dict[str, Any]] = None) -> Tuple[Optional[int], Optional[str]]:
    """``(score 0 to 100, band)`` from the parts in ``values`` and its ``depth`` (see the module docstring);
    ``(None, None)`` when no part with a positive weight was measured. Null parts are left out of both sums, and a
    part with weight 0 counts nowhere: with a coverage weight of 0 the other parts are scaled by neither coverage nor
    depth. A null or absent depth scales nothing."""
    w = dict(DEFAULT_WEIGHTS)
    for key, value in (weights or {}).items():
        if key in w and not isinstance(value, bool) and isinstance(value, (int, float)):
            w[key] = max(0.0, float(value))
    coverage = _number(values.get("coverage"))
    depth = _number(values.get("depth"))
    scale = 1.0
    if SCALE_BY_COVERAGE and coverage is not None and w["coverage"] > 0:
        scale = coverage * (min(1.0, max(0.0, depth)) if depth is not None else 1.0)
    total = weighted = 0.0
    for key in PARTS:
        value = values.get(key)
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or w[key] <= 0:
            continue
        part = float(value) if key == "coverage" else float(value) * scale
        weighted += w[key] * part
        total += w[key]
    if total <= 0:
        return None, None
    result = max(0, min(100, _half_up(weighted / total * 100.0)))
    return result, band(result)


def _open_questions(all_needs: Dict[str, Dict[str, Any]]) -> int:
    return sum(1 for n in all_needs.values() for g in n.get("gaps") or [] if g.get("type") == "open_question")


def _in_flight_id(in_flight: Optional[Dict[str, Any]]) -> Optional[str]:
    pid = in_flight.get("id") if isinstance(in_flight, dict) else None
    return pid if isinstance(pid, str) and pid else None


def _breakdowns(onto: Any, core: Dict[str, Any], in_flight: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    by_kind: Dict[str, int] = {}
    for nid in onto.local_nodes(active_only=True):
        k = onto.kind_of(nid)
        by_kind[k] = by_kind.get(k, 0) + 1
    by_rel: Dict[str, int] = {}
    for eid in onto.local_edge_ids:
        if onto.active(eid):
            rel = str(onto.edges[eid].get("rel"))
            by_rel[rel] = by_rel.get(rel, 0) + 1
    skip = _in_flight_id(in_flight)
    pending = len([p for p in _open_proposals(onto) if p.get("id") != skip])
    return {
        "nodes_by_kind": dict(sorted(by_kind.items())),
        "edges_by_rel": dict(sorted(by_rel.items())),
        "bridges": bridge_counts(onto),
        "dimensions": dict(core["dimensions"]),
        "sources": len(onto.sources),
        "pending": pending,
        "open_questions": _open_questions(needs_mod.all_needs(onto)),
    }


def measures(onto: Any, in_flight: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """``{values, denominators, breakdowns, missing}`` as a history point holds them (C.14): the five parts, the
    score as ``richness`` and the Brier score as ``brier``. ``in_flight`` is a proposal being applied (see the
    module docstring)."""
    core = _core(onto)
    values: Dict[str, Optional[float]] = dict(core["values"])
    values["depth"] = core["depth"]["value"]
    missing = dict(core["missing"])
    total, _band = score(values, _weights(onto))
    values["richness"] = total
    if total is None:
        missing["richness"] = "no part could be measured"
    denominators = dict(core["denominators"])
    if onto.repo is not None:
        cal = calibration(onto.repo, in_flight=in_flight)
        values["brier"] = cal["brier"]
        denominators["brier"] = cal["n"]
        if cal["brier"] is None:
            missing["brier"] = "no reviewed op with a confidence yet"
    else:
        values["brier"] = None
        denominators["brier"] = None
        missing["brier"] = "no topic repo to read proposals from"
    breakdowns = _breakdowns(onto, core, in_flight)
    breakdowns["depth_mass"] = core["depth"]["mass"]
    return {"values": values, "denominators": denominators, "breakdowns": breakdowns,
            "missing": dict(sorted(missing.items()))}


def point(onto: Any, kind: str, in_flight: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A history point (C.14) measured on ``onto``; ``kind`` is the change type (unknown types read ``apply``).
    ``in_flight`` is the proposal this change applies, with its final ``review``: it is measured as saved (not
    pending, its verdicts in the Brier score), since the point is written before the proposal file moves."""
    m = measures(onto, in_flight=in_flight)
    return history.new_point(kind if kind in POINT_KINDS else "apply", m["values"], m["denominators"],
                             m["breakdowns"], m["missing"])


def _history(onto: Any) -> List[Dict[str, Any]]:
    """The history points, cached on the graph object by the file's size and time (every command's version line
    reads them)."""
    if onto.repo is None:
        return []
    stamp = _stat(history.path(onto.repo))
    cached = onto._cache.get("richness_history")
    if cached is not None and cached[0] == stamp:
        return cached[1]
    points = history.read(onto.repo)
    onto._cache["richness_history"] = (stamp, points)
    return points


def _change(points: Sequence[Dict[str, Any]], current: Optional[int], days: int) -> Optional[Dict[str, Any]]:
    """The change of the score over ``days``: the current score against the history (``history.change``)."""
    if current is None:
        return None
    rows = [p for p in points if isinstance(p, dict)]
    last_at = max([str(p.get("at") or "") for p in rows] or [""])
    at = max(util.now_iso(), last_at)
    found = history.change(rows + [{"at": at, "values": {"richness": current}}], "richness", days)
    if found is None:
        return None
    delta, since = found
    return {"delta": delta, "at": since, "text": history.fmt_change(delta, since)}


def _scales(weights: Dict[str, float]) -> bool:
    """Whether coverage x depth scales the other parts (the switch is on and coverage has a weight)."""
    return SCALE_BY_COVERAGE and weights["coverage"] > 0


def summary(onto: Any) -> Dict[str, Any]:
    """``{score, band, parts, depth, change7, change30}`` plus the weights, the dimension values, the denominators
    and the reasons for nulls. ``depth`` is ``{value, mass, scale, scales}`` (``scales``: whether it scales the
    score under these weights); ``change7`` and ``change30`` are ``{delta, at, text}`` or None."""
    core = _core(onto)
    weights = _weights(onto)
    total, name = score(dict(core["values"], depth=core["depth"]["value"]), weights)
    points = _history(onto)
    missing = dict(core["missing"])
    if total is None:
        missing["richness"] = "no part could be measured"
    depth = dict(core["depth"], scales=_scales(weights) and core["depth"]["value"] is not None
                 and core["values"].get("coverage") is not None)
    return {
        "score": total,
        "band": name,
        "parts": dict(core["values"]),
        "depth": depth,
        "weights": {k: (int(v) if float(v).is_integer() else v) for k, v in weights.items()},
        "dimensions": dict(core["dimensions"]),
        "closed": list(core["closed"]),
        "denominators": dict(core["denominators"]),
        "missing": dict(sorted(missing.items())),
        "change7": _change(points, total, 7),
        "change30": _change(points, total, 30),
        "formula": FORMULA if _scales(weights) else PLAIN_FORMULA,
    }


# gaps ----------------------------------------------------------------------------------------------------------
def _why(gap: Dict[str, Any]) -> str:
    t = gap.get("type")
    note = str(gap.get("note") or "")
    field = str(gap.get("field") or "")
    if t == "missing_field":
        return "no %s" % field
    if t == "missing_relation":
        return "%s(%s): %s" % (gap.get("rel"), gap.get("dir") or "out", note or "missing")
    if t == "orphan":
        return "no links"
    if t == "thin":
        return "no summary and no attrs"
    if t == "unconfirmed_hub":
        return "draft with %s" % (note or "many links")
    if t == "no_provenance":
        return "draft without a source"
    if t == "single_source":
        return "one source: %s" % note
    if t == "low_confidence":
        return note or "low confidence"
    if t == "draft":
        return "not reviewed yet"
    if t == "stale_source":
        return "cites stale %s" % note
    if t == "contradiction":
        return "contradicts %s" % note
    if t == "conflict":
        return "%s: %s" % (field, note)
    if t == "dangling_bridge":
        return "%s to %s" % (gap.get("rel"), note)
    if t == "open_question":
        return note or field or "open"
    return note or field or str(t)


def _node_items(onto: Any, nid: str, need: Dict[str, Any]) -> List[Dict[str, Any]]:
    rec = onto.node(nid)
    flags = render.flags(rec)
    out = []
    for gap in need.get("gaps") or []:
        gtype = str(gap.get("type"))
        item: Dict[str, Any] = {"type": gtype, "severity": int(gap.get("severity") or 0), "node": nid,
                                "kind": onto.kind_of(nid)}
        for key in ("field", "rel", "dir", "note", "owner", "edge"):
            if gap.get(key) not in (None, ""):
                item[key] = gap[key]
        if gap.get("read_only"):
            item["read_only"] = True  # a bridge an import brings: onto gaps lists it, the interview never asks it
        if gap.get("held"):
            item["held"] = True
        item["why"] = _why(gap)
        item["ask"] = str(gap.get("ask") or "")
        item["action"] = needs_mod.gap_action(gtype) or ""
        if gap.get("read_only"):
            item["action"] = "keep the pin %s was released with, or have %s re-point it and release again" % (
                gap.get("owner"), gap.get("owner"))
        item.update(flags)
        item["_degree"] = int(need.get("degree") or 0)
        out.append(item)
    return out


bridge_end_gaps = needs_mod.bridge_end_gaps  # one rule for gaps, brief and next (stage 8)


def _bridge_end_items(onto: Any, listed: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The gap items of ``bridge_end_gaps``. A ``same_as`` class disagrees once, so a conflict already listed for
    another member of the class (a local node, or an earlier imported end) is not listed again."""
    seen = {(onto.same_as.get(g["node"], g["node"]), g.get("field")) for g in listed
            if g.get("type") == "conflict" and g.get("node")}
    out = []
    for nid, need in bridge_end_gaps(onto):
        for item in _node_items(onto, nid, need):
            if item["type"] == "conflict":
                key = (onto.same_as.get(nid, nid), item.get("field"))
                if key in seen:
                    continue
                seen.add(key)
            out.append(item)
    return out


def _topic_item(gtype: str, **fields: Any) -> Dict[str, Any]:
    item: Dict[str, Any] = {"type": gtype, "severity": needs_mod.GAP_TYPES[gtype]["severity"]}
    for key, value in fields.items():
        if value is not None:
            item[key] = value
    item["action"] = needs_mod.gap_action(gtype) or ""
    item["_topic"] = True
    return item


def _question_stage(q: Dict[str, Any]) -> Optional[int]:
    stage = q.get("stage")
    return stage if isinstance(stage, int) and not isinstance(stage, bool) else None


def _side_name(onto: Any, ns: str) -> str:
    """How a side of an import pair reads to the user: ``self`` is this topic's title, an import its ns."""
    return onto.title() if ns == "self" else ns


def _pair_label(onto: Any, key: str) -> str:
    """``"kitchen|self"`` as ``"kitchen|<this ns>"`` (``self`` is an internal token)."""
    return "|".join((onto.ns or "this topic") if side == "self" else side for side in key.split("|"))


def _stage_question(onto: Any, dim: str, answers: Dict[str, str]) -> Optional[Dict[str, Any]]:
    done = {qid for qid, status in answers.items() if status in ("answered", "na")}
    found = [q for q in _bank(onto) if q.get("dimension") == dim and not q.get("for_gap") and q["id"] not in done
             and q.get("ask")]
    found.sort(key=lambda q: (int(q.get("stage") or 0), -int(q.get("priority") or 0), str(q["id"])))
    return found[0] if found else None


def _missing_dimensions(onto: Any, answers: Dict[str, str]) -> List[Dict[str, Any]]:
    threshold = _stage_done_at(onto)
    registry = onto.registry
    dims = registry.dimensions()
    out = []
    coverage = needs_mod.dimension_coverage(onto, closed_dimensions(onto), count_imports=True)
    for dim, value in sorted(coverage.items(), key=lambda kv: (registry.stage_of(kv[0]) or 0, kv[0])):
        if value >= threshold:
            continue
        decl = dims.get(dim) or {}
        target = int(decl.get("target") or 0)
        have = int(round(value * target))
        what = "nodes and cited sources" if dim == "data" else "substantive nodes"
        why = "%s: %d of %d %s (%d%%, done at %d%%)" % (dim, have, target, what, _half_up(value * 100),
                                                      _half_up(threshold * 100))
        q = _stage_question(onto, dim, answers)
        label = str(decl.get("label") or dim)
        ask = _fill(q["ask"], topic=onto.title()) if q else _fill(
            "What do we know about {label} in {topic}?", label=label.lower(), topic=onto.title())
        # the stage whose ``next`` call offers the question (a quick-start question sits in stage 0)
        stage = _question_stage(q) if q else None
        out.append(_topic_item("missing_dimension", dimension=dim,
                               stage=stage if stage is not None else registry.stage_of(dim), why=why, ask=ask,
                               question=q["id"] if q else None))
    if onto.imports:
        pairs = _bridge_pairs(onto)
        value = _bridges_value(pairs)
        if value is not None and value < threshold:
            weakest = sorted(pairs.items(), key=lambda kv: (kv[1], kv[0]))[0]
            a, b = weakest[0].split("|")
            why = "bridges: %s" % ", ".join("%s %d of %d" % (_pair_label(onto, k), min(n, BRIDGE_TARGET),
                                                             BRIDGE_TARGET) for k, n in sorted(pairs.items()))
            q = next((x for x in _bank(onto) if x["id"] == COMPOSE_QUESTION and x.get("ask")), None)
            if q is not None and answers.get(COMPOSE_QUESTION) not in ("answered", "na"):
                ask = _fill(q["ask"], topic=onto.title())
            else:
                q = None
                ask = _fill("Which outputs of {a} feed which processes of {b}?", a=_side_name(onto, a),
                            b=_side_name(onto, b))
            out.append(_topic_item("missing_dimension", dimension="bridges", stage=COMPOSE_STAGE, why=why, ask=ask,
                                   question=q["id"] if q else None))
    return out


def _unbridged_imports(onto: Any) -> List[Dict[str, Any]]:
    if not onto.imports:
        return []
    near, has_goal = _goal_reach(onto)
    bridged: Set[str] = set()
    for eid in sorted(onto.bridges):
        edge = onto.edges[eid]
        if not onto.active(eid) or edge.get("background"):
            continue
        for end in (edge.get("src"), edge.get("dst")):
            if isinstance(end, str):
                bridged.update(onto.members(end))
    by_kind: Dict[Tuple[str, str], List[str]] = {}
    for nid in sorted(onto.nodes):
        if nid in onto.virtual or onto.ns_of(nid) == "self" or not onto.active(nid):
            continue
        by_kind.setdefault((onto.ns_of(nid), onto.kind_of(nid)), []).append(nid)
    # namespaces with a node within reach of a goal; an import linked to no goal yet counts in full
    linked = {onto.ns_of(n) for n in near if n not in onto.virtual}
    template = next((q["ask"] for q in _bank(onto) if q.get("for_gap") == "unbridged_import" and q.get("ask")),
                    "How do {name} connect to the rest of {topic}?")
    out = []
    for (ns, kind), members in sorted(by_kind.items()):
        if kind == "source" or onto.registry.is_hub(kind):
            continue
        reach = has_goal and ns in linked
        related = [n for n in members if n in near] if reach else list(members)
        if not related or any(n in bridged for n in members):
            continue
        decl = onto.registry.kind(kind) or {}
        plural = str(decl.get("plural") or (_bare(kind) + "s"))
        name = "the %s %s (such as %s)" % (ns, plural, related[0])
        noun = plural if len(members) != 1 else _bare(kind)
        if _bare(kind) == "goal":
            count = "%d %s" % (len(members), noun)
        elif reach:
            count = "%d %s near a goal" % (len(related), plural if len(related) != 1 else _bare(kind))
            if len(related) != len(members):
                count = "%d of %d %s near a goal" % (len(related), len(members), noun)
        elif has_goal:
            count = "%d %s (no %s node is within %d links of a goal yet)" % (len(members), noun, ns, GOAL_HOPS)
        else:
            count = "%d %s (no goal yet)" % (len(members), noun)
        why = "%s has %s, none bridged" % (ns, count)
        out.append(_topic_item("unbridged_import", ns=ns, kind=kind, example=related[0], stage=COMPOSE_STAGE,
                               why=why, ask=_fill(template, name=name, topic=onto.title())))
    return out


def _open_proposals(onto: Any) -> List[Dict[str, Any]]:
    if onto.repo is None:
        return []
    cached = onto._cache.get("richness_open")
    stamp = _proposal_stamp(onto.repo)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    found = pipeline.pending(onto.repo)
    onto._cache["richness_open"] = (stamp, found)
    return found


def _uncited_sources(onto: Any) -> List[Dict[str, Any]]:
    cited = set(needs_mod.cited_sources(onto))
    in_flight: Set[str] = set()
    for prop in _open_proposals(onto):
        if isinstance(prop.get("source"), str):
            in_flight.add(prop["source"])
        for op in prop.get("ops") or []:
            for p in (op.get("prov") if isinstance(op, dict) else None) or []:
                if isinstance(p, dict) and isinstance(p.get("src"), str):
                    in_flight.add(p["src"])
    superseded = {str(e.get("supersedes")) for e in onto.sources.values() if e.get("supersedes")}
    out = []
    for sid in sorted(onto.sources):
        entry = onto.sources[sid]
        if entry.get("erased") or sid in cited or sid in in_flight or sid in superseded:
            continue
        why = "%s (%s, %s lines, captured %s) is cited by no record and no open proposal" % (
            sid, entry.get("kind"), entry.get("lines"), str(entry.get("captured_at") or "")[:10])
        item = _topic_item("uncited_source", node=sid, kind="source", why=why, ask="Extract from %s?" % sid)
        if entry.get("trust") == "untrusted":
            item["untrusted"] = True
        out.append(item)
    return out


def duplicates_listed(onto: Any, asked: bool = False) -> bool:
    """Whether the ranked list includes duplicate pairs: always when asked for (``type=duplicate`` or ``node``),
    otherwise only up to ``DUPLICATE_MAX_NODES`` active local nodes, since the pair search grows with the topic."""
    return asked or len(onto.local_nodes(active_only=True)) <= DUPLICATE_MAX_NODES


def _duplicates(onto: Any) -> List[Dict[str, Any]]:
    """Every likely duplicate pair (no cap: the result is paged by the caller, and its total must be true)."""
    cached = onto._cache.get("richness_duplicates")
    if cached is None:
        cached = entities.duplicates(onto, limit=0)
        onto._cache["richness_duplicates"] = cached
    out = []
    for pair in cached:
        a, b = pair["a"], pair["b"]
        rec_a, rec_b = onto.node(a), onto.node(b)
        why = "same as %s: %s (score %.2f)" % (render.mark(render.flags(rec_b), b), pair.get("why") or "similar",
                                                float(pair.get("score") or 0))
        item = _topic_item("duplicate", node=a, other=b, kind=onto.kind_of(a), score=_round(pair.get("score"), 2),
                           why=why, ask="Are %s and %s the same? If so, merge one into the other." % (a, b))
        item.update(render.flags(rec_a))
        out.append(item)
    return out


def _backlog(onto: Any) -> List[Dict[str, Any]]:
    items = _open_proposals(onto)
    if not items:
        return []
    policy = _policy(onto)
    ages = [pipeline.age_days(p) for p in items]
    limit = policy.get("max_pending")
    over = isinstance(limit, int) and not isinstance(limit, bool) and len(items) > limit
    why = "%d open proposal%s, oldest %d day%s%s" % (len(items), "" if len(items) == 1 else "s", max(ages),
                                                   "" if max(ages) == 1 else "s", " (over max_pending)" if over else "")
    ask = "Review %d pending proposal%s?" % (len(items), "" if len(items) == 1 else "s")
    return [_topic_item("pending_backlog", why=why, ask=ask, count=len(items))]


TOPIC_TYPES = ("missing_dimension", "unbridged_import", "uncited_source", "duplicate", "pending_backlog")


def _topic_gaps(onto: Any, types: Optional[Iterable[str]] = None,
                duplicates_asked: bool = False) -> List[Dict[str, Any]]:
    wanted = set(types) if types is not None else set(TOPIC_TYPES)
    out: List[Dict[str, Any]] = []
    if "missing_dimension" in wanted:
        out += _missing_dimensions(onto, _latest_answers(onto))
    if "unbridged_import" in wanted:
        out += _unbridged_imports(onto)
    if "uncited_source" in wanted:
        out += _uncited_sources(onto)
    if "duplicate" in wanted and duplicates_listed(onto, asked=duplicates_asked or types is not None):
        out += _duplicates(onto)
    if "pending_backlog" in wanted:
        out += _backlog(onto)
    for n, item in enumerate(out):
        item["_order"] = n
    return out


def _rank(onto: Any, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    near, _has_goal = _goal_reach(onto)

    def key(g: Dict[str, Any]) -> Tuple[Any, ...]:
        topic = bool(g.get("_topic"))
        return (-int(g.get("severity") or 0), 0 if topic else 1, int(g.get("_order") or 0),
                0 if g.get("node") in near else 1, -int(g.get("_degree") or 0), str(g.get("type")),
                str(g.get("node") or ""), str(g.get("field") or g.get("rel") or ""), str(g.get("note") or ""))

    ordered = sorted(items, key=key)
    return [{k: v for k, v in g.items() if not k.startswith("_")} for g in ordered]


def topic_gaps(onto: Any) -> List[Dict[str, Any]]:
    """The topic-level gaps, ranked: ``[{type, severity, node?, kind?, why, ask, action, ...}]``."""
    return _rank(onto, _topic_gaps(onto))


def _matches_kind(onto: Any, item: Dict[str, Any], kind: str) -> bool:
    if item.get("kind") == kind:
        return True
    if item.get("type") == "missing_dimension":
        return kind in onto.registry.kinds_of_dimension(str(item.get("dimension")))
    return False


def ranked_gaps(onto: Any, kind: Optional[str] = None, node: Optional[str] = None,
                type: Optional[str] = None) -> List[Dict[str, Any]]:
    """Topic and node gaps, filtered by kind (a registry kind key), node (a resolved id: its own gaps and the topic
    gaps that name it, duplicate pairs included at any topic size) and gap type, ranked (see the module
    docstring). Unfiltered by node, the node gaps are those of the active local nodes plus ``bridge_end_gaps``; the
    read-only ``inherited_bridge_gaps`` are listed too."""
    gap_type = type
    topic_types: Optional[List[str]] = None
    if gap_type:
        topic_types = [gap_type] if gap_type in TOPIC_TYPES else []
    items = _topic_gaps(onto, topic_types, duplicates_asked=bool(node)) if topic_types != [] else []
    if gap_type is None or gap_type not in TOPIC_TYPES:
        all_needs = needs_mod.all_needs(onto)
        if node:
            if node in all_needs:
                items += _node_items(onto, node, all_needs[node])
            elif node in onto.nodes and node not in onto.virtual and onto.active(node):
                items += _node_items(onto, node, needs_mod.needs(onto, node))
        else:
            for nid in sorted(all_needs):
                items += _node_items(onto, nid, all_needs[nid])
            items += _bridge_end_items(onto, items)
            for nid, need in needs_mod.inherited_bridge_gaps(onto):
                items += _node_items(onto, nid, need)
    if node:
        items = [g for g in items if g.get("node") == node or g.get("other") == node]
    if kind:
        items = [g for g in items if _matches_kind(onto, g, kind)]
    if gap_type:
        items = [g for g in items if g.get("type") == gap_type]
    return _rank(onto, items)


# source suggestions --------------------------------------------------------------------------------------------
def _gap_key(gap: Dict[str, Any]) -> str:
    parts = [str(gap.get("type"))]
    for key in ("node", "dimension", "ns", "kind"):
        if gap.get(key) and not (key == "kind" and gap.get("node")):
            parts.append(str(gap[key]))
    detail = gap.get("field") or ("%s(%s)" % (gap["rel"], gap.get("dir") or "out") if gap.get("rel") else None)
    if detail:
        parts.append(str(detail))
    return " ".join(parts)


def _phrases(texts: Iterable[Any]) -> List[str]:
    out = []
    for text in texts:
        if not isinstance(text, str) or idmod.LOCAL_RE.match(text.strip()) or idmod.QUAL_RE.match(text.strip()):
            continue
        key = util.name_key(text)
        if len(key) >= MIN_PHRASE and key not in out:
            out.append(key)
    return out


def _hints(onto: Any, gap: Dict[str, Any]) -> List[str]:
    """Words that make a line about the node more likely to close this gap."""
    words: List[str] = []
    if gap.get("type") == "missing_field" and gap.get("field"):
        words.append(str(gap["field"]).split(".", 1)[-1].replace("_", " "))
    if gap.get("type") == "missing_relation" and gap.get("rel"):
        rel = str(gap["rel"])
        words += [rel.replace("_", " "), onto.registry.inverse(rel).replace("_", " ")]
        decl = onto.registry.relation(rel) or {}
        side = decl.get("from") if gap.get("dir") == "in" else decl.get("to")
        if isinstance(side, list):
            for k in side:
                kdecl = onto.registry.kind(k) or {}
                words += [str(k), str(kdecl.get("plural") or "")]
    return [w for w in (util.name_key(x) for x in words) if len(w) >= MIN_PHRASE]


def _source_lines(onto: Any) -> List[Tuple[str, List[str]]]:
    """``[(source id, [name key of each line])]`` for every stored, non-erased source, cached on the graph."""
    cached = onto._cache.get("richness_source_lines")
    if cached is not None:
        return cached
    out: List[Tuple[str, List[str]]] = []
    if onto.repo is not None:
        for sid in sorted(onto.sources):
            if onto.sources[sid].get("erased"):
                continue
            try:
                text = sources_mod.read(onto.repo, sid)
            except (NotFound, DataError, OSError):
                continue
            out.append((sid, [" %s " % util.name_key(line) for line in text.split("\n")]))
    onto._cache["richness_source_lines"] = out
    return out


def suggest_sources(onto: Any, gaps: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Source lines that may close each gap: ``[{gap, src, loc, node?, type, matched}]``, at most three per gap.

    A node gap is matched against the stored source text: a line that names the node (its name or an alias, as a
    whole phrase) scores 1, and 2 when it also holds a hint word (the missing field, the relation, its inverse or
    the kinds on the other side). ``single_source`` and ``no_provenance`` skip sources the node already cites. An
    ``uncited_source`` gap points at the source's first chunk. The text itself is never returned (read it with the
    ``get`` call on the ``loc``); ``stale_source`` and ``dangling_bridge`` get no suggestion, since the fix is a
    refresh or a review."""
    out: List[Dict[str, Any]] = []
    lines = None
    for gap in list(gaps)[:SUGGEST_MAX_GAPS]:
        gtype = str(gap.get("type"))
        key = _gap_key(gap)
        if gtype == "uncited_source" and gap.get("node") and onto.repo is not None:
            try:
                chunks = sources_mod.chunk(onto.repo, str(gap["node"]), 1)
            except (NotFound, UsageError, DataError, OSError):
                continue
            out.append({"gap": key, "type": gtype, "src": gap["node"], "loc": chunks[0]["loc"],
                        "matched": "first chunk, not cited yet"})
            continue
        nid = gap.get("node")
        if (needs_mod.GAP_TYPES.get(gtype) or {}).get("scope") == "topic":
            continue
        if not nid or gtype in ("stale_source", "dangling_bridge") or nid in onto.virtual:
            continue
        rec = onto.node(str(nid)) or {}
        phrases = _phrases([rec.get("name")] + list(rec.get("aliases") or []))
        if not phrases:
            continue
        hints = _hints(onto, gap)
        skip: Set[str] = set()
        if gtype in ("single_source", "no_provenance"):
            skip = {str(p.get("src")) for p in rec.get("prov") or [] if isinstance(p, dict)}
        if lines is None:
            lines = _source_lines(onto)
        found = []
        for sid, keyed in lines:
            if sid in skip:
                continue
            best = None
            for n, line in enumerate(keyed, start=1):
                if not any(" %s " % p in line for p in phrases):
                    continue
                hit = next((h for h in hints if " %s " % h in line), None)
                rank = 2 if hit else 1
                if best is None or rank > best[0]:
                    best = (rank, n, hit)
                if rank == 2:
                    break
            if best is not None:
                found.append((-best[0], sid, best[1], best[2]))
        for neg, sid, n, hit in sorted(found)[:SUGGEST_PER_GAP]:
            out.append({"gap": key, "type": gtype, "node": nid, "src": sid, "loc": "L%d-L%d" % (n, n),
                        "matched": "name and %s" % hit if hit else "name"})
    return out


# calibration ---------------------------------------------------------------------------------------------------
def _proposal_files(repo: store.Repo) -> List[str]:
    return [path for folder in (pipeline.PENDING, pipeline.DONE)
            for path in sorted(glob.glob(os.path.join(repo.path(folder), "prop-*.json")))]


def _proposal_stamp(repo: store.Repo) -> Tuple[Any, ...]:
    return tuple((path, _stat(path)) for path in _proposal_files(repo))


_CONF_OPS: List[Set[str]] = []


def conf_ops() -> Set[str]:
    """The op types whose schema (``records.schema.json`` ``op_<type>``) declares ``conf``: they forecast the
    default 0.7 when the op leaves it out (C.12)."""
    if not _CONF_OPS:
        defs = records.schema().get("$defs") or {}
        _CONF_OPS.append({name[3:] for name, body in defs.items() if name.startswith("op_") and isinstance(body, dict)
                          and "conf" in (body.get("properties") or {})})
    return set(_CONF_OPS[0])


def _forecast(op: Dict[str, Any]) -> Optional[float]:
    conf = op.get("conf")
    if isinstance(conf, (int, float)) and not isinstance(conf, bool) and 0 <= conf <= 1:
        return float(conf)
    if op.get("op") in conf_ops():
        return DEFAULT_CONF
    return None


def _op_kind(op: Dict[str, Any]) -> str:
    name = str(op.get("op") or "op")
    if name == "add_node":
        kind = (op.get("node") or {}).get("kind") if isinstance(op.get("node"), dict) else None
        return str(kind) if kind else "node"
    if name == "update_node" and isinstance(op.get("id"), str):
        local = idmod.split_ns(op["id"])[1] if "/" in op["id"] or ":" in op["id"] else op["id"]
        return local.split(":", 1)[0] if ":" in local else "node"
    if name in ("add_edge", "update_edge"):
        return "edge"
    return name


def _stats(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    counts = {v: sum(1 for r in rows if r["verdict"] == v) for v in ("accept", "edit", "draft", "reject")}
    return {
        "n": n,
        "brier": round(sum((r["conf"] - r["outcome"]) ** 2 for r in rows) / n, 4) if n else None,
        "mean_conf": round(sum(r["conf"] for r in rows) / n, 4) if n else None,
        "score": round(sum(r["outcome"] for r in rows) / n, 4) if n else None,
        "verdicts": counts,
    }


def _reviewed_rows(prop: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``[{kind, conf, outcome, verdict}]`` for the ops of one proposal that have a verdict and a forecast."""
    review = prop.get("review")
    verdicts = review.get("verdicts") if isinstance(review, dict) else None
    if not isinstance(verdicts, dict):
        return []
    rows = []
    for i, op in enumerate(prop.get("ops") or [], start=1):
        if not isinstance(op, dict):
            continue
        verdict = verdicts.get(str(op.get("n") or i))
        conf = _forecast(op)
        if verdict not in OUTCOMES or conf is None:
            continue
        rows.append({"kind": _op_kind(op), "conf": conf, "outcome": OUTCOMES[verdict], "verdict": verdict})
    return rows


def _stored_rows(repo: store.Repo) -> Dict[str, List[Dict[str, Any]]]:
    """``{proposal id: reviewed rows}`` over ``proposals/pending`` and ``proposals/done``, cached by the files'
    sizes and times."""
    stamp = _proposal_stamp(repo)
    key = os.path.realpath(repo.root)
    hit = _CAL_CACHE.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    by_prop: Dict[str, List[Dict[str, Any]]] = {}
    for path in _proposal_files(repo):
        try:
            prop = store.read_json(path)
        except (DataError, OSError, ValueError):
            continue
        if not isinstance(prop, dict) or not isinstance(prop.get("id"), str) or prop["id"] in by_prop:
            continue
        by_prop[prop["id"]] = _reviewed_rows(prop)
    _CAL_CACHE[key] = (stamp, by_prop)
    return by_prop


def calibration(repo: store.Repo, in_flight: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """``{brier, by_kind, n, mean_conf, score, verdicts, scoring}`` over every reviewed proposal op that carries a
    confidence. ``by_kind`` groups by the node kind of node ops and ``edge`` for edge ops. ``in_flight`` (a
    proposal with its final ``review``) counts in place of the stored copy with the same id."""
    by_prop = dict(_stored_rows(repo))
    pid = _in_flight_id(in_flight)
    if pid is not None:
        by_prop[pid] = _reviewed_rows(in_flight or {})
    rows = [row for prop_id in sorted(by_prop) for row in by_prop[prop_id]]
    result = _stats(rows)
    kinds = sorted({r["kind"] for r in rows})
    result["by_kind"] = {k: _stats([r for r in rows if r["kind"] == k]) for k in kinds}
    result["scoring"] = SCORING
    return json.loads(json.dumps(result))


# tools check ---------------------------------------------------------------------------------------------------
def _command_of(invoke: Any) -> Optional[str]:
    if not isinstance(invoke, str) or not invoke.strip():
        return None
    try:
        words = shlex.split(invoke)
    except ValueError:
        words = invoke.split()
    return words[0] if words else None


def check_tools(onto: Any) -> List[Dict[str, Any]]:
    """``[{id, ns, interface, status, note, path?}]`` for every active tool node: a ``cli`` tool is looked up on
    PATH (``shutil.which``, never run); every other interface is ``unchecked``."""
    out = []
    for nid in sorted(n for n in onto.nodes if n not in onto.virtual and onto.active(n)
                      and _bare(onto.kind_of(n)) == "tool"):
        rec = onto.node(nid) or {}
        attrs = rec.get("attrs") or {}
        interface = attrs.get("interface") if isinstance(attrs.get("interface"), str) else None
        item: Dict[str, Any] = {"id": nid, "ns": onto.ns_of(nid), "interface": interface}
        if interface == "cli":
            command = _command_of(attrs.get("invoke"))
            if command is None:
                item.update(status="unchecked", note="no invoke command to look up")
            elif not _COMMAND_RE.match(command):
                item.update(status="unchecked", note="invoke does not start with a plain command name")
            else:
                found = shutil.which(command)
                if found:
                    item.update(status="ok", note="%s is on PATH" % command, path=found)
                else:
                    item.update(status="missing", note="%s is not on PATH" % command)
        elif interface:
            item.update(status="unchecked", note="%s tools are not checked" % interface)
        else:
            item.update(status="unchecked", note="no interface set")
        item.update(render.flags(rec))
        out.append(item)
    return out


# commands ------------------------------------------------------------------------------------------------------
def _kind_arg(onto: Any, text: Any) -> Optional[str]:
    if not text:
        return None
    if text == "source":
        return "source"
    key = onto.registry.kind_key(str(text)) or onto.registry.plural_alias(str(text))
    if key is None:
        raise UsageError("unknown kind %r; kinds: %s" % (text, ", ".join(onto.registry.kinds())))
    return key


def _node_arg(onto: Any, text: Any) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    if not text:
        return None, None
    query = str(text).strip()
    found = onto.resolve(query)
    nid = found.get("id") or onto.require(query)
    note = None
    if nid != query:
        note = {"query": query, "id": nid, "also": list(found.get("candidates") or [])[:5]}
    return nid, note


def _small_summary(full: Dict[str, Any]) -> Dict[str, Any]:
    return {k: full.get(k) for k in ("score", "band", "parts", "depth", "change7", "change30")}


def _compact_point(p: Dict[str, Any]) -> Dict[str, Any]:
    values = p.get("values") or {}
    out = {"at": p.get("at"), "kind": p.get("kind"), "label": p.get("label")}
    for key in ("richness",) + PARTS + ("depth", "brier"):
        out[key] = values.get(key)
    return out


def offered(ctx: Any, name: str) -> bool:
    """Whether the caller can make a ``name`` call: every command on the CLI; over MCP only the tools of the
    server's active profile (the read-only ``query`` profile has no ``next`` and no ``review``)."""
    if not getattr(ctx, "mcp", False):
        return True
    if hasattr(ctx, "available"):
        return bool(ctx.available(name))  # one profile check, shared with the status Next line
    profile = str(getattr(ctx, "profile", None) or "full")
    return any(c.name == name for c in commands_mod.tools(profile))


def _follow_up(ctx: Any, gap: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """``(call, action)`` for one gap in the caller's words. The call is the one that closes the gap when the caller
    has it (``offered``). Without it (the query profile has no ``next`` and no ``review``), the action is reworded
    from ``READ_ONLY_ACTIONS`` and the call is a read that helps, or None. ``action`` None keeps the gap's own."""
    t = str(gap.get("type"))
    if t == "uncited_source":
        return ctx.call("get", id=gap.get("node"), chunk=1), None
    closer = {"missing_dimension": "next", "unbridged_import": "next", "pending_backlog": "review"}.get(t)
    if closer is None:
        return None, None
    if not offered(ctx, closer):
        if t == "unbridged_import" and gap.get("example"):
            return ctx.call("brief", subject=gap["example"]), READ_ONLY_ACTIONS[t]
        return None, READ_ONLY_ACTIONS[t]
    if t == "missing_dimension":
        return (ctx.call("next", stage=gap["stage"]) if gap.get("stage") is not None else None), None
    if t == "unbridged_import":
        return ctx.call("next", stage=COMPOSE_STAGE), None
    return ctx.call("review"), None


def _as_int(value: Any, default: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _history_page(points: Sequence[Dict[str, Any]], limit: Any,
                  offset: Any) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """One page of the history, counted back from the newest point: ``offset`` skips the newest points and
    ``limit`` (0 for no cap) takes the ones before them. The page is returned oldest first, with a paging object
    shaped like the one ``dispatch`` adds to paged lists (``next_offset`` reaches older points)."""
    limit = _as_int(limit, 20)
    offset = max(0, _as_int(offset, 0))
    total = len(points)
    end = max(0, total - offset)
    start = max(0, end - limit) if limit > 0 else 0
    shown = list(points[start:end])
    return shown, {"key": "history", "offset": offset, "limit": limit, "more": start > 0, "remaining": start,
                   "next_offset": offset + len(shown) if start > 0 else None, "order": "newest first"}


def cmd_gaps(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """``gaps`` (and ``richness``, which presets ``section=summary``)."""
    section = args.get("section") or "gaps"
    if section not in SECTIONS:
        raise UsageError("section must be one of %s" % ", ".join(SECTIONS))
    onto = ctx.onto()
    full = summary(onto)
    result: Dict[str, Any] = {"section": section, "summary": full if section == "summary" else _small_summary(full)}
    if section == "gaps":
        gap_type = args.get("type") or None
        if gap_type and gap_type not in needs_mod.GAP_TYPES:
            raise UsageError("type must be one of %s" % ", ".join(sorted(needs_mod.GAP_TYPES)))
        kind = _kind_arg(onto, args.get("kind"))
        node, resolved = _node_arg(onto, args.get("node"))
        gaps = ranked_gaps(onto, kind=kind, node=node, type=gap_type)
        read_only = False
        for gap in gaps:
            call, action = _follow_up(ctx, gap)
            if call:
                gap["call"] = call
            if action:
                gap["action"] = action
                read_only = True
        result["gaps"] = gaps
        result["filters"] = {k: v for k, v in (("kind", kind), ("node", node), ("type", gap_type)) if v}
        notes = []
        if gap_type is None and not node and not duplicates_listed(onto):
            notes.append("duplicate pairs are listed only up to %d nodes here: %s" % (
                DUPLICATE_MAX_NODES, ctx.call("gaps", kind=kind, type="duplicate")))
        if read_only:
            notes.append(READ_ONLY_NOTE)
        if notes:
            result["notes"] = notes
        if resolved:
            result["resolved"] = resolved
        if args.get("suggest_sources"):
            limit = args.get("limit")
            offset = max(0, int(args.get("offset") or 0))
            size = int(limit) if isinstance(limit, int) and limit > 0 else SUGGEST_MAX_GAPS
            found = suggest_sources(onto, gaps[offset: offset + size])
            for s in found:
                a, _sep, b = str(s["loc"]).partition("-")
                s["call"] = ctx.call("get", id=s["src"], lines="%s-%s" % (a.lstrip("L"), b.lstrip("L")))
            result["suggestions"] = found
    elif section == "history":
        points = history.read(ctx.repo)
        shown, paging = _history_page(points, args.get("limit"), args.get("offset"))
        result["history"] = [_compact_point(p) for p in shown]
        result["totals"] = {"history": len(points)}
        result["paging"] = paging
    elif section == "calibration":
        result["calibration"] = calibration(ctx.repo)
    return result


def _num(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return render.fmt(value)
    return "%.*f" % (digits, float(value))


def _head(summ: Dict[str, Any]) -> str:
    if summ.get("score") is None:
        return "richness n/a"
    text = "richness %s %s" % (summ.get("score"), summ.get("band") or "")
    change = summ.get("change7") or summ.get("change30")
    if isinstance(change, dict) and change.get("text"):
        text += " (%s)" % change["text"]
    return text.strip()


def _parts_line(summ: Dict[str, Any], weights: bool = False) -> str:
    parts = summ.get("parts") or {}
    w = summ.get("weights") or {}
    out = []
    for key in PARTS:
        text = "%s %s" % (key, _num(parts.get(key)))
        if weights and key in w:
            text += " (w %s)" % w[key]
        out.append(text)
    return ", ".join(out)


def _depth_line(summ: Dict[str, Any], mode: str) -> Optional[str]:
    """``depth 0.59: evidence sum 25.50 of target sum 20 (it scales the other parts with coverage)``; None when the
    summary has no depth."""
    depth = summ.get("depth")
    if not isinstance(depth, dict) or depth.get("value") is None:
        return None
    text = "depth %s: evidence sum %s of target sum %s" % (_num(depth.get("value")), _num(depth.get("mass")),
                                                          render.fmt(depth.get("scale")))
    text += (" (it scales the other parts with coverage)" if depth.get("scales")
             else " (it scales nothing under these weights)")
    if mode == "text":
        text += "; " + DEPTH_RULE
    return text


def _render_summary(summ: Dict[str, Any], mode: str) -> List[str]:
    lines = ["%s: %s" % (_head(summ), _parts_line(summ, weights=mode == "text"))]
    depth = _depth_line(summ, mode)
    if depth:
        lines.append(depth)
    dims = summ.get("dimensions") or {}
    if dims:
        lines.append("dimensions: " + ", ".join("%s %s" % (k, _num(v)) for k, v in dims.items()))
    if summ.get("closed"):
        lines.append("closed as n/a: " + ", ".join(summ["closed"]))
    for key, reason in sorted((summ.get("missing") or {}).items()):
        lines.append("not measured: %s (%s)" % (key, reason))
    if mode == "text":
        den = summ.get("denominators") or {}
        lines.append("denominators: " + ", ".join("%s %s" % (k, render.fmt(v)) for k, v in sorted(den.items())))
        for label, key in (("7 days", "change7"), ("30 days", "change30")):
            change = summ.get(key)
            lines.append("%s: %s" % (label, change.get("text") if isinstance(change, dict) else "no earlier point"))
        lines.append(str(summ.get("formula") or FORMULA))
    lines.append(HEURISTIC)
    return lines


def _gap_subject(gap: Dict[str, Any]) -> str:
    if gap.get("node"):
        return render.mark(gap, str(gap["node"]))
    if gap.get("type") == "unbridged_import":
        return "%s/%s" % (gap.get("ns"), _bare(str(gap.get("kind"))))
    return ""


def _gap_line(gap: Dict[str, Any]) -> str:
    subject = _gap_subject(gap)
    head = "%s %s" % (gap.get("severity"), gap.get("type"))
    if subject:
        head += " " + subject
    if gap.get("why"):
        head += " " + str(gap["why"])
    if gap.get("held"):
        head += " (raised in the last %d days; the interview does not ask it back yet)" % needs_mod.RECENT_DAYS
    return "%s -> ask %s" % (head, json.dumps(str(gap.get("ask") or ""), ensure_ascii=False))


def render_gaps(result: Dict[str, Any], mode: str, ctx: Any) -> List[str]:
    section = result.get("section") or "gaps"
    summ = result.get("summary") or {}
    if section == "summary":
        return _render_summary(summ, mode)
    if section == "history":
        points = result.get("history") or []
        total = (result.get("totals") or {}).get("history", len(points))
        paging = result.get("paging") if isinstance(result.get("paging"), dict) else {}
        skipped = int(paging.get("offset") or 0)
        lines = ["history: %d of %d point%s%s, oldest first" % (
            len(points), total, "" if total == 1 else "s", " (before the newest %d)" % skipped if skipped else "")]
        if not points and total:
            lines.append("no points on this page")
        for p in points:
            lines.append("%s %s%s richness %s: %s" % (
                str(p.get("at") or "")[:16].replace("T", " "), p.get("kind"),
                " [%s]" % p["label"] if p.get("label") else "", _num(p.get("richness"), 0),
                ", ".join("%s %s" % (SHORT[k], _num(p.get(k))) for k in PARTS + ("depth",))))
        for label, key in (("7 days", "change7"), ("30 days", "change30")):
            change = summ.get(key)
            lines.append("%s: %s" % (label, change.get("text") if isinstance(change, dict) else "no earlier point"))
        if paging.get("more") and paging.get("next_offset") is not None:
            limit = paging.get("limit")
            follow = ctx.call("gaps", section="history", limit=limit if limit != 20 else None,
                              offset=paging["next_offset"])
            lines.append("[page] history %d-%d of %d, counted from the newest; next: %s" % (
                skipped + 1, skipped + len(points), total, follow))
        return lines
    if section == "calibration":
        cal = result.get("calibration") or {}
        if not cal.get("n"):
            return ["calibration: no reviewed op with a confidence yet (%s)" % SCORING]
        lines = ["calibration: brier %s over %d reviewed op%s (mean conf %s, mean outcome %s)" % (
            _num(cal.get("brier"), 4), cal["n"], "" if cal["n"] == 1 else "s", _num(cal.get("mean_conf")),
            _num(cal.get("score")))]
        for kind, s in (cal.get("by_kind") or {}).items():
            lines.append("  %s: n %d, brier %s, conf %s, outcome %s" % (kind, s["n"], _num(s.get("brier"), 4),
                                                                        _num(s.get("mean_conf")), _num(s.get("score"))))
        lines.append(SCORING if mode == "text" else "accept 1, edit or draft 0.5, reject 0; lower is better")
        return lines
    gaps = result.get("gaps") or []
    total = (result.get("totals") or {}).get("gaps", len(gaps))
    filters = result.get("filters") or {}
    head = "%d gap%s, most severe first" % (total, "" if total == 1 else "s")
    if filters:
        head += " (%s)" % ", ".join("%s %s" % kv for kv in sorted(filters.items()))
    lines = [head]
    resolved = result.get("resolved")
    if resolved:
        lines.append("resolved: %s -> %s" % (json.dumps(resolved.get("query"), ensure_ascii=False), resolved.get("id")))
    for note in result.get("notes") or []:
        lines.append("note: %s" % note)
    if not gaps:
        lines.append("no gaps" if not total else "no gaps on this page")
    by_gap: Dict[str, List[Dict[str, Any]]] = {}
    for s in result.get("suggestions") or []:
        by_gap.setdefault(str(s.get("gap")), []).append(s)
    for gap in gaps:
        lines.append(_gap_line(gap))
        if mode == "text":
            extra = "   action: %s" % gap.get("action")
            if gap.get("call"):
                extra += " (%s)" % gap["call"]
            lines.append(extra)
        found = by_gap.get(_gap_key(gap))
        if found:
            lines.append("   from: " + "; ".join("%s %s (%s)" % (s["src"], s["loc"], s["matched"]) for s in found))
    if result.get("suggestions"):
        first = result["suggestions"][0]
        lines.append("read a suggested line: %s" % first.get("call"))
    elif "suggestions" in result:
        lines.append("no source line names these nodes")
    if mode == "text":
        lines.append(HEURISTIC)
    return lines


def cmd_tools_check(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """``tools check``: look the tool nodes up and write ``.onto/tools-check.json`` (nothing else)."""
    action = args.get("action") or "check"
    if action != "check":
        raise UsageError("tools takes one action: check")
    tools = check_tools(ctx.onto())
    counts = {s: sum(1 for t in tools if t["status"] == s) for s in ("ok", "missing", "unchecked")}
    report = {"at": util.now_iso(), "kit": __version__, "tools": tools}
    path = ctx.repo.path(TOOLS_REPORT)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    store.write_json(path, report)
    return {"tools": tools, "counts": counts, "report": TOOLS_REPORT}


def render_tools_check(result: Dict[str, Any], mode: str, ctx: Any) -> List[str]:
    tools = result.get("tools") or []
    c = result.get("counts") or {}
    lines = ["tools: %d checked: %d ok, %d missing, %d unchecked (report %s)" % (
        len(tools), c.get("ok", 0), c.get("missing", 0), c.get("unchecked", 0), result.get("report"))]
    for t in tools:
        text = "%s %s %s (%s)" % (render.mark(t, str(t.get("id"))), t.get("interface") or "-", t.get("status"),
                                  t.get("note"))
        if mode == "text" and t.get("path"):
            text += " at %s" % t["path"]
        lines.append(text)
    if not tools:
        lines.append("no tool nodes yet")
    return lines
