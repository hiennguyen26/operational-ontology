"""Rendering helpers shared by every command: compact text, follow-up calls, markers, paging, the token-budget
engine, card fitting and the version line.

Compact text is ids plus one-line facts. Every one-line text (``trunc``, ``Cuts.cut``, ``plain``) drops the control
and bidi characters of ``CONTROL_RE`` and makes each run of whitespace (newlines included) one space, so stored text
can neither start a line of its own (a forged ``Next:`` line) nor drive the terminal. Long texts are cut at ``WIDTH`` characters (``Cuts`` counts them, and a
closing line names the call that returns the whole text). A list cut short ends with ``+N more``, counting only what
follows the page. ``false``, ``0`` and ``null`` print as themselves. ``mcp`` switches follow-up calls between MCP
tool calls (``onto_get id=X full=true``) and CLI commands (``onto get X --full``).

The budget engine (``select``) fills a token budget (characters / 4) with units of text in order. A section stops
at its first unit that does not fit; an essential unit that does not fit stops everything after it. A unit is taken
only when the output still fits with every unit not yet taken listed as left out, so the result always fits unless
the head alone does not. The footer names what was left out and the exact call that returns it.
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

WIDTH = 100
CHARS_PER_TOKEN = 4
CARD_CHARS = 1000
MAX_SIMILAR_LISTED = 3  # similar targets named in a footer group; the rest count as "+N similar"
MAX_CALLS = 3
FLOORS = (3, 1, 0)  # items a card list keeps in each round of shrinking
POSITIONAL = ("id", "subject", "task", "text", "q", "from", "to", "action")


# C0 and C1 controls (not tab and newline, which whitespace folding handles), DEL and the bidi controls.
CONTROL_RE = re.compile("[\x00-\x08\x0e-\x1b\x7f-\x84\x86-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
# Whitespace that moves the cursor off the line (carriage return, vertical tab, form feed, the separators).
BREAK_RE = re.compile("[\r\x0b\x0c\x1c-\x1f\x85\u2028\u2029]")


def plain(text: Any, width: int = 0) -> str:
    """Text as one safe line: ``CONTROL_RE`` characters removed and every run of whitespace (newlines included) made
    one space; cut at ``width`` with ``...`` when ``width`` is given."""
    flat = " ".join(CONTROL_RE.sub("", "" if text is None else str(text)).split())
    return flat if not width or len(flat) <= width else flat[: width - 3].rstrip() + "..."


def plain_block(text: Any) -> str:
    """Multi-line text (a source chunk inside its fences) with ``CONTROL_RE`` characters removed and the
    line-moving whitespace of ``BREAK_RE`` made a space; newlines and tabs stay."""
    return BREAK_RE.sub(" ", CONTROL_RE.sub("", "" if text is None else str(text)))


def trunc(text: Any, width: int = WIDTH) -> str:
    """One safe line (``plain``) of at most ``width`` characters, cut with ``...``."""
    return plain(text or "", width)


class Cuts(object):
    """Counts the texts cut at ``WIDTH``, for the closing line that names the whole-text call."""

    def __init__(self) -> None:
        self.count = 0

    def cut(self, text: Any, width: int = WIDTH) -> str:
        flat = plain(text or "")
        if len(flat) > width:
            self.count += 1
        return trunc(flat, width)

    def whole_text_line(self, node_id: str, mcp: bool) -> List[str]:
        """``(N text(s) cut at 100 characters; the whole text: <call>)`` when anything was cut."""
        if not self.count:
            return []
        return ["  (%d text(s) cut at %d characters; the whole text: %s)"
                % (self.count, WIDTH, call(mcp, "get", id=node_id, full=True))]


def quote(text: str) -> str:
    """A CLI argument, JSON-quoted unless it is plain."""
    return text if text and all(c.isalnum() or c in ":._/-@[],=" for c in text) else json.dumps(text, ensure_ascii=False)


def call(mcp: bool, tool: str, **args: Any) -> str:
    """A follow-up call in the caller's words: ``onto_get id=X full=true`` over MCP, ``onto get X --full`` on the
    CLI. Leading arguments named ``id``, ``subject``, ``task``, ``text``, ``q``, ``from``, ``to`` or ``action`` are
    CLI positionals; ``None`` values are left out; on the CLI ``True`` is ``--flag`` and ``False`` is ``--no-flag``
    (so a follow-up that turns off a switch that is on by default, such as ``drafts``, keeps its meaning)."""
    items = [(k, v) for k, v in args.items() if v is not None]
    if mcp:
        parts = ["onto_%s" % tool]
        for key, value in items:
            if isinstance(value, bool):
                value = "true" if value else "false"
            elif isinstance(value, (list, tuple, dict)):
                value = json.dumps(list(value) if isinstance(value, tuple) else value, ensure_ascii=False,
                                   separators=(",", ":"))
            elif isinstance(value, str) and (not value or any(c.isspace() or c in "\"'" for c in value)):
                value = json.dumps(value, ensure_ascii=False)
            parts.append("%s=%s" % (key, value))
        return " ".join(parts)
    parts = ["onto", tool]
    leading = True
    for key, value in items:
        if leading and key in POSITIONAL and not isinstance(value, bool):
            parts.append(quote(str(value)))
            continue
        leading = False
        flag = "--%s" % key.replace("_", "-")
        if value is True:
            parts.append(flag)
        elif value is False:
            parts.append("--no-%s" % key.replace("_", "-"))  # every CLI switch has its --no- form
        elif isinstance(value, (list, tuple)) and any("," in str(v) for v in value):
            for v in value:  # an item with a comma gets a flag of its own (list flags repeat on the CLI)
                parts.extend([flag, quote(str(v))])
        elif isinstance(value, (list, tuple)):
            parts.extend([flag, quote(",".join(str(v) for v in value))])
        elif isinstance(value, dict):
            parts.extend([flag, quote(json.dumps(value, ensure_ascii=False, separators=(",", ":")))])
        else:
            parts.extend([flag, quote(str(value))])
    return " ".join(parts)


def more(total: int, shown: int, offset: int = 0) -> str:
    """``+N more`` for the items after this page (``total - offset - shown``), else ``""``."""
    left = int(total) - int(offset) - int(shown)
    return "+%d more" % left if left > 0 else ""


def flags(rec: Optional[Dict[str, Any]]) -> Dict[str, bool]:
    """The JSON marker flags of a record: ``untrusted``, ``draft`` and ``archived`` (only the true ones)."""
    out: Dict[str, bool] = {}
    if not rec:
        return out
    if rec.get("untrusted") is True or rec.get("trust") == "untrusted":
        out["untrusted"] = True
    if rec.get("draft") is True or rec.get("status") == "proposed":
        out["draft"] = True
    if rec.get("archived") is True or rec.get("status") == "archived":
        out["archived"] = True
    return out


def mark(item: Dict[str, Any], text: Optional[str] = None) -> str:
    """``[untrusted] <id> (draft)`` or ``<id> (archived)``: the item's id (or ``text``) with its markers. The item
    may carry the JSON flags or a record's ``trust`` and ``status``."""
    f = flags(item)
    out = text if text is not None else str(item.get("id") or "")
    if f.get("untrusted"):
        out = "[untrusted] " + out
    if f.get("archived"):
        out += " (archived)"
    elif f.get("draft"):
        out += " (draft)"
    return out


