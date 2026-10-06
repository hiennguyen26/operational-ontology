"""Proposal commands: ``propose``, ``review``, ``apply`` and ``dupes``, with verdict parsing and the numbered preview.

The graph changes only through ``pipeline``; this module is the command surface around it.

- ``propose`` checks a proposal with ``pipeline.prepare`` and saves it as pending. A refusal is a result, not a
  bare error: it carries ``error: "refused"``, every problem, and the numbered preview, so the author can fix them
  all in one pass. Nothing is saved. The preview of a refusal shows only the ops a problem or warning names, and
  lists the problems of the ops it shows right after its head line (each op is tagged with its problem codes), so
  a long proposal cannot push them past a size cap. Both previews are paged by ``limit`` (``PROPOSE_LIMIT`` ops by
  default; 0 = all) and ``offset``: a saved proposal pages on with ``review``, a refused one with ``propose``.
- ``review`` lists the open proposals in ``pipeline.pending`` order (priority first); the compact text groups the
  page by source. With an id it shows the numbered preview of one proposal, its ops paged by ``limit`` and
  ``offset``; the last line names the call for the next page. A page carries only the entries of its own ops (new
  terms, impact ids, verdicts, edits, applied results, per-op checks, destructive ops) with the proposal's totals,
  so it grows with its ops. Over MCP the page also shrinks until it fits the size cap, measured in the form the
  call asked for (the compact text holds far more ops than the JSON); a page that one op already takes over the
  cap is cut with a plain note, since a smaller limit cannot help.
- ``apply`` records verdicts (``pipeline.review``) and commits (``pipeline.commit``). It first runs the commit as
  a preview, so a refusal or a data conflict writes nothing, not even the verdicts; a refusal that only the final
  graph check finds puts the proposal file back as it was. Over MCP it writes only with ``confirm=true``; without
  it the result is the preview. An applied proposal returns its stored record with ``note: "already applied"`` and
  needs no confirm, since nothing is written. A ``reason`` given with no verdicts records the recorded verdicts
  again with that reason. The whole call holds the write lock, so concurrent applies of one proposal apply it once
  and the others return ``already applied``.
- ``dupes`` lists likely duplicate pairs (``entities.duplicates``); ``--propose`` writes a pending proposal of
  ``merge`` ops for the pairs shown.

Verdicts (``parse_verdicts``): op numbers as ranges such as ``1-3,5``. Each op needs exactly one verdict. An op named
under two verdicts, an op number outside the proposal, or a range that runs backwards is a usage error. ``all``
gives its verdict to every op not named otherwise, so ``all=draft accept=1`` accepts op 1 and drafts the rest.

Rendering: quotes from sources that are not interview answers are marked ``[untrusted]``, and so is the heading of
each group of ops drawn from such a source (an op that cites no source belongs to the proposal's source). Ids of
existing records an op names carry their markers (``[untrusted]``, ``(draft)``, ``(archived)``). JSON results carry
the same marks: ``"untrusted": true`` on such provenance entries and ops, and ``marks`` {id: flags} on ops. Compact
text cuts free text at ``render.WIDTH`` and closes with the call that shows the whole text. Problem and warning
messages are never cut: they name what the kit accepts (kinds, relations, patterns), and a refusal has no saved
text to show in full.

A settled proposal (applied, rejected or superseded) shows as it ended (``settled_view``): the new terms its ops
created move from ``new_terms`` to ``created_terms`` (text ``created``; the rest read ``not added``), and its
destructive ops read as applied or not applied instead of asking for a yes. The stored file is not changed.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import commands, entities, ids, mutate, pipeline, records, render, store, util
from .errors import Conflict, Refused, UsageError

BULK = ("accept", "draft", "reject")
MAX_QUOTES = 3
MAX_MATCHES = 3
MAX_LIST = 12
REVIEW_LIMIT = 10  # ops per page of one proposal (the review command's default limit)
PROPOSE_LIMIT = REVIEW_LIMIT  # ops in the preview propose returns (a saved proposal pages on with review)
SETTLED = ("applied", "rejected", "superseded")  # a proposal that is decided: its preview reports, it asks nothing
FIT_CHARS = 18000  # an MCP review page of one proposal keeps its result (JSON or text) under this many characters
FIT_ATTEMPTS = 12
TRUST_RANK = {"user": 3, "reviewed": 2, "agent": 1, "untrusted": 0}
_TOKEN_RE = re.compile(r"^\s*([0-9]+)\s*(?:-\s*([0-9]+)\s*)?$")
Claims = Tuple[Dict[str, Any], Dict[str, Any]]  # ({new term: op n}, {impact id: op n}), from _claims


# verdicts ------------------------------------------------------------------------------------------------------
def parse_ops(spec: Any, n_ops: int, what: str = "ops") -> List[int]:
    """Op numbers from ``"1-3,5"`` (an int or a list of such strings also works), in the order given, each once.
    Numbers outside ``1..n_ops`` and ranges that run backwards are usage errors."""
    if spec is None or spec == "" or spec == [] or spec == ():
        return []
    if isinstance(spec, bool):
        raise UsageError("%s: expected op numbers such as 1-3,5, not %r" % (what, spec))
    if isinstance(spec, int):
        parts = [str(spec)]
    elif isinstance(spec, (set, frozenset)):
        parts = [str(p) for p in sorted(spec, key=str)]
    elif isinstance(spec, (list, tuple)):
        parts = [str(p) for p in spec]
    elif isinstance(spec, str):
        parts = [spec]
    else:
        raise UsageError("%s: expected op numbers such as 1-3,5, not %r" % (what, spec))
    out: List[int] = []
    for part in parts:
        for token in part.split(","):
            if not token.strip():
                continue
            m = _TOKEN_RE.match(token)
            if not m:
                raise UsageError("%s: %r is not an op number or a range such as 1-3" % (what, token.strip()))
            lo = int(m.group(1))
            hi = int(m.group(2)) if m.group(2) is not None else lo
            if hi < lo:
                raise UsageError("%s: the range %d-%d runs backwards; write %d-%d" % (what, lo, hi, hi, lo))
            for n in (lo, hi):
                if n < 1 or n > n_ops:
                    raise UsageError("%s: op %d does not exist (the proposal has %d op%s)"
                                     % (what, n, n_ops, "" if n_ops == 1 else "s"))
            for n in range(lo, hi + 1):
                if n not in out:
                    out.append(n)
    return out


def parse_verdicts(accept: Any, draft: Any, reject: Any, all_: Optional[str], n_ops: int,
                   edit: Any = None) -> Dict[str, str]:
    """``{"1": "accept", ...}`` for every op of a proposal with ``n_ops`` ops. ``accept``, ``draft`` and ``reject``
    take ranges such as ``1-3,5``; ``edit`` holds the op numbers that have a replacement op (a list, or the edits
    object itself); ``all_`` (accept, draft or reject) covers every op not named. Each op must end with exactly one
    verdict: two verdicts for one op, or an op with none, is a ``UsageError``."""
    if not isinstance(n_ops, int) or n_ops < 1:
        raise UsageError("the proposal has no ops to give verdicts to")
    if isinstance(edit, dict):
        edit = [str(k) for k in edit]
    out: Dict[str, str] = {}
    for verdict, spec in (("accept", accept), ("draft", draft), ("reject", reject), ("edit", edit)):
        for n in parse_ops(spec, n_ops, verdict):
            key = str(n)
            if key in out and out[key] != verdict:
                raise UsageError("op %d has two verdicts, %s and %s; each op needs exactly one"
                                 % (n, out[key], verdict))
            out[key] = verdict
    if all_ not in (None, ""):
        if all_ not in BULK:
            raise UsageError("all must be accept, draft or reject, not %r" % (all_,))
        for n in range(1, n_ops + 1):
            out.setdefault(str(n), str(all_))
    missing = [str(n) for n in range(1, n_ops + 1) if str(n) not in out]
    if missing:
        raise UsageError("every op needs exactly one verdict; missing: %s. Name them with accept, draft, reject or "
                         "edit, or give all for the rest" % ranges(int(m) for m in missing))
    return {k: out[k] for k in sorted(out, key=int)}


def ranges(nums: Iterable[int]) -> str:
    """``1-3,5`` for ``[1, 2, 3, 5]``."""
    ordered = sorted(set(int(n) for n in nums))
    parts: List[str] = []
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1] == ordered[j] + 1:
            j += 1
        parts.append(str(ordered[i]) if i == j else "%d-%d" % (ordered[i], ordered[j]))
        i = j + 1
    return ",".join(parts)


def verdict_text(verdicts: Dict[str, str]) -> str:
    """``accept 1-4; draft 5; reject 6``."""
    groups: Dict[str, List[int]] = {}
    for key, value in (verdicts or {}).items():
        try:
            groups.setdefault(str(value), []).append(int(key))
        except (TypeError, ValueError):
            continue
    order = ["accept", "edit", "draft", "reject"]
    return "; ".join("%s %s" % (v, ranges(groups[v])) for v in order + sorted(set(groups) - set(order)) if v in groups)


# the preview ---------------------------------------------------------------------------------------------------
class _Cutter(object):
    """Cuts free text to one line of ``width`` characters (0 = no cut) and counts the cuts."""

    def __init__(self, width: int) -> None:
        self.width = int(width or 0)
        self.count = 0

    def cut(self, text: Any) -> str:
        flat = render.plain(text)  # one safe line: no control or bidi characters, no line breaks
        if self.width and len(flat) > self.width:
            self.count += 1
            return render.trunc(flat, self.width)
        return flat

    def quoted(self, text: Any) -> str:
        return '"%s"' % self.cut(text)

    def value(self, value: Any) -> str:
        if isinstance(value, str):
            return self.quoted(value)
        return self.cut(render.fmt(value))


def _source_kind(src: str, onto: Any) -> Optional[str]:
    if onto is None:
        return None
    entry = (getattr(onto, "sources", None) or {}).get(src) or {}
    return entry.get("kind")


def _untrusted(src: str, onto: Any) -> bool:
    """A quote is untrusted unless the loaded data says its source holds interview answers."""
    return _source_kind(src, onto) != "interview"


def _source_untrusted(src: Optional[str], onto: Any) -> bool:
    """Whether a group of ops (or a proposal) drawn from ``src`` is marked ``[untrusted]``: a source that is not an
    interview; ops without a source and import data are not marked."""
    return bool(src) and not str(src).startswith("imp:") and _untrusted(str(src), onto)


def source_label(src: Optional[str], onto: Any = None) -> str:
    """The heading of a group of ops or proposals drawn from one source."""
    if not src:
        return "without a source"
    if src.startswith("imp:"):
        return "from %s (import)" % src
    kind = _source_kind(src, onto)
    if kind is None:
        return "from %s [untrusted]" % src
    return "from %s (%s)%s" % (src, kind, "" if kind == "interview" else " [untrusted]")


def _op_source(op: Dict[str, Any], fallback: Optional[str] = None) -> Optional[str]:
    """The first source an op cites; an op that cites none belongs to ``fallback`` (the proposal's source)."""
    for p in op.get("prov") or []:
        if isinstance(p, dict) and p.get("src"):
            return str(p["src"])
    return str(fallback) if fallback else None


def _ref(value: Any, onto: Any) -> str:
    """An id an op names, with the markers of the record it names now (``[untrusted] id (draft)``). ``$refs`` and
    ids that are not in the graph (new ones) print as they are."""
    text = render.plain(value)
    if onto is None or not isinstance(value, str) or not value or value.startswith("$"):
        return text
    rec = onto.record(value)
    return render.mark(rec, text) if rec else text


def _op_refs(op: Dict[str, Any]) -> List[str]:
    """The ids of existing records an op may name: edge endpoints, update and archive targets, merge sides,
    successors, gap targets and match candidates (``$refs`` left out)."""
    kind = op.get("op")
    annot = op.get("annot") or {}
    found: List[Any] = []
    if kind == "add_node":
        found = [m.get("id") for m in annot.get("matches") or [] if isinstance(m, dict)]
    elif kind == "add_edge":
        edge = op.get("edge") if isinstance(op.get("edge"), dict) else {}
        found = [edge.get("src"), edge.get("dst")]
    elif kind in ("update_node", "update_edge", "add_gap"):
        found = [op.get("id")]
    elif kind == "merge":
        found = [op.get("keep"), op.get("drop")]
    elif kind == "archive":
        block = op.get("archived") if isinstance(op.get("archived"), dict) else {}
        later = block.get("superseded_by") or []
        found = [op.get("id")] + ([later] if isinstance(later, str) else list(later) if isinstance(later, list) else [])
    out: List[str] = []
    for rid in found:
        if isinstance(rid, str) and rid and not rid.startswith("$") and rid not in out:
            out.append(rid)
    return out


def _effective(proposal: Dict[str, Any]) -> List[Tuple[Dict[str, Any], bool]]:
    """``(op, edited)`` per op: an op with a recorded edit shows its replacement."""
    edits = (proposal.get("review") or {}).get("edits") or {}
    out = []
    for op in proposal.get("ops") or []:
        if not isinstance(op, dict):
            continue
        replacement = edits.get(str(op.get("n")))
        if isinstance(replacement, dict):
            out.append((dict(replacement, n=op.get("n")), True))
        else:
            out.append((op, False))
    return out


def _by_n(items: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for item in items or []:
        if isinstance(item, dict):
            out.setdefault(str(item.get("n")), []).append(item)
    return out


def _term_ops(proposal: Dict[str, Any], terms: Sequence[str]) -> Dict[str, Any]:
    """``{term: n}``: the first add_node op (as proposed or as edited) that names each of ``terms``; a term no op
    names goes with the first op."""
    ops = [op for op in proposal.get("ops") or [] if isinstance(op, dict)]
    wanted = set(terms)
    edits = (proposal.get("review") or {}).get("edits") or {}
    out: Dict[str, Any] = {}
    for op in ops:
        replacement = edits.get(str(op.get("n"))) if isinstance(edits, dict) else None
        for form in [op] + ([replacement] if isinstance(replacement, dict) else []):
            node = form.get("node") if form.get("op") == "add_node" else None
            name = str(node.get("name") or "") if isinstance(node, dict) else ""
            if name in wanted:
                out.setdefault(name, op.get("n"))
    first = ops[0].get("n") if ops else None
    for term in terms:
        out.setdefault(term, first)
    return out


def _done(result: Any) -> bool:
    """Whether an applied result says its op wrote (a record or a pack entry), not ``skipped``."""
    return isinstance(result, dict) and bool(result) and not result.get("skipped")


def settled_view(proposal: Dict[str, Any]) -> Dict[str, Any]:
    """A proposal as it ended. A settled one (``SETTLED``) comes back as a shallow copy whose ``new_terms`` keeps
    only the terms none of its ops created, with the created ones in ``created_terms``: ``new_terms`` is worked out
    at propose time, and once the proposal is applied the terms of its applied add_node ops are in the ontology. A
    pending or reviewed proposal, or one already settled here, comes back as it is. The stored file is not changed."""
    if not isinstance(proposal, dict) or proposal.get("status") not in SETTLED or "created_terms" in proposal:
        return proposal
    terms = [str(t) for t in proposal.get("new_terms") or []]
    results = (proposal.get("applied") or {}).get("results") or {}
    op_of = _term_ops(proposal, terms)
    created = [t for t in terms if isinstance(results, dict) and _done(results.get(str(op_of.get(t))))]
    out = dict(proposal)
    out["new_terms"] = [t for t in terms if t not in created]
    out["created_terms"] = created
    return out


def _op_head(op: Dict[str, Any], cut: _Cutter, onto: Any = None, results: Any = None) -> List[str]:
    """The first line of an op (after its number) plus its detail lines. With ``onto``, ids of existing records
    carry their markers. ``results`` (an applied proposal's ``applied.results``) names the node an ``add_node``
    created, which a proposal stored before apply-time ids were written back may give wrong."""
    kind = str(op.get("op") or "?")
    annot = op.get("annot") or {}
    lines: List[str] = []
    if kind == "add_node":
        node = op.get("node") if isinstance(op.get("node"), dict) else {}
        nid = (mutate.created_id(op, results, op.get("n")) if results else None) or annot.get("assigned_id") \
            or node.get("id") or "%s:?" % (node.get("kind") or "?")
        head = "add_node %s %s (conf %s, %s)" % (nid, cut.quoted(node.get("name")), render.fmt(op.get("conf", 0.7)),
                                                 op.get("basis") or "inferred")
        if op.get("ref"):
            head += " as %s" % op["ref"]
        lines.append(head)
        if node.get("summary"):
            lines.append("summary: " + cut.quoted(node["summary"]))
        attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
        if attrs:
            # the status comes first and is never cut: an approval decides which checks a record must pass
            first = "status=%s" % render.plain(render.fmt(attrs["status"]), 40) if "status" in attrs else ""
            rest = "; ".join("%s=%s" % (k, render.fmt(attrs[k])) for k in sorted(attrs) if k != "status")
            lines.append("attrs: " + "; ".join(t for t in (first, cut.cut(rest) if rest else "") if t))
        if node.get("aliases"):
            lines.append("aliases: " + cut.cut(render.fmt(node["aliases"])))
        found = [m for m in annot.get("matches") or [] if isinstance(m, dict)]
        if found:
            shown = ["%s %s (%s)" % (_ref(m.get("id"), onto), render.fmt(m.get("score")), m.get("why") or "")
                     for m in found[:MAX_MATCHES]]
            extra = render.more(len(found), len(shown))
            lines.append("matches: " + "; ".join(shown) + ("; " + extra if extra else ""))
    elif kind == "add_edge":
        edge = op.get("edge") if isinstance(op.get("edge"), dict) else {}
        head = "add_edge %s -%s-> %s (conf %s, %s)" % (_ref(edge.get("src"), onto), edge.get("rel"),
                                                       _ref(edge.get("dst"), onto), render.fmt(op.get("conf", 0.7)),
                                                       op.get("basis") or "inferred")
        if edge.get("key"):
            head += " key %s" % edge["key"]
        if edge.get("background"):
            head += " background"
        lines.append(head)
        if edge.get("note"):
            lines.append("note: " + cut.quoted(edge["note"]))
    elif kind in ("update_node", "update_edge"):
        sets = op.get("set") if isinstance(op.get("set"), dict) else {}
        unsets = [str(u) for u in op.get("unset") or []]
        parts = ["%s=%s" % (k, cut.value(sets[k])) for k in sorted(sets)]
        head = "%s %s" % (kind, _ref(op.get("id"), onto))
        if parts:
            head += " set " + ", ".join(parts)
        if unsets:
            head += " unset " + ", ".join(unsets)
        lines.append(head)
        expect = annot.get("expect") or {}
        was = ["%s=%s" % (k, cut.value(expect[k])) for k in sorted(expect) if k not in ("change", "status")]
        if was:
            lines.append("was: " + ", ".join(was) + (" (%s)" % expect["status"] if expect.get("status") else ""))
        if op.get("reason"):
            lines.append("reason: " + cut.quoted(op["reason"]))
        conflict = annot.get("conflict")
        if isinstance(conflict, dict):
            lines.append("conflict %s: confirmed %s, proposed %s" % (
                conflict.get("path"), cut.value(conflict.get("current")), cut.value(conflict.get("proposed"))))
    elif kind == "merge":
        lines.append("merge %s into %s (%s is archived; its links, sources and names move to %s)" % (
            _ref(op.get("drop"), onto), _ref(op.get("keep"), onto), op.get("drop"), op.get("keep")))
        if op.get("reason"):
            lines.append("reason: " + cut.quoted(op["reason"]))
    elif kind == "archive":
        block = op.get("archived") if isinstance(op.get("archived"), dict) else {}
        head = "archive %s" % _ref(op.get("id"), onto)
        if block.get("decision"):
            head += " under %s" % block["decision"]
        successors = block.get("superseded_by")
        if successors:
            # a string (refused by P02) still prints as one id, not letter by letter
            successors = [successors] if isinstance(successors, str) else list(successors) \
                if isinstance(successors, (list, tuple)) else [successors]
            head += " superseded by %s" % ", ".join(_ref(s, onto) for s in successors)
        lines.append(head)
        if block.get("reason"):
            lines.append("reason: " + cut.quoted(block["reason"]))
    elif kind == "add_gap":
        gap = op.get("gap") if isinstance(op.get("gap"), dict) else {}
        lines.append("add_gap %s %s: %s" % (_ref(op.get("id"), onto), gap.get("field"), cut.quoted(gap.get("note"))))
    elif kind == "add_kind":
        decl = op.get("kind") if isinstance(op.get("kind"), dict) else {}
        extra = ", ".join("%s %s" % (k, decl[k]) for k in ("label", "dimension") if decl.get(k))
        lines.append("add_kind %s%s" % (op.get("name"), " (%s)" % extra if extra else ""))
    elif kind == "add_relation":
        decl = op.get("relation") if isinstance(op.get("relation"), dict) else {}
        lines.append("add_relation %s (%s -> %s%s)" % (op.get("name"), render.fmt(decl.get("from")),
                                                         render.fmt(decl.get("to")),
                                                         ", symmetric" if decl.get("symmetric") else ""))
    elif kind == "add_field":
        lines.append("add_field %s.%s %s" % (op.get("kind"), op.get("field"), cut.cut(render.fmt(op.get("schema")))))
    elif kind == "map_kinds":
        lines.append("map_kinds %s = %s" % (op.get("a"), op.get("b")))
    elif kind == "add_question":
        question = op.get("question") if isinstance(op.get("question"), dict) else {}
        lines.append("add_question %s %s" % (question.get("id"), cut.quoted(question.get("ask"))))
    else:
        lines.append("%s %s" % (kind, cut.cut(render.fmt({k: v for k, v in op.items() if k not in ("n", "annot")}))))
    return lines


def _prov_lines(op: Dict[str, Any], group_src: Optional[str], onto: Any, cut: _Cutter) -> List[str]:
    entries = [p for p in op.get("prov") or [] if isinstance(p, dict)]
    out = []
    for p in entries[:MAX_QUOTES]:
        src = str(p.get("src") or "")
        where = "%s %s" % (src, p.get("loc")) if src != group_src else str(p.get("loc"))
        if p.get("quote"):
            mark = "[untrusted] " if _untrusted(src, onto) else ""
            out.append("%s%s %s" % (mark, where, cut.quoted(p["quote"])))
        else:
            out.append("%s (no quote)" % where)
    extra = render.more(len(entries), len(out))
    if extra:
        out.append("%s provenance entries" % extra)
    return out


def _codes(items: Sequence[Dict[str, Any]]) -> str:
    """The tag of an op that problems name: ``problem quote``, or ``problems P02, P08``."""
    codes: List[str] = []
    for p in items:
        if str(p.get("code")) not in codes:
            codes.append(str(p.get("code")))
    return "problem%s %s" % ("s" if len(codes) > 1 else "", ", ".join(codes))


def _propose_pages() -> bool:
    """Whether the propose command takes ``offset``, so a refused preview can name the call for its next page."""
    try:
        return "offset" in commands.get("propose").props
    except Exception:  # a renderer must still print what it has
        return False


def _numbers(page: Sequence[Tuple[Dict[str, Any], bool]]) -> str:
    return ranges(op.get("n") for op, _edited in page if isinstance(op.get("n"), int))


def preview_lines(proposal: Dict[str, Any], mcp: bool = False, onto: Any = None,
                  width: int = render.WIDTH, offset: int = 0, limit: int = 0,
                  claims: Optional[Claims] = None) -> List[str]:
    """The numbered preview of a proposal: a head line, then its ops grouped by the source they cite (numbers kept;
    an op that cites none goes with the proposal's source), each with its provenance, matches, expectations,
    conflicts and warnings; then the impact, the new terms, the destructive ops, the review and the applied record,
    and the next call. ``onto`` (optional) tells interview sources apart, whose quotes are not marked
    ``[untrusted]`` (without it every quote is marked), and marks the ids of existing records. ``width`` cuts free
    text (0 = no cut); problem and warning messages are never cut. ``offset`` and ``limit`` (0 = all) page the ops;
    a paged preview says which ops it shows, lists the impact ids and new terms of those ops (``_claims``;
    ``claims`` saves working them out again) with how many the proposal has in all, and ends with the call for the
    next page.

    A refused preview (the proposal has problems) pages over the ops a problem or warning names, not every op, and
    prints the problems of the ops it shows (and those of the whole proposal) right after the head line, each op
    tagged with its problem codes; its next page is ``propose`` again with ``offset``. A settled proposal
    (``settled_view``) lists what it created and which destructive ops it applied, not what is still to decide."""
    cut = _Cutter(width)
    proposal = settled_view(proposal)
    pid = str(proposal.get("id") or "(unsaved)")
    ops = _effective(proposal)
    checks = proposal.get("checks") or {}
    problems = [p for p in checks.get("problems") or [] if isinstance(p, dict)]
    warnings = [w for w in checks.get("warnings") or [] if isinstance(w, dict)]
    refused = bool(problems)
    op_keys = {str(op.get("n")) for op, _edited in ops}
    problem_by_n, warning_by_n = _by_n(problems), _by_n(warnings)
    # a refusal shows what is to be fixed: the ops a problem or warning names (every op when none names one)
    flagged = [(op, edited) for op, edited in ops if str(op.get("n")) in problem_by_n
               or str(op.get("n")) in warning_by_n]
    focus = refused and bool(flagged)
    pool = flagged if focus else ops
    offset = max(0, int(offset or 0))
    limit = max(0, int(limit or 0))
    page = pool[offset: offset + limit] if limit else pool[offset:]
    keys = {str(op.get("n")) for op, _edited in page}
    paged = len(page) < len(ops)  # some ops are left out, so the lists kept per op show those of this page
    settled = proposal.get("status") in SETTLED and not refused
    status = "refused, not saved" if refused else str(proposal.get("status") or "pending")
    head = "%s %s | priority %s | by %s | %d op%s" % (pid, status, render.fmt(proposal.get("priority")),
                                                      proposal.get("by") or "agent", len(ops),
                                                      "" if len(ops) == 1 else "s")
    if proposal.get("supersedes"):
        head += " | supersedes %s" % proposal["supersedes"]
    lines = [head]
    if proposal.get("summary"):
        lines.append("summary: " + cut.quoted(proposal["summary"]))
    # problems in full (they name what the kit accepts, and a refusal has no saved text to show them in); the ops
    # of the page that share a problem (the same unknown kind, say) share its line
    shared: Dict[Tuple[str, str], List[int]] = {}
    for p in problems:
        message = render.plain(p.get("message"))
        if p.get("n") is None or str(p.get("n")) not in op_keys:
            lines.append("problem %s: %s" % (p.get("code"), message))
        elif str(p.get("n")) in keys and isinstance(p.get("n"), int) and not isinstance(p.get("n"), bool):
            shared.setdefault((str(p.get("code")), message), []).append(p["n"])
        elif str(p.get("n")) in keys:
            lines.append("problem op %s %s: %s" % (p.get("n"), p.get("code"), message))
    for (code, message), ns in shared.items():
        lines.append("problem op%s %s %s: %s" % ("" if len(set(ns)) == 1 else "s", ranges(ns), code, message))
    for w in warnings:
        if w.get("n") is None or str(w.get("n")) not in op_keys:
            lines.append("warning %s: %s" % (w.get("code"), render.plain(w.get("message"))))
    if focus:
        plural = "" if len(ops) == 1 else "s"
        if not page:
            lines.append("no ops with problems or warnings from offset %d; %d of the %d op%s have them"
                         % (offset, len(pool), len(ops), plural))
        elif len(page) == len(pool):
            lines.append("ops with problems or warnings: %s (%d of %d op%s)" % (_numbers(page), len(pool), len(ops),
                                                                               plural))
        else:
            lines.append("ops with problems or warnings: %s shown, %d-%d of %d (%d op%s in all)" % (
                _numbers(page), offset + 1, offset + len(page), len(pool), len(ops), plural))
    elif paged:
        if page:
            lines.append("ops %s-%s of %d shown" % (page[0][0].get("n"), page[-1][0].get("n"), len(ops)))
        else:
            lines.append("no ops from offset %d; the proposal has %d" % (offset, len(ops)))
    verdicts = (proposal.get("review") or {}).get("verdicts") or {}
    results = (proposal.get("applied") or {}).get("results") or {}
    destructive = [n for n in pipeline.destructive(proposal) if n is not None]
    groups: List[Tuple[Optional[str], List[Tuple[Dict[str, Any], bool]]]] = []
    index: Dict[Optional[str], int] = {}
    for op, edited in page:
        src = _op_source(op, proposal.get("source"))
        if src not in index:
            index[src] = len(groups)
            groups.append((src, []))
        groups[index[src]][1].append((op, edited))
    for src, members in groups:
        lines.append(source_label(src, onto))
        for op, edited in members:
            n = str(op.get("n"))
            body = _op_head(op, cut, onto, results)
            tags = []
            if problem_by_n.get(n):
                tags.append(_codes(problem_by_n[n]))  # the messages are listed after the head line
            if edited:
                tags.append("edited")
            if verdicts.get(n):
                tags.append(verdicts[n])
            if op.get("n") in destructive:
                tags.append("destructive")
            result = results.get(n)
            if isinstance(result, dict):
                tags.append(_result_text(result))
            first = "  %s %s" % (n, body[0])
            if tags:
                first += " [%s]" % ", ".join(tags)
            lines.append(first)
            detail = body[1:] + _prov_lines(op, src, onto, cut)
            detail += ["warning %s: %s" % (w.get("code"), render.plain(w.get("message")))
                       for w in warning_by_n.get(n, [])]
            lines.extend("      " + d for d in detail)
    impact = [str(i) for i in proposal.get("impact") or []]
    terms = [str(t) for t in proposal.get("new_terms") or []]
    created = [str(t) for t in proposal.get("created_terms") or []]
    label_impact, label_created = "impact", "created"
    label_terms = "not added" if settled else "not in the ontology yet"
    if paged and not page:
        impact, terms, created = [], [], []
    elif paged and (impact or terms or created):
        # a page names the impact ids and new terms of its own ops, so every one is read once, page by page
        term_of, impact_of = claims if claims is not None else _claims(proposal, onto)
        span = ("op%s %s" % ("" if len(page) == 1 else "s", _numbers(page)) if focus
                else "ops %s-%s" % (page[0][0].get("n"), page[-1][0].get("n")))
        if impact:
            label_impact = "impact of %s (%d in all)" % (span, len(impact))
            impact = [i for i in impact if str(impact_of.get(i)) in keys]
        if terms:
            label_terms = "%s, from %s (%d in all)" % (label_terms, span, len(terms))
            terms = [t for t in terms if str(term_of.get(t)) in keys]
        if created:
            label_created = "created, from %s (%d in all)" % (span, len(created))
            created = [t for t in created if str(term_of.get(t)) in keys]
    cap = MAX_LIST if cut.width else max(len(impact), len(terms), len(created))  # the full text form lists all
    if impact:
        extra = render.more(len(impact), min(len(impact), cap))
        lines.append(label_impact + ": " + ", ".join(impact[:cap]) + (" " + extra if extra else ""))
    for label, names in ((label_created, created), (label_terms, terms)):
        if names:
            extra = render.more(len(names), min(len(names), cap))
            lines.append(label + ": " + ", ".join(cut.quoted(t) for t in names[:cap]) + (" " + extra if extra else ""))
    if destructive and settled:
        # decided and written: report what happened, never ask for the yes again
        done = [n for n in destructive if _done(results.get(str(n)))]
        if done:
            lines.append("destructive ops applied: %s" % ranges(done))
        if len(done) < len(destructive):
            lines.append("destructive ops not applied: %s" % ranges(n for n in destructive if n not in done))
    elif destructive:
        lines.append("destructive (merge, archive, or a change to a confirmed record; needs an explicit yes): ops %s"
                     % ranges(destructive))
    review = proposal.get("review") or {}
    if review:
        text = "review: by %s at %s: %s" % (review.get("by"), review.get("at"), verdict_text(verdicts))
        if review.get("reason"):
            text += " | reason " + cut.quoted(review["reason"])
        lines.append(text)
    applied = proposal.get("applied") or {}
    if applied:
        lines.append("applied: %s at %s" % (applied.get("change") or "no change", applied.get("at")))
    stale: List[Any] = []
    if not refused and onto is not None and proposal.get("id") and proposal.get("status") in pipeline.OPEN:
        try:
            stale = list(pipeline.stale_ops(onto, proposal))
        except Exception:  # rendering never fails on the check
            stale = []
    if refused:
        lines.append("Next: fix every problem and propose again: %s" % render.call(mcp, "propose", proposal="..."))
    elif stale:
        lines.append("stale: the data changed under op%s %s; propose the same draft again (it replaces this one) or "
                     "reject it: %s" % ("" if len(stale) == 1 else "s", ranges(stale),
                                        render.call(mcp, "apply", id=pid, all="reject",
                                                    confirm=True if mcp else None)))
    elif proposal.get("id") and proposal.get("status") == "pending":
        confirm = True if mcp else None
        fixed = undraftable(proposal)
        calibration = proposal.get("calibration") if isinstance(proposal.get("calibration"), dict) else {}
        for p in (calibration.get("accept") or [])[:3]:
            lines.append("would be refused if accepted: %s %s" % (p.get("code"), render.plain(p.get("message"))))
        if calibration.get("accept") or calibration.get("draft"):
            lines.append("a draft verdict leaves attrs.status as it is: to keep a risk draft, give its op an edit (the "
                         "same op with attrs.status draft, as edit={n: op}), link an implemented control, or raise "
                         "the residual")
        text = "Next: %s" % render.call(mcp, "apply", id=pid, accept="...",
                                        draft="..." if len(fixed) < len(ops) else None, reject="...", confirm=confirm)
        if len(fixed) < len(ops) and not calibration.get("draft"):  # never a triage the check would refuse
            text += " | fast triage: %s" % render.call(mcp, "apply", id=pid, all="draft",
                                                       reject=ranges(fixed) if fixed else None, confirm=confirm)
        lines.append(text)
        if fixed:
            lines.append("ops %s take accept or reject only (merge, archive, pack or question ops, or a change to a "
                         "confirmed record)" % ranges(fixed))
    elif proposal.get("id") and proposal.get("status") == "reviewed":
        lines.append("Next: apply the recorded verdicts: %s" % render.call(mcp, "apply", id=pid,
                                                                             confirm=True if mcp else None))
    if cut.count and proposal.get("id") and not refused:
        where = {"offset": offset or None, "limit": len(page) if paged else None}
        if mcp:
            whole = render.call(True, "review", id=pid, **dict(where, format="text"))
        else:
            whole = render.call(False, "review", id=pid, **where) + " --text"
        lines.append("(%d text(s) cut at %d characters; the whole text: %s)" % (cut.count, cut.width, whole))
    left = len(pool) - offset - len(page)
    if left > 0:
        follow = {"offset": offset + len(page), "limit": len(page) if len(page) != REVIEW_LIMIT else None}
        what = "%d more op%s%s" % (left, "" if left == 1 else "s", " with problems or warnings" if focus else "")
        if refused and _propose_pages():
            lines.append("+%s: %s" % (what, render.call(mcp, "propose", proposal="...", **follow)))
        elif refused:
            lines.append("+%s (fix the problems shown and propose again to see the rest)" % what)
        elif proposal.get("id"):
            lines.append("+%s: %s" % (what, render.call(mcp, "review", id=pid, **follow)))
        else:
            lines.append("+%s" % what)
    return lines


def calibration(repo: Any, proposal: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """For a pending proposal in a topic with the ``assessment`` pack: the problems (a P23) the final check of
    ``apply`` would refuse it with when every op is accepted (``accept``) and under the fast triage, every op a
    draft and the undraftable ones rejected (``draft``). Empty lists otherwise. Nothing is written."""
    out: Dict[str, List[Dict[str, Any]]] = {"accept": [], "draft": []}
    try:
        packs_on = [str(p) for p in (repo.manifest.get("packs") or [])]
    except Exception:
        return out
    ops = [op for op in proposal.get("ops") or [] if isinstance(op, dict)]
    if "assessment" not in packs_on or proposal.get("status") != "pending" or not ops:
        return out
    fixed = set(undraftable(proposal))
    for key, verdict in (("accept", lambda n: "accept"), ("draft", lambda n: "reject" if n in fixed else "draft")):
        verdicts = {str(op.get("n")): verdict(op.get("n")) for op in ops}
        try:
            found = pipeline.trial_problems(repo, proposal, verdicts, by="user", change_type="apply")
        except Exception:  # a preview never fails on the check
            found = []
        out[key] = [p for p in found if str(p.get("code")) == "P23" or " P23 " in str(p.get("message"))]
    return out


def undraftable(proposal: Dict[str, Any]) -> List[int]:
    """Op numbers that take accept or reject only: merges, archives, pack and question ops, and updates of a
    confirmed record (``pipeline`` refuses to draft them)."""
    out = []
    for op, _edited in _effective(proposal):
        kind = op.get("op")
        expect = (op.get("annot") or {}).get("expect") or {}
        if kind in ("merge", "archive", "add_question") + pipeline.PACK_OPS or (
                kind in ("update_node", "update_edge") and expect.get("status") == "confirmed"):
            if isinstance(op.get("n"), int):
                out.append(op["n"])
    return out


def _result_text(result: Dict[str, Any]) -> str:
    """One applied result in a few words."""
    if result.get("skipped"):
        return "skipped (%s)" % result["skipped"]
    if result.get("merged"):
        return "%s merged into %s" % (result["merged"], result.get("id"))
    if result.get("id"):
        text = "%s %s" % (result["id"], result.get("status") or "")
        if result.get("existed"):
            text += ", existed"
        if result.get("edges_archived"):
            text += ", %s edge(s) archived" % render.fmt(result["edges_archived"])
        return text.strip()
    for key in ("kind", "relation", "question"):
        if result.get(key):
            extra = ".%s" % result["field"] if key == "kind" and result.get("field") else ""
            return "%s %s%s added" % (key, result[key], extra)
    if result.get("map"):
        return "kinds %s mapped" % " = ".join(str(k) for k in result["map"])
    return render.fmt(result)


# shared helpers ------------------------------------------------------------------------------------------------
def _onto(ctx: Any) -> Any:
    """The loaded graph for rendering, or None when it cannot load (rendering never fails on it)."""
    try:
        return ctx.onto()
    except Exception:  # a renderer must still print what it has
        return None


def _brief(proposal: Dict[str, Any], saved: bool = True) -> Dict[str, Any]:
    out = {"id": proposal.get("id"), "status": proposal.get("status") if saved else "refused",
           "priority": proposal.get("priority"), "source": proposal.get("source"), "summary": proposal.get("summary"),
           "ops": len(proposal.get("ops") or []), "impact": list(proposal.get("impact") or []), "saved": saved}
    if proposal.get("note"):
        out["note"] = proposal["note"]
    return out


def _matches(proposal: Dict[str, Any], onto: Any = None) -> List[Dict[str, Any]]:
    """Every match candidate of the proposal's new nodes, with the JSON flags of the record it names."""
    out = []
    for op in proposal.get("ops") or []:
        if not isinstance(op, dict):
            continue
        for m in (op.get("annot") or {}).get("matches") or []:
            if isinstance(m, dict):
                item = {"n": op.get("n"), "id": m.get("id"), "score": m.get("score"), "why": m.get("why")}
                if onto is not None and isinstance(m.get("id"), str):
                    item.update(render.flags(onto.record(m["id"])))
                out.append(item)
    return out


def _flag_op(op: Dict[str, Any], source: Optional[str], onto: Any) -> None:
    """Put the JSON marks on one op of an output copy (C.7): ``untrusted: true`` on each provenance entry from a
    source that is not an interview and on an op drawn from such a source, and ``marks`` {id: flags} for the
    existing records it names that carry a marker."""
    for p in op.get("prov") or []:
        if isinstance(p, dict) and p.get("src") and _untrusted(str(p["src"]), onto):
            p["untrusted"] = True
    if _source_untrusted(_op_source(op, source), onto):
        op["untrusted"] = True
    if onto is None:
        return
    marks: Dict[str, Dict[str, bool]] = {}
    for rid in _op_refs(op):
        found = render.flags(onto.record(rid))
        if found:
            marks[rid] = found
    if marks:
        op["marks"] = marks


def _resolved(ref: Any, onto: Any, wanted: Set[str]) -> Optional[str]:
    """The id of ``wanted`` that an op's reference names: itself, its local form, or what the graph resolves it
    to (a loose id such as ``watering``); None when it names none of them."""
    if not isinstance(ref, str) or not ref or ref.startswith("$"):
        return None
    if ref in wanted:
        return ref
    if onto is None:
        return None
    try:
        local = onto.own_local(ref)
        if local in wanted:
            return local
        found = (onto.resolve(ref) or {}).get("id")
    except Exception:  # a reference that does not resolve names nothing here
        return None
    return found if found in wanted else None


def _touched(op: Dict[str, Any], onto: Any, wanted: Set[str]) -> List[str]:
    """The impact ids (of ``wanted``) one op touches: the existing records it names (not the match candidates of a
    new node), the edge it adds when that edge exists, and the edges of a node it merges away or archives."""
    kind = op.get("op")
    out: List[str] = []
    named: Dict[str, Optional[str]] = {}
    for ref in ([] if kind == "add_node" else _op_refs(op)):
        named[ref] = _resolved(ref, onto, wanted)
        if named[ref] and named[ref] not in out:
            out.append(str(named[ref]))
    if onto is None:
        return out
    extra: List[Any] = []
    try:
        if kind == "add_edge":
            edge = op.get("edge") if isinstance(op.get("edge"), dict) else {}
            src, dst = named.get(edge.get("src")), named.get(edge.get("dst"))
            if src and dst:
                rel, key = str(edge.get("rel")), str(edge.get("key") or "")
                ends = [(src, dst)] + ([(dst, src)] if (onto.registry.relation(rel) or {}).get("symmetric") else [])
                extra = [ids.edge_id_in(onto.edges, a, rel, b, key) for a, b in ends]
        elif kind in ("merge", "archive"):
            rid = named.get(op.get("drop") if kind == "merge" else op.get("id"))
            if rid:
                extra = [e["edge"].get("id") for e in onto.edges_of(rid, include_archived=True)]
    except Exception:  # attribution is best effort: what no op claims shows with the first op
        extra = []
    for rid in extra:
        if isinstance(rid, str) and rid in wanted and rid not in out:
            out.append(rid)
    return out


def _claims(proposal: Dict[str, Any], onto: Any = None) -> Claims:
    """Which op each new term and each impact id belongs to: ``({term: n}, {id: n})``. A term (of ``new_terms`` and,
    in a ``settled_view``, ``created_terms``) belongs to the first add_node op that names it (as proposed or as
    edited; ``_term_ops``); an impact id to the first op that touches it (``_touched``). An entry no op claims
    belongs to the first op, so it shows on the first page."""
    ops = [op for op in proposal.get("ops") or [] if isinstance(op, dict)]
    first = ops[0].get("n") if ops else None
    terms = [str(t) for t in list(proposal.get("new_terms") or []) + list(proposal.get("created_terms") or [])]
    impact = [str(i) for i in proposal.get("impact") or []]
    wanted_ids = set(impact)
    edits = (proposal.get("review") or {}).get("edits") or {}
    impact_of: Dict[str, Any] = {}
    for op in ops:
        if not wanted_ids or len(impact_of) >= len(wanted_ids):
            break
        n = op.get("n")
        replacement = edits.get(str(n)) if isinstance(edits, dict) else None
        for form in [op] + ([replacement] if isinstance(replacement, dict) else []):
            for rid in _touched(form, onto, wanted_ids):
                impact_of.setdefault(rid, n)
    for rid in impact:
        impact_of.setdefault(rid, first)
    return _term_ops(proposal, terms), impact_of


def _page_copy(proposal: Dict[str, Any], page: List[Dict[str, Any]], claims: Claims) -> Dict[str, Any]:
    """A copy of a proposal that holds one page of its ops and, of every list or map kept per op, only the entries of
    those ops: the new terms and impact ids they claim (``_claims``), their verdicts and edits, their applied results
    and their problems and warnings (those of the whole proposal stay on every page)."""
    keys = {str(op.get("n")) for op in page}
    term_of, impact_of = claims
    split = ("ops", "new_terms", "created_terms", "impact", "checks", "review", "applied")
    out = copy.deepcopy({k: v for k, v in proposal.items() if k not in split})
    out["ops"] = copy.deepcopy(page)
    out["new_terms"] = [t for t in proposal.get("new_terms") or [] if str(term_of.get(str(t))) in keys]
    if "created_terms" in proposal:
        out["created_terms"] = [t for t in proposal.get("created_terms") or [] if str(term_of.get(str(t))) in keys]
    out["impact"] = [i for i in proposal.get("impact") or [] if str(impact_of.get(str(i))) in keys]
    checks = proposal.get("checks")
    if isinstance(checks, dict):
        kept = copy.deepcopy({k: v for k, v in checks.items() if k not in ("problems", "warnings")})
        for key in ("problems", "warnings"):
            if key in checks:
                kept[key] = [copy.deepcopy(p) for p in checks.get(key) or []
                             if not isinstance(p, dict) or p.get("n") is None or str(p.get("n")) in keys]
        out["checks"] = kept
    elif "checks" in proposal:
        out["checks"] = copy.deepcopy(checks)
    for block, fields in (("review", ("verdicts", "edits")), ("applied", ("results",))):
        value = proposal.get(block)
        if not isinstance(value, dict):
            if block in proposal:
                out[block] = copy.deepcopy(value)
            continue
        kept = copy.deepcopy({k: v for k, v in value.items() if k not in fields})
        for key in fields:
            if key in value:
                items = value.get(key)
                kept[key] = ({k: copy.deepcopy(v) for k, v in items.items() if str(k) in keys}
                             if isinstance(items, dict) else copy.deepcopy(items))
        out[block] = kept
    return out


def marked(proposal: Dict[str, Any], onto: Any = None, offset: int = 0, limit: int = 0,
           claims: Optional[Claims] = None) -> Dict[str, Any]:
    """A copy of a proposal for a JSON result, with the C.7 marks on its ops and recorded edits (see ``_flag_op``)
    and ``untrusted: true`` when its source is not an interview; ``offset`` and ``limit`` (0 = all) keep one page
    of its ops, and with them only the entries of those ops in the lists and maps kept per op (``_page_copy``), so a
    page grows with its ops and not with the proposal. ``claims`` (from ``_claims``) saves working them out again.
    A settled proposal is shown as it ended (``settled_view``: its created terms in ``created_terms``). Without
    ``onto`` every cited source counts as untrusted. The stored file is not changed."""
    proposal = settled_view(proposal)
    offset = max(0, int(offset or 0))
    limit = max(0, int(limit or 0))
    if offset or limit:
        every = [op for op in proposal.get("ops") or [] if isinstance(op, dict)]
        page = every[offset: offset + limit] if limit else every[offset:]
        out = _page_copy(proposal, page, claims if claims is not None else _claims(proposal, onto))
    else:
        out = copy.deepcopy(proposal)
    ops = [op for op in out.get("ops") or [] if isinstance(op, dict)]
    source = out.get("source")
    for op in ops:
        _flag_op(op, source, onto)
    for op in ((out.get("review") or {}).get("edits") or {}).values():
        if isinstance(op, dict):
            _flag_op(op, source, onto)
    if _source_untrusted(source, onto):
        out["untrusted"] = True
    return out


def _json_arg(value: Any, what: str) -> Any:
    """An object argument: an object, or JSON text (never a file over MCP; the CLI reads ``@file`` itself)."""
    if not isinstance(value, str):
        return value
    if value.startswith("@"):
        raise UsageError("%s: pass the JSON itself here; only the command line reads @file" % what)
    try:
        return json.loads(value)
    except ValueError as exc:
        raise UsageError("%s: not valid JSON (%s)" % (what, exc))


def _richness(ctx: Any) -> Optional[int]:
    """The richness score when the richness module is built (None otherwise, or when it fails)."""
    rich = commands.optional("richness")
    if rich is None or not hasattr(rich, "summary"):
        return None
    try:
        summary = rich.summary(ctx.onto())
    except Exception:  # a measure must never fail a write that already happened
        return None
    score = summary.get("score") if isinstance(summary, dict) else None
    return score if isinstance(score, int) and not isinstance(score, bool) else None


def _change(before: Optional[int], after: Optional[int]) -> Optional[Dict[str, Any]]:
    if before is None or after is None:
        return None
    return {"before": before, "after": after, "delta": after - before}


# propose -------------------------------------------------------------------------------------------------------
def _propose_paging(proposal: Dict[str, Any], offset: int, limit: int) -> Dict[str, Any]:
    """The paging of a propose preview (``preview_lines``): a refused proposal pages over the ops a problem or
    warning names (every op when none names one), a saved one over every op."""
    ops = [op for op in proposal.get("ops") or [] if isinstance(op, dict)]
    checks = proposal.get("checks") or {}
    problems = [p for p in checks.get("problems") or [] if isinstance(p, dict)]
    named = {str(p.get("n")) for p in problems + [w for w in checks.get("warnings") or [] if isinstance(w, dict)]
             if p.get("n") is not None}
    flagged = [op for op in ops if str(op.get("n")) in named] if problems else []
    pool = flagged or ops
    shown = len(pool[offset: offset + limit] if limit else pool[offset:])
    remaining = max(0, len(pool) - offset - shown)
    return {"key": "flagged_ops" if flagged else "ops", "offset": offset, "limit": limit, "total": len(pool),
            "more": remaining > 0, "remaining": remaining, "next_offset": offset + shown if remaining else None}


def cmd_propose(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """Check a proposal and save it as pending. A refusal returns ``error: "refused"`` with every problem and the
    preview (exit code 1); nothing is saved. The preview shows ``limit`` ops (``PROPOSE_LIMIT`` by default, 0 = all)
    from ``offset``: of a refusal, the ops a problem or warning names, with their problems after the head line; of
    a saved proposal, its ops, the rest paged with ``review``. ``paging`` says which part it shows."""
    draft = _json_arg(args.get("proposal"), "proposal")
    if not isinstance(draft, dict):
        raise UsageError("proposal must be a JSON object {source, summary, ops}")
    offset = _count(args.get("offset"), 0)
    limit = _count(args.get("limit"), PROPOSE_LIMIT)
    repo = ctx.repo
    by = draft.get("by") if draft.get("by") in ("user", "agent") else "agent"
    try:
        proposal = pipeline.prepare(repo, draft, by=by, mcp=bool(ctx.mcp))
    except Refused as exc:
        refused = exc.extra.get("proposal")
        if not isinstance(refused, dict):
            raise
        onto = _onto(ctx)
        checks = refused.get("checks") or {}
        return {
            "error": "refused",
            "message": exc.message,
            "exit_code": 1,
            "proposal": _brief(refused, saved=False),
            "problems": list(exc.problems),
            "warnings": list(checks.get("warnings") or []),
            "new_terms": list(refused.get("new_terms") or []),
            "matches": _matches(refused, onto),
            "preview": preview_lines(refused, ctx.mcp, onto, offset=offset, limit=limit),
            "paging": _propose_paging(refused, offset, limit),
        }
    onto = _onto(ctx)
    proposal = settled_view(proposal)  # proposed before and applied since: say what it created
    proposal = dict(proposal, calibration=calibration(repo, proposal))
    checks = proposal.get("checks") or {}
    out = {
        "proposal": _brief(proposal),
        "calibration": proposal["calibration"],
        "problems": [],
        "warnings": list(checks.get("warnings") or []),
        "new_terms": list(proposal.get("new_terms") or []),
        "matches": _matches(proposal, onto),
        "preview": _fit_preview(proposal, ctx, onto, offset, limit, render.WIDTH),
        "paging": _propose_paging(proposal, offset, limit),
    }
    if "created_terms" in proposal:
        out["created_terms"] = list(proposal["created_terms"])
    return out


def render_propose(result: Dict[str, Any], mode: str, ctx: Any) -> List[str]:
    brief = result.get("proposal") or {}
    paging = result.get("paging") if isinstance(result.get("paging"), dict) else {}
    preview = list(result.get("preview") or [])
    if result.get("error"):
        count = len(result.get("problems") or [])
        head = "refused: %d problem%s; nothing was saved. " % (count, "" if count == 1 else "s")
        if paging.get("offset") or paging.get("more"):
            head += ("Below are the problems of the ops on this page (and of the whole proposal); the last line "
                     "pages on. Fix every problem and propose again.")
        else:
            head += "Fix every problem below and propose again."
    elif brief.get("note") == "already proposed":
        head = "already proposed: %s is %s; nothing new was saved" % (brief.get("id"), brief.get("status"))
    else:
        head = "proposed %s (pending, priority %s); review it before it changes the ontology" % (
            brief.get("id"), render.fmt(brief.get("priority")))
    if mode == "text" and not result.get("error") and brief.get("id"):
        # the full text form: the saved proposal again, its texts uncut (a refusal has no saved text to reload)
        try:
            whole = pipeline.load(ctx.repo, str(brief["id"]))
        except Exception:  # a renderer must still print what it has
            whole = None
        if whole is not None:
            preview = _fit_preview(settled_view(whole), ctx, _onto(ctx), paging.get("offset") or 0,
                                   paging.get("limit") or 0, 0)
    return [head] + preview


# review --------------------------------------------------------------------------------------------------------
def _pending_item(proposal: Dict[str, Any], onto: Any = None) -> Dict[str, Any]:
    item = {
        "id": proposal.get("id"),
        "status": proposal.get("status"),
        "summary": proposal.get("summary"),
        "priority": proposal.get("priority"),
        "age_days": pipeline.age_days(proposal),
        "ops": len(proposal.get("ops") or []),
        "source": proposal.get("source"),
        "by": proposal.get("by"),
        "created": proposal.get("created"),
        "destructive": [n for n in pipeline.destructive(proposal) if n is not None],
    }
    if _source_untrusted(proposal.get("source"), onto):
        item["untrusted"] = True
    return item


def _count(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        raise UsageError("expected a whole number, not %r" % (value,))


def _one(proposal: Dict[str, Any], ctx: Any, onto: Any, offset: int, count: int, claims: Optional[Claims],
         asked: Optional[int] = None) -> Dict[str, Any]:
    """The review result of one proposal with ``count`` ops from ``offset`` (0 = all). A page holds the entries
    of its own ops only (``marked``, and ``destructive`` too); ``totals`` counts each list over the whole
    proposal. ``asked`` is the page size the call asked for, when the size cap made it ``count``. A settled
    proposal is shown as it ended (``settled_view``)."""
    proposal = settled_view(proposal)
    total = len(proposal.get("ops") or [])
    shown = max(0, min(total - offset, count) if count else total - offset)
    remaining = max(0, total - offset - shown)
    paged = bool(offset or shown < total)
    destructive = [n for n in pipeline.destructive(proposal) if n is not None]
    totals = {"ops": total, "new_terms": len(proposal.get("new_terms") or []),
              "impact": len(proposal.get("impact") or []), "destructive": len(destructive)}
    if "created_terms" in proposal:
        totals["created_terms"] = len(proposal["created_terms"])
    for block, key in (("review", "verdicts"), ("applied", "results")):
        items = (proposal.get(block) or {}).get(key)
        if isinstance(items, dict) and items:
            totals[key] = len(items)
    if paged:
        keys = {str(op.get("n")) for op in (proposal.get("ops") or [])[offset: offset + shown] if isinstance(op, dict)}
        destructive = [n for n in destructive if str(n) in keys]
    paging = {"key": "ops", "offset": offset, "limit": count, "more": remaining > 0, "remaining": remaining,
              "next_offset": offset + shown if remaining > 0 else None}
    if asked is not None and asked != count:
        paging.update(asked=asked, capped_by_size=True)
    return {
        "proposal": marked(proposal, onto, offset, shown if paged else 0, claims=claims),
        "status": proposal.get("status"),
        "destructive": destructive,
        "preview": preview_lines(proposal, ctx.mcp, onto, offset=offset, limit=count, claims=claims),
        "totals": totals,
        "paging": paging,
    }


def _json_size(result: Dict[str, Any]) -> int:
    """The characters of a result as the MCP server sends it (one compact JSON line)."""
    return len(json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str))


def _shrunk(count: int, size: int) -> int:
    """The next, smaller page size for a page of ``count`` ops that came to ``size`` characters."""
    return max(1, min(count - 1, int(count * FIT_CHARS * 0.9 / size)))


def _cut_to_fit(value: Any, rule: str, path: str, cut: Dict[str, int]) -> Any:
    """``value`` with each list longer than ``MAX_LIST`` kept to its first entries (``rule`` lists) or each text
    longer than ``render.WIDTH`` cut (``rule`` texts); ``cut`` maps each shortened path to its full length."""
    if isinstance(value, dict):
        return {k: _cut_to_fit(v, rule, "%s.%s" % (path, k), cut) for k, v in value.items()}
    if isinstance(value, list):
        if rule == "lists" and len(value) > MAX_LIST:
            cut[path] = len(value)
            value = value[:MAX_LIST]
        return [_cut_to_fit(v, rule, "%s[%d]" % (path, i), cut) for i, v in enumerate(value)]
    if rule == "texts" and isinstance(value, str) and len(value) > render.WIDTH:
        cut[path] = len(value)
        return render.trunc(value, render.WIDTH)
    return value


def _too_big(pid: str, size: int, offset: int, how: str, flag: str) -> str:
    """The plain note on a page that one op already takes over the cap, where a smaller limit cannot help."""
    whole = render.call(False, "review", id=pid, offset=offset or None, limit=1) + " " + flag
    return ("one op of %s with what does not split by op (the head, proposal-wide checks, ids and terms no op "
            "names) is %d characters, over the %d a review returns over MCP, so a smaller limit or another offset "
            "cannot shorten it; %s. In full: %s on the command line" % (pid, size, FIT_CHARS, how, whole))


def _fit_json(result: Dict[str, Any], pid: str, offset: int) -> Dict[str, Any]:
    """A one-op page whose JSON is still over ``FIT_CHARS``, cut to fit with a plain ``note``: long lists in the
    proposal and the preview keep their first ``MAX_LIST`` entries, then long texts are cut, then the preview (the
    same page as text) is left out."""
    out = json.loads(json.dumps(result, default=str))
    cut: Dict[str, int] = {}
    out["note"] = _too_big(pid, _json_size(result), offset, "this page is cut to fit (cut gives the full length of "
                                                            "each list and text it shortens)", "--json")
    out["cut"] = cut  # filled below: the note and the map count toward the size
    for rule in ("lists", "texts"):
        if _json_size(out) <= FIT_CHARS:
            break
        for key in ("proposal", "preview"):
            out[key] = _cut_to_fit(out.get(key), rule, key, cut)
    if _json_size(out) > FIT_CHARS and out.get("preview"):
        cut["preview"] = len(result.get("preview") or [])  # the text of the same page; format=compact shows it
        out["preview"] = []
    return out


def _fit_lines(lines: List[str], pid: str, offset: int) -> List[str]:
    """A one-op text page still over ``FIT_CHARS``: its lines kept while they fit, then a plain note."""
    note = "(%s)" % _too_big(pid, len("\n".join(lines)), offset, "the lines after this note are left out",
                             "--text")
    out: List[str] = []
    used = len(note)
    for line in lines:
        if used + len(line) + 1 > FIT_CHARS:
            break
        out.append(line)
        used += len(line) + 1
    return out + [note]


def _fit_preview(proposal: Dict[str, Any], ctx: Any, onto: Any, offset: int, count: int,
                 width: int) -> List[str]:
    """The preview of ``count`` ops from ``offset``. Over MCP the page shrinks until the text itself fits
    ``FIT_CHARS`` (the text is far shorter than the JSON, so it holds more ops); when one op does not fit, the
    lines are cut with a plain note."""
    if not ctx.mcp:
        return preview_lines(proposal, False, onto, width=width, offset=offset, limit=count)
    claims = _claims(proposal, onto)  # worked out once for every try
    lines = preview_lines(proposal, True, onto, width=width, offset=offset, limit=count, claims=claims)
    count = count or max(1, len(proposal.get("ops") or []) - offset)
    for attempt in range(FIT_ATTEMPTS + 1):
        size = len("\n".join(lines))
        if size <= FIT_CHARS or count <= 1:
            break
        count = _shrunk(count, size) if attempt < FIT_ATTEMPTS else 1  # the last try is one op
        lines = preview_lines(proposal, True, onto, width=width, offset=offset, limit=count, claims=claims)
    if len("\n".join(lines)) > FIT_CHARS:  # one op and what does not split by op are over the cap
        lines = _fit_lines(lines, str(proposal.get("id")), offset)
    return lines


def cmd_review(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """Without an id: the open proposals by priority. With an id: that proposal (a JSON copy with the C.7 marks)
    and its numbered preview, the ops paged by ``limit`` (0 = all) and ``offset``. A page holds the entries of its
    own ops only (new terms, impact ids, verdicts, edits, applied results, checks and destructive ops), so its size
    follows its ops. Over MCP the JSON page shrinks until it fits ``FIT_CHARS`` (``paging.asked`` and
    ``paging.capped_by_size`` then say so); the text forms fit their own, far shorter, text (``render_review``). A
    page that one op already takes over the cap is cut with a plain note, since a smaller limit cannot help."""
    repo = ctx.repo
    pid = args.get("id")
    if pid:
        proposal = settled_view(pipeline.load(repo, str(pid).strip()))  # an applied one shows what it created
        proposal = dict(proposal, calibration=calibration(repo, proposal))
        onto = _onto(ctx)
        total = len(proposal.get("ops") or [])
        offset = _count(args.get("offset"), 0)
        asked = _count(args.get("limit"), REVIEW_LIMIT) or max(0, total - offset)
        count = asked
        # which op each new term and impact id goes with, worked out once: needed when a page may leave ops out
        claims = _claims(proposal, onto) if (ctx.mcp or offset or asked < total) else None
        result = _one(proposal, ctx, onto, offset, count, claims)
        if not ctx.mcp:
            return result
        for attempt in range(FIT_ATTEMPTS + 1):
            size = _json_size(result)
            if size <= FIT_CHARS or count <= 1:
                break
            count = _shrunk(count, size) if attempt < FIT_ATTEMPTS else 1  # the last try is one op
            result = _one(proposal, ctx, onto, offset, count, claims, asked)
        if _json_size(result) > FIT_CHARS:  # one op and what does not split by op are over the cap
            result = _fit_json(result, str(proposal.get("id")), offset)
        return result
    onto = _onto(ctx)
    items = [_pending_item(p, onto) for p in pipeline.pending(repo)]
    limit = repo.policy.get("max_pending")
    return {
        "pending": items,
        "count": len(items),
        "max_pending": limit,
        "over_max": isinstance(limit, int) and len(items) > limit,
        "oldest_days": max([i["age_days"] for i in items]) if items else None,
    }


def render_review(result: Dict[str, Any], mode: str, ctx: Any) -> List[str]:
    if "proposal" in result:
        paging = result.get("paging") if isinstance(result.get("paging"), dict) else {}
        if mode != "text" and not paging.get("capped_by_size") and not result.get("note"):
            return list(result.get("preview") or [])  # the JSON page fits, so its compact preview does too
        # the text forms page by their own size: the page the call asked for, shrunk only as far as the text needs
        shown = result["proposal"]
        width = 0 if mode == "text" else render.WIDTH
        try:  # the result holds one page of ops; the preview needs the whole proposal
            whole = pipeline.load(ctx.repo, str(shown.get("id")))
        except Exception:  # a renderer must still print what it has
            return preview_lines(shown, ctx.mcp, _onto(ctx), width=width)
        return _fit_preview(whole, ctx, _onto(ctx), paging.get("offset") or 0,
                            paging.get("asked") or paging.get("limit") or 0, width)
    items = result.get("pending") or []
    total = (result.get("totals") or {}).get("pending", len(items))
    head = "%d open proposal%s" % (total, "" if total == 1 else "s")
    if result.get("over_max"):
        head += " (more than max_pending %s: review before proposing more)" % result.get("max_pending")
    if result.get("oldest_days"):
        head += "; oldest %s days" % result["oldest_days"]
    if not items:
        return ["no open proposals" if not total else head]
    cut = _Cutter(render.WIDTH if mode != "text" else 0)
    onto = _onto(ctx)
    lines = [head]
    groups: List[Tuple[Optional[str], List[Dict[str, Any]]]] = []
    index: Dict[Optional[str], int] = {}
    for item in items:
        src = item.get("source")
        if src not in index:
            index[src] = len(groups)
            groups.append((src, []))
        groups[index[src]][1].append(item)
    for src, members in groups:
        lines.append(source_label(src, onto))
        for item in members:
            text = "  %s %s, priority %s, %d op%s, age %sd, by %s" % (
                item.get("id"), item.get("status"), render.fmt(item.get("priority")), item.get("ops") or 0,
                "" if item.get("ops") == 1 else "s", render.fmt(item.get("age_days")), item.get("by"))
            if item.get("destructive"):
                text += ", destructive ops %s" % ranges(item["destructive"])
            lines.append(text + ": " + cut.quoted(item.get("summary")))
    lines.append("Next: %s" % render.call(ctx.mcp, "review", id=items[0].get("id")))
    return lines


# apply ---------------------------------------------------------------------------------------------------------
def _edits_arg(value: Any) -> Dict[str, Dict[str, Any]]:
    value = _json_arg(value, "edit")
    if value in (None, "", {}):
        return {}
    if not isinstance(value, dict):
        raise UsageError("edit must map op numbers to replacement ops, for example {\"3\": {...}}")
    out: Dict[str, Dict[str, Any]] = {}
    for key, op in value.items():
        if not re.match(r"^[1-9][0-9]*$", str(key).strip()):
            raise UsageError("edit: %r is not an op number" % (key,))
        if not isinstance(op, dict):
            raise UsageError("edit for op %s must be a full op object" % key)
        out[str(int(str(key).strip()))] = op
    return out


def _verdict_args(args: Dict[str, Any]) -> bool:
    return any(args.get(k) not in (None, "", [], {}) for k in ("accept", "draft", "reject", "all", "edit"))


def _applied_result(pid: str, done: Dict[str, Any], status: str, change: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = {"proposal": pid, "status": status, "change": done.get("change"), "results": done.get("results") or {},
           "ids": list(done.get("ids") or []), "richness_change": change}
    if done.get("note"):
        out["note"] = done["note"]
    return out


def cmd_apply(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """Record verdicts and apply the proposal (see the module docstring for the order of the checks).

    The whole call holds the write lock (re-entrant, so the pipeline's own locks nest): the proposal is loaded,
    checked, reviewed and committed as one step. Concurrent applies of one proposal thus run one after another; the
    first applies it and the rest load it from ``done/`` and return the ``already applied`` record."""
    with store.write_lock(ctx.repo):
        return _apply(ctx, args)


def _apply(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """``cmd_apply`` under the write lock."""
    repo = ctx.repo
    pid = str(args.get("id") or "").strip()
    proposal = pipeline.load(repo, pid)
    status = proposal.get("status")
    if status == "applied":
        ctx.preview = False  # re-applying writes nothing, so no confirm is needed
        return _applied_result(pid, pipeline.commit(repo, pid), "applied", None)
    if status in ("rejected", "superseded"):
        raise Refused("%s is %s; nothing to apply" % (pid, status))
    n_ops = len(proposal.get("ops") or [])
    edits = _edits_arg(args.get("edit"))
    by = args.get("by") or "user"
    if by not in ("user", "agent"):
        raise UsageError("by must be user or agent")
    reason = str(args.get("reason") or "")
    recorded = proposal.get("review") or {}
    if _verdict_args(args):
        verdicts: Optional[Dict[str, str]] = parse_verdicts(args.get("accept"), args.get("draft"), args.get("reject"),
                                                            args.get("all"), n_ops, edit=list(edits))
    elif recorded:
        verdicts = None  # apply the recorded review
        if reason and util.normalize_ws(reason)[:600] != (recorded.get("reason") or ""):
            # a new reason: record the same verdicts, edits and reviewer again with it, so it is not lost
            verdicts = {str(k): str(v) for k, v in (recorded.get("verdicts") or {}).items()}
            edits = {str(k): v for k, v in (recorded.get("edits") or {}).items() if isinstance(v, dict)}
            by = recorded.get("by") if recorded.get("by") in ("user", "agent") else by
    else:
        raise UsageError("%s has no verdicts yet: give accept, draft or reject (op numbers such as 1-3,5), edit, "
                         "or all" % pid)
    # a recorded review is previewed with its own reviewer; new verdicts with the caller's
    check = pipeline.commit(repo, pid, preview=True, verdicts=verdicts, edits=edits or None,
                            by=by if verdicts is not None else None)
    if ctx.preview:
        call_args = {k: args.get(k) for k in ("accept", "draft", "reject", "edit", "all", "reason")
                     if args.get(k) not in (None, "", [], {})}
        if args.get("by") not in (None, "", "user"):
            call_args["by"] = args["by"]
        shown = verdicts if verdicts is not None else dict((proposal.get("review") or {}).get("verdicts") or {})
        nxt = ctx.call("apply", **dict([("id", pid)] + sorted(call_args.items()) + [("confirm", True)]))
        if check.get("problems"):
            # never offer a confirm the preview says would be refused (nor the generic "call again with confirm")
            ctx.preview = False
            nxt = ("do not confirm: it would be refused. Fix what the problems name (for a P23: keep the risk draft by "
                   "setting attrs.status to draft with an edit, since a draft verdict leaves attrs.status as it is; "
                   "link an implemented control; raise residual; or rate the missing measures), then propose "
                   "again; or reject it: %s"
                   % ctx.call("apply", id=pid, all="reject"))
        return {
            "proposal": pid,
            "status": status,
            "verdicts": shown,
            "would_change": check.get("would_change") or [],
            "destructive": check.get("destructive") or [],
            "skipped": check.get("skipped") or [],
            "conflicts": check.get("conflicts") or [],
            "problems": check.get("problems") or [],
            "next": nxt,
        }
    if check.get("conflicts"):
        stale = check["conflicts"]
        raise Conflict("the data changed under op%s %s since %s was prepared; nothing was written. Propose the same "
                       "draft again against the current data (it replaces %s, which is marked superseded), or "
                       "reject it: %s" % ("" if len(stale) == 1 else "s", ", ".join(str(n) for n in stale), pid, pid,
                                          ctx.call("apply", id=pid, all="reject", confirm=True if ctx.mcp else None)),
                       ops=stale, proposal=pid)
    if check.get("problems"):
        found = check["problems"]
        raise Refused("the change would leave %d problem(s); nothing was written: %s"
                      % (len(found), "; ".join(str(p.get("message")) for p in found[:3])), problems=found)
    before = _richness(ctx)
    if verdicts is None:
        done = pipeline.commit(repo, pid)
        return _applied_result(pid, done, "applied", _change(before, _richness(ctx)))
    # the caller holds the write lock, so the file is the pending one loaded above and nobody sees the verdicts
    # recorded before the commit settles
    path = repo.path("%s/%s.json" % (pipeline.PENDING, pid))
    prior = store.read_bytes(path)
    reviewed = pipeline.review(repo, pid, verdicts, edits=edits or None, by=by, reason=reason)
    if reviewed.get("status") == "rejected":
        skipped = {k: {"skipped": "rejected"} for k in sorted(verdicts, key=int)}
        return _applied_result(pid, {"change": None, "results": skipped, "ids": []}, "rejected", None)
    try:
        done = pipeline.commit(repo, pid)
    except (Refused, Conflict):
        # the final graph check (or a late conflict) refused it and wrote no data: put the file back
        store.write_bytes(path, prior)
        raise
    return _applied_result(pid, done, "applied", _change(before, _richness(ctx)))


def render_apply(result: Dict[str, Any], mode: str, ctx: Any) -> List[str]:
    pid = result.get("proposal")
    if "would_change" in result:
        would = result.get("would_change") or []
        skipped = result.get("skipped") or []
        lines = ["preview of %s: %d op%s would apply, %d rejected (%s)" % (
            pid, len(would), "" if len(would) == 1 else "s", len(skipped), verdict_text(result.get("verdicts") or {}))]
        cap = len(would) if mode == "text" else MAX_LIST
        for item in would[:cap]:
            status = item.get("status") or ("proposed" if item.get("verdict") == "draft" else None)
            lines.append("  %s %s %s%s (%s)" % (item.get("n"), item.get("op"), item.get("id"),
                                                " -> %s" % status if status else "", item.get("verdict")))
        if len(would) > cap:
            rest = would[cap].get("n")
            start = int(rest) - 1 if isinstance(rest, int) and rest > 0 else cap
            lines.append("  +%d more; the ops: %s" % (len(would) - cap,
                                                      render.call(ctx.mcp, "review", id=pid, offset=start)))
        if result.get("destructive"):
            lines.append("destructive (needs an explicit yes from the user): ops %s" % ranges(result["destructive"]))
        if result.get("conflicts"):
            lines.append("conflicts: the data changed under ops %s; propose it again against the current data"
                         % ranges(result["conflicts"]))
        problems = result.get("problems") or []
        if problems:
            lines.append("refused if confirmed: the change would leave %d problem(s)" % len(problems))
            shown = len(problems) if mode == "text" else MAX_LIST  # the full text form lists them all
            for item in problems[:shown]:  # never cut: a message names what the kit accepts
                lines.append("  %s" % render.plain(item.get("message")))
            extra = render.more(len(problems), min(len(problems), shown))
            if extra:
                lines.append("  %s problems (the same call with %s lists them all)"
                             % (extra, "format=text" if ctx.mcp else "--text"))
        lines.append("Next: %s" % result.get("next"))
        return lines
    if result.get("status") == "rejected":
        return ["%s rejected: every op was rejected; nothing was applied" % pid]
    if result.get("note") == "already applied":
        lines = ["%s was applied already as %s; nothing changed" % (pid, result.get("change") or "no change")]
    else:
        lines = ["applied %s as %s" % (pid, result.get("change") or "no change")]
    results = result.get("results") or {}
    keys = sorted(results, key=lambda k: int(k) if str(k).isdigit() else 0)
    cap = len(keys) if mode == "text" else MAX_LIST
    for key in keys[:cap]:
        value = results[key]
        lines.append("  %s %s" % (key, _result_text(value) if isinstance(value, dict) else render.fmt(value)))
    extra = render.more(len(keys), min(len(keys), cap))
    if extra:
        # the rest from the first op not shown: a review page shows each op's result
        rest = keys[cap]
        start = int(rest) - 1 if str(rest).isdigit() and int(rest) > 0 else cap
        lines.append("  %s results (%s)" % (extra, render.call(ctx.mcp, "review", id=pid, offset=start)))
    change = result.get("richness_change")
    if isinstance(change, dict):
        lines.append("richness %s -> %s (%+d)" % (change.get("before"), change.get("after"), change.get("delta") or 0))
    return lines


# dupes ---------------------------------------------------------------------------------------------------------
def _keep_rank(onto: Any, nid: str) -> Tuple[Any, ...]:
    """Lower is better to keep: confirmed first, then higher trust, more links, more provenance, then the id."""
    node = onto.node(nid) or {}
    return (0 if node.get("status") == "confirmed" else 1, -TRUST_RANK.get(str(node.get("trust")), 0),
            -onto.degree(nid), -len(node.get("prov") or []), nid)


def _merge_takes_conf() -> bool:
    """Whether the record schema lets a merge op carry ``conf`` (then dupes records conf = score)."""
    return not records.check({"op": "merge", "keep": "x:a", "drop": "x:b", "conf": 0.5}, "op")


def _standing(onto: Any, nid: str) -> str:
    """``confirmed`` or ``draft``, then the trust: what makes a record the better one to keep."""
    node = onto.node(nid) or {}
    return "%s, trust %s" % ("confirmed" if node.get("status") == "confirmed" else "draft", node.get("trust"))


def merge_ops(onto: Any, pairs: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Merge ops for duplicate pairs, best score first: ``(ops, skipped)``. A node is dropped at most once and a
    dropped node is never kept; a kept node may absorb several; pairs of different kinds are skipped (link them
    instead). The keep rule (confirmed first, then higher trust) always holds: a pair whose better record would be
    dropped into a node that already keeps another one here is skipped, to be proposed again after this merge.
    Each op's reason carries the score, and ``conf`` too when the schema allows it."""
    idx = entities.index(onto)
    with_conf = _merge_takes_conf()
    dropped: Set[str] = set()
    kept: Set[str] = set()
    ops: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for pair in sorted(pairs, key=lambda p: (-float(p["score"]), p["a"], p["b"])):
        a, b = str(pair["a"]), str(pair["b"])
        if idx.kind.get(b) not in idx.same_kinds(idx.kind.get(a, "")):
            skipped.append(dict(pair, why_skipped="different kinds; link them instead of merging"))
            continue
        if a in dropped or b in dropped:
            skipped.append(dict(pair, why_skipped="one of them is merged by an earlier op"))
            continue
        if a in kept and b in kept:
            skipped.append(dict(pair, why_skipped="both keep other nodes; propose it again after this merge"))
            continue
        if a in kept or b in kept:
            keep, drop = (a, b) if a in kept else (b, a)
            if _keep_rank(onto, drop)[:2] < _keep_rank(onto, keep)[:2]:
                skipped.append(dict(pair, why_skipped="%s (%s) is the better record to keep, but %s (%s) already "
                                                      "keeps another node here; propose it again after this merge"
                                    % (drop, _standing(onto, drop), keep, _standing(onto, keep))))
                continue
        else:
            keep, drop = sorted((a, b), key=lambda n: _keep_rank(onto, n))
        op: Dict[str, Any] = {"op": "merge", "keep": keep, "drop": drop,
                              "reason": "likely duplicate (onto dupes): score %s, %s" % (render.fmt(pair["score"]),
                                                                                          pair.get("why") or "")}
        if with_conf:
            op["conf"] = float(pair["score"])
        ops.append(op)
        kept.add(keep)
        dropped.add(drop)
    return ops, skipped


def cmd_dupes(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """Likely duplicate pairs among active local nodes; ``propose`` writes a pending proposal of merge ops for the
    pairs on the page shown (``limit`` and ``offset``)."""
    onto = ctx.onto()
    kind = args.get("kind")
    key = None
    if kind:
        key = onto.registry.kind_key(str(kind)) or onto.registry.plural_alias(str(kind))
        if key is None:
            raise UsageError("unknown kind %r; known kinds: %s" % (kind, ", ".join(onto.registry.kinds())))
    pairs = []
    for pair in entities.duplicates(onto, kind=key, limit=0):
        a, b = onto.node(pair["a"]) or {}, onto.node(pair["b"]) or {}
        pairs.append({"a": pair["a"], "b": pair["b"], "score": pair["score"], "why": pair.get("why") or "",
                      "a_name": a.get("name"), "b_name": b.get("name"), "a_marks": render.flags(a),
                      "b_marks": render.flags(b)})
    result: Dict[str, Any] = {"pairs": pairs, "kind": key, "proposal": None}
    if not args.get("propose"):
        return result
    limit = args.get("limit")
    page, _total = render.page(pairs, 50 if limit is None else int(limit), int(args.get("offset") or 0))
    ops, skipped = merge_ops(onto, page)
    result["skipped"] = skipped
    if not ops:
        result["note"] = "no pairs on this page can be merged; nothing was proposed"
        return result
    draft = {"by": "agent", "source": None, "ops": ops,
             "summary": "Likely duplicates: %d merge%s (onto dupes)" % (len(ops), "" if len(ops) == 1 else "s")}
    proposal = pipeline.prepare(ctx.repo, draft, by="agent")
    result["proposal"] = _brief(proposal)
    return result


def render_dupes(result: Dict[str, Any], mode: str, ctx: Any) -> List[str]:
    pairs = result.get("pairs") or []
    total = (result.get("totals") or {}).get("pairs", len(pairs))
    cut = _Cutter(render.WIDTH if mode != "text" else 0)
    head = "%d likely duplicate pair%s%s" % (total, "" if total == 1 else "s",
                                             " of kind %s" % result["kind"] if result.get("kind") else "")
    lines = [head]
    for pair in pairs:
        a = render.mark(dict(result_marks(pair, "a"), id=pair["a"]))
        b = render.mark(dict(result_marks(pair, "b"), id=pair["b"]))
        lines.append("  %s %s %s ~ %s %s (%s)" % (render.fmt(pair.get("score")), a, cut.quoted(pair.get("a_name")),
                                                  b, cut.quoted(pair.get("b_name")), pair.get("why")))
    for pair in result.get("skipped") or []:
        lines.append("skipped %s ~ %s: %s" % (pair.get("a"), pair.get("b"), pair.get("why_skipped")))
    brief = result.get("proposal")
    if isinstance(brief, dict):
        state = "already proposed as" if brief.get("note") == "already proposed" else "proposed"
        lines.append("%s %s: %d merge%s, pending; merges need an explicit yes" % (
            state, brief.get("id"), brief.get("ops") or 0, "" if brief.get("ops") == 1 else "s"))
        lines.append("Next: %s" % render.call(ctx.mcp, "review", id=brief.get("id")))
    elif result.get("note"):
        lines.append(result["note"])
    elif pairs:
        lines.append("Next: %s" % render.call(ctx.mcp, "dupes", kind=result.get("kind"), propose=True))
    return lines


def result_marks(pair: Dict[str, Any], side: str) -> Dict[str, Any]:
    """The JSON marker flags of one side of a pair."""
    marks = pair.get("%s_marks" % side)
    return dict(marks) if isinstance(marks, dict) else {}


__all__ = [
    "cmd_apply", "cmd_dupes", "cmd_propose", "cmd_review", "merge_ops", "parse_ops", "parse_verdicts",
    "preview_lines", "ranges", "render_apply", "render_dupes", "render_propose", "render_review", "source_label",
    "undraftable", "verdict_text",
]
