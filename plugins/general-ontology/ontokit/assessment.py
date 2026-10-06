"""Calibration checks of the opt-in ``assessment`` pack (``onto pack add assessment``), run by ``validate``.

A ``risk`` is rated per dimension (the topic picks its dimensions; the interview suggests regulatory, financial,
reputational and customer) in ``attrs.ratings``, one item per measure: ``<dimension>.<measure>=<level>`` with the
measures ``impact``, ``inherent`` and ``residual`` and the levels ``very_low``, ``low``, ``moderate``, ``high`` and
``critical`` (``financial.impact=high``). Node attrs hold literals or lists of literals, and edges carry no attrs,
so this flat list is the shape the kit already stores, searches and shows. A ``control`` reduces a risk through a
``mitigates`` edge; its ``attrs.covers`` lists the dimensions it reduces (absent or empty: all of them).

For each active local risk and each dimension it rates:

- the inherent level is at most the impact, and the residual level at most the inherent one;
- a residual level below the inherent one needs a mitigating control (active, covering the dimension) whose
  ``status`` is ``implemented`` or ``partial``;
- a drop of two levels or more needs one that is ``implemented`` and ``automated``;
- a measure is rated once, and each of the three measures is rated (a dimension rated on impact alone has no
  inherent or residual level to check).

A reviewed or approved risk also rates at least one dimension. A draft risk with no ratings at all gets nothing: a
risk is named before it is rated (``q.risks.what`` comes before ``q.risks.ratings``). The topic's dimensions are
checked only through the ratings: the answer to ``q.risks.dimensions`` is free text in the interview log, and no
pack field or decision records them, so there is no list to compare against.

Ratings stay draft until a named owner reviews them: a risk whose ``status`` is ``reviewed`` or ``approved`` needs
an ``owner`` and a ``statement``; a draft one without a statement gets a W09 (a pack cannot mark a field required).
Archived controls and archived ``mitigates`` edges do not count. The findings of a draft risk (``status`` absent
or ``draft``) are warnings (W09), so the work in progress never blocks a build; once a risk is reviewed or approved
they are problems (P23): ``onto build`` and ``onto release`` refuse them, and a write that would add one (through
``mutate.apply_ops``, so review, answer --apply and the pipeline too) is refused, archiving a control a reviewed
risk relies on included. Nothing here writes: a wrong rating is fixed with an update, and a risk that no longer
applies is archived with a reason and a decision, never deleted.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .errors import Problem

PACK = "assessment"
LEVELS = ("very_low", "low", "moderate", "high", "critical")
MEASURES = ("impact", "inherent", "residual")
REVIEWED = ("reviewed", "approved")
IN_PLACE = ("implemented", "partial")
RATING_RE = re.compile(r"^([a-z][a-z0-9_-]{0,39})\.(impact|inherent|residual)=(%s)$" % "|".join(LEVELS))
DIMENSIONS_SUGGESTED = ("regulatory", "financial", "reputational", "customer")


def enabled(onto: Any) -> bool:
    """True when the topic lists the assessment pack."""
    return PACK in onto.registry.pack_names()


def parse_ratings(value: Any) -> Tuple[Dict[str, Dict[str, str]], List[str]]:
    """``({dimension: {measure: level}}, messages)`` from ``attrs.ratings``. A measure rated twice keeps its first
    level and adds a message; items that do not read as a rating are left to the field check (P08)."""
    out: Dict[str, Dict[str, str]] = {}
    notes: List[str] = []
    for item in value if isinstance(value, list) else []:
        m = RATING_RE.match(item) if isinstance(item, str) else None
        if m is None:
            continue
        dim, measure, level = m.groups()
        seen = out.setdefault(dim, {})
        if measure in seen:
            notes.append("%s: %s is rated twice (%s and %s); keep one" % (dim, measure, seen[measure], level))
            continue
        seen[measure] = level
    return out, notes


def _rank(level: Optional[str]) -> Optional[int]:
    return LEVELS.index(level) if level in LEVELS else None


def _word(level: str) -> str:
    return level.replace("_", " ")


def _named_controls(onto: Any, rid: str) -> List[Tuple[str, Dict[str, Any]]]:
    """``[(id, attrs)]`` of the active controls with an active ``mitigates`` edge to ``rid``."""
    out = []
    for e in onto.edges_of(rid, direction="in", rels=["mitigates"]):
        if e["edge"].get("rel") != "mitigates" or onto.kind_of(e["other"]) != "control":
            continue
        node = onto.node(e["other"]) or {}
        out.append((str(e["other"]), node.get("attrs") if isinstance(node.get("attrs"), dict) else {}))
    return out


def _controls(onto: Any, rid: str) -> List[Dict[str, Any]]:
    """The attrs of the active controls with an active ``mitigates`` edge to ``rid``."""
    return [attrs for _cid, attrs in _named_controls(onto, rid)]


def _left_out(dim: str, controls: List[Dict[str, Any]], names: Optional[List[str]], wanted: Any) -> str:
    """When controls ``wanted`` accepts mitigate the risk but their ``covers`` leaves ``dim`` out: the clause that
    names them and the fix (empty when there are none)."""
    found = [(names[i] if names and i < len(names) else "a control") for i, c in enumerate(controls)
             if wanted(c) and not _covers(c, dim)]
    if not found:
        return ""
    return ("; %s mitigate%s it, but %s covers leaves out %s (add %s to covers, or leave covers empty to cover "
            "every dimension)" % (", ".join(found), "s" if len(found) == 1 else "", "its" if len(found) == 1 else
                                  "their", dim, dim))


def _covers(attrs: Dict[str, Any], dim: str) -> bool:
    covers = attrs.get("covers")
    return not covers or (isinstance(covers, list) and dim in covers)


def _dimension_findings(dim: str, rated: Dict[str, str], controls: List[Dict[str, Any]],
                        names: Optional[List[str]] = None) -> List[str]:
    """The calibration messages of one dimension; ``names`` are the ids of ``controls``, in order, so a message
    can name a control whose ``covers`` leaves the dimension out."""
    impact, inherent, residual = (_rank(rated.get(m)) for m in MEASURES)
    out: List[str] = []
    missing = [m for m in MEASURES if m not in rated]
    if missing:
        out.append("%s: %s %s not rated (every rated dimension needs impact, inherent and residual)"
                   % (dim, " and ".join(missing), "is" if len(missing) == 1 else "are"))
    if impact is not None and inherent is not None and inherent > impact:
        out.append("%s: inherent %s is above impact %s (inherent must not exceed impact)"
                   % (dim, _word(rated["inherent"]), _word(rated["impact"])))
    if inherent is None or residual is None:
        return out
    if residual > inherent:
        out.append("%s: residual %s is above inherent %s (residual must not exceed inherent)"
                   % (dim, _word(rated["residual"]), _word(rated["inherent"])))
        return out
    covering = [c for c in controls if _covers(c, dim)]
    in_place = lambda c: c.get("status") in IN_PLACE  # noqa: E731
    automated = lambda c: c.get("status") == "implemented" and c.get("nature") == "automated"  # noqa: E731
    if residual < inherent and not [c for c in covering if in_place(c)]:
        left = _left_out(dim, controls, names, in_place)
        if left:
            out.append("%s: residual %s is below inherent %s, but no implemented or partial control that covers %s "
                       "mitigates it%s, or raise residual"
                       % (dim, _word(rated["residual"]), _word(rated["inherent"]), dim, left))
        else:
            out.append("%s: residual %s is below inherent %s, but no implemented or partial control mitigates it "
                       "(link one with mitigates, or raise residual)"
                       % (dim, _word(rated["residual"]), _word(rated["inherent"])))
    elif inherent - residual >= 2 and not [c for c in covering if automated(c)]:
        out.append("%s: residual %s is %d levels below inherent %s; a drop of two or more levels needs an "
                   "implemented, automated control%s" % (dim, _word(rated["residual"]), inherent - residual,
                                                        _word(rated["inherent"]),
                                                        _left_out(dim, controls, names, automated)))
    return out


def findings(onto: Any, rid: str) -> Tuple[bool, List[str]]:
    """``(reviewed, messages)`` for one risk: ``reviewed`` is True when its status is reviewed or approved."""
    node = onto.node(rid) or {}
    attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
    reviewed = attrs.get("status") in REVIEWED
    out: List[str] = []
    for field in ("owner", "statement") if reviewed else ("statement",):
        value = attrs.get(field)
        if not isinstance(value, str) or not value.strip():
            if reviewed:
                out.append("ratings are %s but the risk has no %s; ratings stay draft until a named owner reviews "
                           "them" % (attrs.get("status"), field))
            else:
                out.append("the risk has no statement (what could happen, and to what); every risk needs one")
    ratings, notes = parse_ratings(attrs.get("ratings"))
    if reviewed and not ratings:
        out.append("ratings are %s but the risk has no ratings; rate impact, inherent and residual on at least one "
                   "dimension (financial.impact=high)" % attrs.get("status"))
    out += notes
    named = _named_controls(onto, rid)
    controls, names = [a for _c, a in named], [c for c, _a in named]
    for dim in sorted(ratings):
        out += _dimension_findings(dim, ratings[dim], controls, names)
    return reviewed, out


def calibration(onto: Any, where: Any) -> Tuple[List[Problem], List[Problem]]:
    """``(problems, warnings)``: P23 for reviewed or approved risks, W09 for draft ones; empty when the pack is
    off. ``where(rid)`` gives the ``(file, line)`` of a record."""
    problems: List[Problem] = []
    warnings: List[Problem] = []
    if not enabled(onto) or onto.registry.kind_pack("risk") != PACK:
        return problems, warnings
    for rid in onto.local_nodes(active_only=True):
        if onto.kind_of(rid) != "risk":
            continue
        reviewed, messages = findings(onto, rid)
        file, line = where(rid)
        for message in messages:
            if reviewed:
                problems.append(Problem("P23", file, line, "%s: %s" % (rid, message)))
            else:
                warnings.append(Problem("W09", file, line, "%s: %s (draft)" % (rid, message)))
    return problems, warnings