def fmt(v: Any) -> str:
    """A fact value: ``false``, ``0`` and ``null`` print as JSON literals, lists join with ``, ``, objects as JSON."""
    if v is None or isinstance(v, bool):
        return json.dumps(v)
    if isinstance(v, (int, float)):
        return json.dumps(v)
    if isinstance(v, (list, tuple)):
        return _fmt_list(v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(v)


def _fmt_list(v: Any) -> str:
    """``fmt`` of a list: its items joined with ``, `` (a string as it is, a nested list as its own join), walked
    with a stack and not recursion, so a list nested as deep as the kit reads (``util.JSON_MAX_DEPTH``) formats the
    same on every Python version."""
    stack: List[Tuple[Any, List[str]]] = [(iter(v), [])]
    while True:
        items, parts = stack[-1]
        x = next(items, _END)
        if x is _END:
            stack.pop()
            joined = ", ".join(parts)
            if not stack:
                return joined
            stack[-1][1].append(joined)
        elif isinstance(x, (list, tuple)):
            stack.append((iter(x), []))
        else:
            parts.append(x if isinstance(x, str) else fmt(x))


_END = object()


def page(lst: Sequence[Any], limit: int, offset: int = 0) -> Tuple[List[Any], int]:
    """``(the page, total)``: ``limit`` items from ``offset`` (``limit`` 0 means no cap)."""
    items = list(lst)
    offset = max(0, int(offset or 0))
    shown = items[offset: offset + limit] if limit and limit > 0 else items[offset:]
    return shown, len(items)


def collapse_relations(rel_groups: Dict[str, List[Dict[str, Any]]],
                       totals: Optional[Dict[str, int]] = None) -> Tuple["OrderedDict[str, List[Dict[str, Any]]]",
                                                                          Dict[str, int]]:
    """One row per (label, id): parallel edges and repeats collapse into the first row, which gets ``count``.
    Returns ``(groups, relation_totals)``; a total counts distinct ids (``totals`` gives the raw counts when the
    groups are already paged)."""
    groups: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    out_totals: Dict[str, int] = {}
    for label, items in rel_groups.items():
        merged: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        for item in items:
            key = str(item.get("id"))
            if key in merged:
                merged[key]["count"] = merged[key].get("count", 1) + 1
                continue
            merged[key] = dict(item)
        groups[label] = list(merged.values())
        raw = (totals or {}).get(label, len(items))
        out_totals[label] = raw - (len(items) - len(merged))
    return groups, out_totals


# the budget engine ---------------------------------------------------------------------------------------------
class Unit(object):
    """A piece of budgeted output: its text lines, its JSON item, the group it is left out under, the call that
    returns that group, whether it is essential, and how many items it holds (for the left-out counts)."""

    def __init__(self, section: str, lines: List[str], item: Any = None, group: str = "", follow: str = "",
                 essential: bool = False, count: int = 1) -> None:
        self.section = section
        self.lines = list(lines)
        self.item = item
        self.group = group
        self.follow = follow
        self.essential = essential
        self.count = count

    def size(self) -> int:
        return chars(self.lines)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Unit(%r, %d lines)" % (self.section, len(self.lines))


def chars(lines: Sequence[str]) -> int:
    """Characters of ``lines`` joined by newlines, counting one newline per line."""
    return sum(len(line) + 1 for line in lines)


def tokens(text_or_lines: Union[str, Sequence[str]]) -> int:
    """The token estimate: characters / 4, rounded up."""
    n = len(text_or_lines) if isinstance(text_or_lines, str) else chars(text_or_lines)
    return (n + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def omitted_groups(units: Sequence[Unit], chosen: Sequence[Unit], everything: str = "") -> List[Dict[str, Any]]:
    """The units left out, grouped in order: ``[{what, count, calls, call, similar}]``. ``call`` is the group's
    first distinct follow-up call (the everything call when it has none) and ``similar`` counts its other distinct
    calls, so a footer can say ``(<call>, +2 similar)`` instead of falling back to the everything call."""
    picked = {id(u) for u in chosen}
    groups: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    for unit in units:
        if id(unit) in picked:
            continue
        what = unit.group or unit.section
        g = groups.setdefault(what, {"what": what, "count": 0, "calls": []})
        g["count"] += unit.count
        if unit.follow and unit.follow not in g["calls"]:
            g["calls"].append(unit.follow)
    for g in groups.values():
        g["call"] = g["calls"][0] if g["calls"] else everything
        g["similar"] = max(0, len(g["calls"]) - 1)
    return list(groups.values())


def _split_call(call: str) -> Tuple[str, str]:
    """``(stem, target)`` of a follow-up call: ``onto_get id=`` and ``e:x``, or ``onto get `` and ``e:x``."""
    cut = max(call.rfind("="), call.rfind(" "))
    return (call[:cut + 1], call[cut + 1:]) if cut >= 0 else ("", call)


def _similar_text(g: Dict[str, Any], shown: str) -> str:
    """The group's call, then its similar calls: a target the output names nowhere (a bridge's edge id) is listed,
    up to ``MAX_SIMILAR_LISTED``, so it can be followed; the rest count as ``+N similar``."""
    calls = list(g.get("calls") or [])
    call = g.get("call") or ""
    similar = int(g.get("similar") or 0)
    if not similar:
        return call
    stem = _split_call(call)[0]
    listed = []
    for other in calls:
        if other == call or len(listed) >= MAX_SIMILAR_LISTED:
            continue
        o_stem, target = _split_call(other)
        if stem and o_stem == stem and target and '"' not in target and target not in shown:
            listed.append(target)
    text = call + (", also %s" % ", ".join(listed) if listed else "")
    rest = similar - len(listed)
    return text + (", +%d similar" % rest if rest > 0 else "")


def left_out_line(omitted: List[Dict[str, Any]], everything: str, budget: int, shown: str = "") -> List[str]:
    """``left out to fit N tokens: 4 related (<call>); 3 goals (<call>, +2 similar); 9 more (<everything>)``. At most
    three groups with a call of their own are named, each call once; every other group (those whose call is the
    everything call, a call already named, or past the third) folds into one trailing ``N more``, so no call is ever
    printed twice. A similar call whose target ``shown`` (the output so far) names nowhere is listed:
    ``3 bridges (onto_get id=e:a, also e:b, e:c)``."""
    if not omitted:
        return []
    parts = []
    named = set()
    rest = 0
    for g in omitted:
        call = g.get("call") or ""
        if call and call != everything and call not in named and len(parts) < MAX_CALLS:
            parts.append("%d %s (%s)" % (g["count"], g["what"], _similar_text(g, shown)))
            named.add(call)
        else:
            rest += g["count"]
    if rest:
        parts.append("%d more (%s)" % (rest, everything) if everything else "%d more" % rest)
    return ["left out to fit %d tokens: %s" % (budget, "; ".join(parts))]


def select(head: List[str], units: List[Unit], tail: List[str], budget: int, everything_call: str,
           mcp: bool = False) -> Dict[str, Any]:
    """Fill ``budget`` tokens (0 = no cap) with ``head``, then ``units`` in order, then the footer and ``tail``.

    Returns ``{lines, omitted, budget: {tokens, chars, estimated_tokens}, chosen}`` where ``chosen`` holds the units
    taken, in order."""
    limit = budget * CHARS_PER_TOKEN if budget and budget > 0 else 0
    used = chars(head) + chars(tail)
    chosen: List[Unit] = []
    taken = set()
    stopped = set()
    halted = False
    shown = list(head) + list(tail)
    for unit in units:
        if halted or unit.section in stopped:
            continue
        if limit:
            left = [u for u in units if u is not unit and id(u) not in taken]
            foot = left_out_line(omitted_groups(left, [], everything_call), everything_call, budget,
                                 "\n".join(shown + unit.lines))
            if used + unit.size() + chars(foot) > limit:
                stopped.add(unit.section)
                halted = unit.essential
                continue
        chosen.append(unit)
        taken.add(id(unit))
        used += unit.size()
        shown.extend(unit.lines)
    omitted = omitted_groups(units, chosen, everything_call) if limit else []
    foot = left_out_line(omitted, everything_call, budget, "\n".join(shown))
    if limit and omitted and used + chars(foot) > limit:
        foot = ["left out to fit %d tokens: %d items (%s)" % (budget, sum(g["count"] for g in omitted),
                                                              everything_call)]
    lines = list(head)
    for unit in chosen:
        lines.extend(unit.lines)
    lines += foot + list(tail)
    text_chars = chars(lines) - (1 if lines else 0)
    return {
        "lines": lines,
        "omitted": [dict({"what": g["what"], "count": g["count"], "call": g["call"]},
                         **({"similar": g["similar"], "calls": list(g["calls"])} if g.get("similar") else {}))
                    for g in omitted],
        "budget": {"tokens": budget, "chars": text_chars, "estimated_tokens": (text_chars + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN},
        "chosen": chosen,
    }


# card fitting --------------------------------------------------------------------------------------------------
class Line(object):
    """A card line: fixed text, or a prefix and a list of items shown while they fit (``+N more`` for the rest).
    ``rank`` orders the lists when they must shrink: rank 1 is the card's point and shrinks last. ``more`` counts
    items that are never listed (they join the ``+N more``)."""

    def __init__(self, prefix: str, items: Optional[List[str]] = None, rank: int = 1, empty: str = "none",
                 more: int = 0) -> None:
        self.prefix = prefix
        self.items = items
        self.shown = len(items or [])
        self.rank = rank
        self.empty = empty
        self.more = more

    def render(self) -> str:
        if self.items is None:
            return self.prefix
        if not self.items:
            return self.prefix + self.empty
        text = self.prefix + "; ".join(self.items[: self.shown])
        left = len(self.items) - self.shown + self.more
        if left > 0:
            text += "%s+%d more" % ("; " if self.shown else "", left)
        return text


_Line = Line


def fit_card(lines: Sequence[Union[Line, str]], limit: int = CARD_CHARS) -> str:
    """Render ``lines`` in at most ``limit`` characters. In rounds (``FLOORS``), each list keeps at least that many
    items while another can still shrink: the least important list (highest ``rank``) gives up its last shown item
    first, the longest of those on a tie. A last resort cuts the text."""
    parts = [line if isinstance(line, Line) else Line(str(line)) for line in lines]

    def text() -> str:
        return "\n".join(line.render() for line in parts)

    for floor in FLOORS:
        while len(text()) > limit:
            lists = [line for line in parts if line.items and line.shown > floor]
            if not lists:
                break
            max(lists, key=lambda line: (line.rank, len(line.render()))).shown -= 1
    body = text()
    return body if len(body) <= limit else body[: limit - 3].rstrip() + "..."


def follow_line(follow: Sequence[Tuple[str, Dict[str, Any]]], mcp: bool = True) -> str:
    """``Next: <call>; <call>`` in the caller's words."""
    return "Next: " + "; ".join(call(mcp, name, **args) for name, args in follow)


# the version line ----------------------------------------------------------------------------------------------
def version_line(stamp: Optional[Dict[str, Any]]) -> str:
    """``<ns> <version> [+ N changes after it] | richness 41 working (+12 since 09-21) | imports: garden v1 a1b2c3d
    ok [| kit 0.2.0, topic written by 0.3.0]``; absent parts are dropped, and ``mismatch`` replaces ``ok`` for a
    failed pin."""
    if not stamp:
        return "no topic"
    head = "%s %s" % (stamp.get("ns") or "?", stamp.get("version") or "unreleased")
    after = stamp.get("changes_after")
    if stamp.get("version") not in (None, "unreleased") and stamp.get("matches_release") is False:
        if isinstance(after, int) and after > 0:
            head += " + %d change%s after it" % (after, "" if after == 1 else "s")
        else:  # the data differs from the release, but no logged change says how (unknown, or a hand edit)
            head += " + changes after it"
    parts = [head]
    rich = stamp.get("richness")
    if isinstance(rich, dict) and rich.get("score") is not None:
        text = "richness %s %s" % (rich.get("score"), rich.get("band") or "")
        text = text.rstrip()
        if rich.get("change_text"):
            text += " (%s)" % rich["change_text"]
        parts.append(text)
    pins = stamp.get("imports") or []
    if pins:
        parts.append("imports: " + ", ".join(
            "%s %s %s %s" % (p.get("ns"), p.get("ref") or "-", p.get("commit7") or "-", "ok" if p.get("ok") else
                             "mismatch")
            for p in pins))
    if stamp.get("kit_mismatch"):
        parts.append("kit %s, topic written by %s" % (stamp.get("kit"), stamp.get("repo_kit")))
    return " | ".join(parts)
