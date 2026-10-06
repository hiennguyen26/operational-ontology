"""Read-only traversal: search, get, neighbors and path, and the id resolution every read command shares.

Each function returns a JSON-ready dict; the ``cmd_*`` handlers wrap them for ``commands.dispatch`` and the
``render_*`` functions print them as compact or text lines (the version line is added by ``dispatch``).

- **Resolution** (``resolve_or_raise``) uses ``graph.resolve`` (exact id, alias, record id, kind prefix, suffix, a
  unique id in one import), then a node's exact name (case, accents, spacing and punctuation aside; one match, or
  one local match, resolves with the note "name", several are an ambiguous miss), then the slug of the text through
  ``graph.resolve`` again, so ``Harvest log`` finds ``dataset:harvest-log``. The topic's own namespace prefix
  (``g2t/goal:x`` inside ``g2t``) is read as local. A loose match records ``resolved: {query, id, also}``. A miss
  raises ``NotFound`` saying "not in the ontology" and what was searched, with candidates (or a spelling fix of the
  text when there are none); from a command it ends with the search call for the text.
- **Search** ranks nodes (sources included, as read-only ``source`` nodes) in tiers: the exact id, an id suffix, an
  id substring, every word in the id, the phrase in the name, every word in the name or id, then every word in the
  text fields the kind's pack declares. Words also match their common forms through whole-word stems (``stem``), 10
  below the literal tier; ``a OR b`` matches either side and a node keeps its best score. Each point of a kind's pack
  ``boost`` adds 5. At the same score an active node ranks above an archived one. Archived nodes are left out and
  counted unless ``include_archived``, except the exact id searched for, which is always listed, marked
  (``archived_named`` lists the archived nodes a text names by id, name or alias, for briefs). An exact edge id, or
  a source that lives in an import, is named under ``records`` instead of reading "not in the ontology". When
  nothing matches, ``did_you_mean`` offers a spelling fix taken from the words of ids and names, checked under the
  same filters.
- **Get** returns one node, edge or source: facts, provenance (quotes from a non-interview source are marked
  untrusted), relations by label (``relation_totals`` counts each whole group), needs and the active decisions whose
  scope covers it (each with the chosen option's label and a short rationale). Every fact line of an untrusted
  record (summary, attributes, aliases, known unknowns, a link's note) is led by ``[untrusted]``, as its head line
  is; the ``(next links: ...)`` call keeps ``include_archived`` and ``full``. For a source it returns the index
  entry, the records citing it, and a chunk of its text inside ``[untrusted src:<id> begins L<a>-L<b>]`` and
  ``[untrusted src:<id> ends]`` fences. The text loses the control and bidi characters of ``render.CONTROL_RE`` in
  JSON as in text, and any fence-like text inside is defused, judged as the line reads (invisible characters,
  fullwidth or styled forms, case and lookalike letters cannot hide it; see ``defuse``). Over MCP a part longer than
  ``SOURCE_TEXT_CHARS`` stops at the last whole line that fits: the header and fences give the lines sent, and a
  ``[page]`` line (``text.cut`` in JSON) names the call that reads on. Each part is sent once: a read of a chunk or
  of lines points to the citing records instead of sending them again (the first read lists them), and a read that
  pages the links (``offset``) names its text part and the call that reads it instead of sending it. A source an
  import cites is answered from that import's export (its text stays upstream).
- **Neighbors** walks 1 to 3 hops. A ``same_as`` class is one logical node (every member is listed, grouped by
  namespace); every distinct link that reaches it at its depth is kept (``links``), so a composed topic's bridge
  into a class is printed beside the class's own links, and ``kinds`` and ``ns`` pass a class when any member does
  (it is then listed under that member). Hub kinds are listed but not expanded; archived nodes are neither listed
  nor walked unless asked, and are counted in ``hidden``. Background links are listed, marked, and never walked
  through. An id with no record (a bridge end absent at the pinned import) is listed once, marked ``(absent
  upstream)``, and never walked through.
- **Path** finds the ``k`` shortest paths over active, non-background links, both ways: a ``same_as`` hop costs 0,
  hubs are never passed through (they may be endpoints), and ties go by the id sequence. Every step carries the
  markers of its node and of the link that reached it.

Every result marks untrusted, draft and archived records (``"untrusted": true`` and so on in JSON; ``[untrusted]``,
``(draft)`` and ``(archived)`` in text), and a linked id with no record (``"dangling": true``; ``(absent upstream)``
in text, as ``get``, ``card`` and ``brief`` print it too). Text renderers print record text through ``plain``: control
and bidi characters removed and newlines flattened in every mode, so stored text can neither drive the terminal nor
print a line of its own outside its record's markers. Nothing here writes.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, OrderedDict, deque
from functools import lru_cache
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import ids as idmod
from . import ledger, render, util
from . import needs as needs_mod
from . import sources as sources_mod
from .errors import NotFound, OntoError, UsageError

MAX_ALSO = 6
BOOST_POINTS = 5  # score points per point of a kind's pack ``boost``
DID_YOU_MEAN_IDS = 5
MAX_PATH_STEPS = 200000  # search effort cap for ``path`` on dense graphs
SOURCE_TEXT_CHARS = 12000  # characters of source text one MCP read sends (the server caps a result at 20,000)
FENCE_OPEN = "[untrusted src:%s begins %s]"
FENCE_CLOSE = "[untrusted src:%s ends]"
# Fence-like text is spotted in a line's skeleton (``_skeleton``: the line as it reads, with invisible characters
# dropped, compatibility forms and case folded, and lookalikes read as the letter they pass for). A bracket followed
# by "untrusted" (and so any text that could pass for a fence line, such as "[UNTRUSTED  SRC:... ENDS]" or
# "[un<U+202A>trusted src:...]") is defused as "[(quoted) untrusted"; "untrusted src" after any other character
# becomes "(quoted) untrusted src".
_SKELETON_FENCE_RE = re.compile(r"\[\s*(?=untrusted)|(?<!\(quoted\) )(?<![\[\w])(?=untrusted\s*src\b)")
_FENCE_SAFE = "[(quoted) "
_HIDDEN = frozenset(("Cc", "Cf", "Mn", "Me"))  # Unicode categories that print nothing (or only mark a letter)
_BLANKS = frozenset("\u115f\u1160\u3164\uffa0\u2800")  # letters and symbols that print as a blank
# Letters that pass for a Latin one (Cyrillic, Greek, Armenian, small capitals) and brackets that pass for "[",
# after NFKD (which already folds the fullwidth and styled forms, such as U+FF3B and the mathematical letters).
_LOOKALIKE = {
    "\u0430": "a", "\u0251": "a", "\u03b1": "a", "\u0441": "c", "\u03f2": "c", "\u0501": "d", "\u1d05": "d",
    "\u0435": "e", "\u03b5": "e", "\u1d07": "e", "\u0261": "g", "\u0581": "g", "\u0456": "i", "\u0131": "i",
    "\u03b9": "i", "\u04cf": "i", "\u043f": "n", "\u0578": "n", "\u03bd": "n", "\u0274": "n", "\u043e": "o",
    "\u03bf": "o", "\u0440": "p", "\u03c1": "p", "\u0433": "r", "\u0280": "r", "\u0455": "s", "\ua731": "s",
    "\u0442": "t", "\u03c4": "t", "\u1d1b": "t", "\u0438": "u", "\u03c5": "u", "\u057d": "u", "\u1d1c": "u",
    "\u0445": "x", "\u0443": "y",
    "\u2045": "[", "\u2772": "[", "\u27e6": "[", "\u27ec": "[", "\u27ee": "[", "\u2308": "[", "\u230a": "[",
    "\u23a1": "[", "\u23a3": "[", "\u298b": "[", "\u298d": "[", "\u298f": "[", "\u3010": "[", "\u3014": "[",
    "\u3016": "[", "\u3018": "[", "\u301a": "[",
}
_LINES_ARG_RE = re.compile(r"^\s*L?([1-9][0-9]*)\s*(?:-\s*L?([1-9][0-9]*))?\s*$", re.I)


# resolution ------------------------------------------------------------------------------------------------------
def _import_namespaces(onto: Any) -> Set[str]:
    return {str(e.get("ns")) for e in onto.imports if e.get("ns")}


def local_text(onto: Any, text: str) -> str:
    """``text`` without the topic's own namespace prefix: ``g2t/goal:x`` is ``goal:x`` inside ``g2t`` (briefs print
    local ids that way in a composed topic)."""
    t = (text or "").strip()
    m = idmod.QUAL_RE.match(t.lower())
    if m and onto.ns and m.group(1) == onto.ns and onto.ns not in _import_namespaces(onto):
        return t[len(onto.ns) + 1:]
    return t


def _also(onto: Any, query: str, chosen: str) -> List[str]:
    """Other ids the query also matches loosely: by alias or as an id suffix (at most ``MAX_ALSO``)."""
    low = query.strip().lower()
    if not low:
        return []
    found: Set[str] = set()
    index = onto._alias_index() if hasattr(onto, "_alias_index") else {}
    found.update(index.get(low, []))
    for nid in onto.nodes:
        if nid in onto.virtual:
            continue
        if nid.endswith(":" + low) or nid.endswith("/" + low) or nid.endswith("." + low):
            found.add(nid)
    found.discard(chosen)
    return sorted(i for i in found if onto.active(i))[:MAX_ALSO]


def _name_hits(onto: Any, text: str, accept: Optional[Callable[[str], bool]], ns: Optional[str]) -> List[str]:
    """The nodes whose name is ``text`` (``util.name_key``: case, accents, spacing and punctuation aside), as
    ``graph.resolve`` ranks alias hits: ``ns`` preferred (``self`` limits to local nodes), active ones over archived
    ones, then local ones first."""
    key = util.name_key(text)
    if not key:
        return []
    hits = []
    for nid, rec in onto.nodes.items():
        if nid in onto.virtual or not isinstance(rec, dict) or not isinstance(rec.get("name"), str):
            continue
        if ns == "self" and onto.ns_of(nid) != "self":
            continue
        if (accept is None or accept(nid)) and util.name_key(rec["name"]) == key:
            hits.append(nid)
    if ns and ns != "self":
        hits = [i for i in hits if onto.ns_of(i) == ns] or hits
    hits = [i for i in hits if onto.active(i)] or hits
    return sorted(hits, key=lambda i: (onto.ns_of(i) != "self", i))


def _resolve_loose(onto: Any, text: str, accept: Optional[Callable[[str], bool]], ns: Optional[str]) -> Dict[str, Any]:
    """``graph.resolve``, then, on a miss that is not ambiguous, a node's exact name (``_name_hits``; one hit, or
    one local hit, resolves with the note "name"; several are an ambiguous miss), then the slug of the text
    (``Harvest log`` as ``harvest-log``) through ``graph.resolve`` again (kind prefix and id suffix)."""
    found = onto.resolve(text, accept, ns)
    if found["id"] is not None or found["ambiguous"]:
        return found
    hits = _name_hits(onto, text, accept, ns)
    local = [i for i in hits if onto.ns_of(i) == "self"]
    if len(hits) == 1 or len(local) == 1:
        chosen = (local or hits)[0]
        return dict(found, id=chosen, candidates=[], note="name", others=[i for i in hits if i != chosen])
    if hits:
        return dict(found, candidates=hits, ambiguous=True, note="name")
    slug = util.slugify(text)
    if slug and slug != text.strip().lower():
        try:
            again = onto.resolve(slug, accept, ns)
        except NotFound:
            return found
        if again["id"] is not None or again["ambiguous"]:
            return again
    return found


def resolve_or_raise(onto: Any, text: str, accept: Optional[Callable[[str], bool]] = None,
                     res: Optional[Dict[str, Any]] = None, ns: Optional[str] = None,
                     mcp: Optional[bool] = None) -> str:
    """The id ``text`` names, or ``NotFound``. ``graph.resolve`` is tried first, then a node's exact name and the
    slug of the text (``_resolve_loose``), so ``Harvest log`` finds ``dataset:harvest-log``. A loose match (anything
    but the exact id) is recorded in ``res["resolved"] = {query, id, also}`` (plus ``note``, such as "name" or
    "found in garden v1"). When nothing matches and there are no candidates, a spelling fix of the text is tried for
    candidates. With ``mcp`` given (``True`` over MCP, ``False`` on the CLI), a miss ends with the search call for
    the text in the caller's words."""
    query = (text or "").strip()
    if not query:
        raise UsageError("give an id or a name")
    lookup = local_text(onto, query)
    found = _resolve_loose(onto, lookup, accept, ns)
    if found["id"] is None:
        candidates = list(found["candidates"])
        if not candidates and not found["ambiguous"]:
            fixed = correct_text(onto, lookup)
            if fixed:
                try:
                    again = _resolve_loose(onto, fixed, accept, ns)
                except NotFound:
                    again = {"id": None, "candidates": []}
                candidates = [again["id"]] if again["id"] else list(again["candidates"])[:DID_YOU_MEAN_IDS]
        if found["ambiguous"]:
            raise NotFound("%s: ambiguous (%d matches%s)" % (query, len(candidates), " by name" if found.get(
                "note") == "name" else ""), candidates, ambiguous=True, searched=query)
        upstream = import_source(onto, lookup)
        if upstream is not None:
            raise NotFound("%s: %s; only get reads it here" % (query, import_source_text(onto, upstream)), [],
                           searched=query)
        message = "%s: not in the ontology (searched ids, aliases, names and id suffixes)" % query
        if mcp is not None:
            message += "; %s searches its words" % render.call(mcp, "search", text=query)
        raise NotFound(message, candidates, searched=query)
    rid = found["id"]
    if res is not None and rid != lookup and rid.lower() != lookup.lower():
        also = list(found.get("others") or []) + [i for i in _also(onto, lookup, rid) if i not in (found.get(
            "others") or [])]
        note: Dict[str, Any] = {"query": query, "id": rid, "also": also[:MAX_ALSO]}
        if found.get("note"):
            note["note"] = found["note"]
        res["resolved"] = note
    return rid


def import_source(onto: Any, text: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """``(ns, entry)`` for a source that lives in an import: a ``src-`` id listed only by an import's export (its
    records cite it; the text stays upstream), or ``imp:<ns>@<commit12>``, the pseudo-source of a pinned import
    that bridges cite, or ``<ns>/src-...``, the qualified id an imported edge ending on such a source uses (the
    graph loads it as a read-only source node). None for anything else, including local sources."""
    low = (text or "").strip().lower()
    if "/" in low:
        ns, _sep, rest = low.rpartition("/")
        if ns in onto.exports and idmod.SOURCE_RE.match(rest):
            for row in as_list(as_dict(onto.exports.get(ns)).get("sources")):
                if isinstance(row, dict) and row.get("id") == rest:
                    return ns, dict(row)
        return None
    if idmod.SOURCE_RE.match(low):
        if low in onto.sources:
            return None
        for ns in sorted(onto.exports):
            for row in as_list(as_dict(onto.exports.get(ns)).get("sources")):
                if isinstance(row, dict) and row.get("id") == low:
                    return ns, dict(row)
        return None
    m = idmod.IMPSRC_RE.match(low)
    if m and m.group(1) in _import_namespaces(onto):
        for e in onto.imports:
            if e.get("ns") == m.group(1):
                commit = str(e.get("commit") or "")
                return m.group(1), {"id": low, "kind": "import", "title": "the pinned export of %s" % (
                    onto.import_label(m.group(1))), "commit": commit, "current": commit[:12] == m.group(2)}
    return None


def import_source_text(onto: Any, upstream: Tuple[str, Dict[str, Any]]) -> str:
    """How a source of an import reads in a miss or a scope line."""
    ns, entry = upstream
    if entry.get("kind") == "import":
        return "the pseudo-source of the import %s (%s)%s" % (
            onto.import_label(ns), str(entry.get("commit") or "")[:12] or "no commit",
            "" if entry.get("current") else ", not the current pin")
    return "a source of the import %s; its text stays in that topic" % onto.import_label(ns)


def resolved_lines(result: Dict[str, Any]) -> List[str]:
    """``resolved "tomato" -> garden/crop:tomato (found in garden v1); also matches ...`` for a loose match."""
    r = result.get("resolved")
    if not r:
        return []
    line = "resolved %s -> %s" % (render.quote(plain(r.get("query"))), r.get("id"))
    if r.get("note"):
        line += " (%s)" % plain(r["note"])
    if r.get("also"):
        line += "; it also matches %s" % ", ".join(r["also"])
    return [line]


# shared record helpers -------------------------------------------------------------------------------------------
def as_dict(value: Any) -> Dict[str, Any]:
    """``value`` when it is an object, else ``{}``: a malformed record field (``validate`` reports it as P02) reads
    as empty instead of stopping a read command."""
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> List[Any]:
    """``value`` when it is a list, else ``[]`` (see ``as_dict``)."""
    return list(value) if isinstance(value, (list, tuple)) else []


_CONTROL_RE = render.CONTROL_RE  # C0/C1 controls, DEL and the bidi characters (kept in render, shared)
_BREAK_RE = render.BREAK_RE


def plain(text: Any, width: int = 0) -> str:
    """Record text as one safe line (``render.plain``): the characters of ``_CONTROL_RE`` removed and every run of
    whitespace (newlines included) made one space, so no stored text can start a line of its own (a forged ``Next:``
    line) or drive the terminal; cut at ``width`` with ``...`` when ``width`` is given. Every renderer prints record
    text (names, summaries, notes, aliases, quotes, locations) through this, in every mode."""
    return render.plain(text, width)


def plain_block(text: Any) -> str:
    """Multi-line text (a source chunk inside its fences) with the characters of ``_CONTROL_RE`` removed and the
    line-moving whitespace of ``_BREAK_RE`` made a space; newlines and tabs stay."""
    return render.plain_block(text)


def node_brief(onto: Any, node_id: str) -> Dict[str, Any]:
    """``{id, kind, name, status, ns}`` plus the marker flags (``untrusted``, ``draft``, ``archived``), or
    ``dangling: true`` for an id with no record."""
    n = onto.node(node_id)
    item: Dict[str, Any] = {"id": node_id, "kind": onto.kind_of(node_id), "name": str((n or {}).get("name") or ""),
                            "status": str((n or {}).get("status") or ""), "ns": onto.ns_of(node_id)}
    if n is None:
        item["dangling"] = True
    item.update(render.flags(n))
    return item


def link_flags(edge: Optional[Dict[str, Any]]) -> Dict[str, bool]:
    """The marker flags of the link itself, kept apart from the node's: ``link_untrusted``, ``link_draft`` and
    ``link_archived`` (only the true ones)."""
    return {"link_" + k: v for k, v in render.flags(edge).items()}


def absent_mark(item: Dict[str, Any]) -> str:
    """`` (absent upstream)`` for a linked id with no record in an import at its pinned release (`` (absent)`` for a
    local one), else ``""``: the link dangles, so the id is named but never walked through."""
    if not item.get("dangling"):
        return ""
    return " (absent)" if item.get("ns") in (None, "self") else " (absent upstream)"


def rel_mark(item: Dict[str, Any], text: Optional[str] = None) -> str:
    """A related node as text: its own markers (``render.mark``), ``absent_mark`` when it has no record, then the
    markers of the link that reaches it: ``[untrusted]`` before when the link is untrusted, `` (draft link)`` or
    `` (archived link)`` after."""
    out = render.mark(item, text) + absent_mark(item)
    if item.get("link_untrusted") and not item.get("untrusted"):
        out = "[untrusted] " + out
    if item.get("link_archived") and not item.get("archived"):
        out += " (archived link)"
    elif item.get("link_draft") and not item.get("draft"):
        out += " (draft link)"
    return out


def source_entry(onto: Any, src: str, ns: str = "self") -> Optional[Dict[str, Any]]:
    """The index entry of a source: local, or from the export of the import ``ns``."""
    if src in onto.sources:
        return onto.sources[src]
    if ns and ns != "self":
        for row in (onto.exports.get(ns) or {}).get("sources") or []:
            if isinstance(row, dict) and row.get("id") == src:
                return row
    return None


def quote_untrusted(onto: Any, src: str, ns: str = "self") -> bool:
    """True unless the source is an interview (the user's own words) or an import pseudo-source."""
    if idmod.IMPSRC_RE.match(str(src or "")):
        return False
    entry = source_entry(onto, src, ns)
    return not (entry and entry.get("kind") == "interview")


def prov_items(onto: Any, rec: Dict[str, Any], ns: str = "self") -> List[Dict[str, Any]]:
    """A record's provenance as ``{src, loc, quote, by, via, untrusted}`` (``untrusted``: the quote comes from a
    source other than an interview)."""
    out = []
    for p in as_list(rec.get("prov")):
        if not isinstance(p, dict):
            continue
        item = {"src": p.get("src"), "loc": p.get("loc"), "quote": p.get("quote"), "by": p.get("by"),
                "via": p.get("via")}
        item["untrusted"] = quote_untrusted(onto, str(p.get("src") or ""), ns)
        out.append(item)
    return out


def decision_scope(onto: Any, scope_ids: Sequence[str]) -> List[str]:
    """``scope_ids`` plus the ``<ns>/<id>`` form of each local node id (briefs print local ids that way in a
    composed topic, so a decision may be recorded with either form), in order, without repeats."""
    own = onto.ns if onto.ns and onto.ns not in _import_namespaces(onto) else None
    out: List[str] = []
    seen: Set[str] = set()
    for i in scope_ids:
        forms = [i]
        if own and i in onto.nodes and i not in onto.virtual and onto.ns_of(i) == "self":
            forms.append("%s/%s" % (own, i))
        for f in forms:
            if f not in seen:
                seen.add(f)
                out.append(f)
    return out


_norm_scope = ledger.norm_scope  # a scope as ``ledger.scope_overlaps`` compares it


def _scope_prefixes(norm: str) -> Set[str]:
    """``norm`` and each prefix of it that ends with a separator or right before one: the scopes that
    ``ledger.scope_overlaps`` finds on ``norm``'s side of a prefix match."""
    out = {norm}
    for i, ch in enumerate(norm):
        if ch in ledger.SCOPE_SEPARATORS:
            if i:
                out.add(norm[:i])
            out.add(norm[: i + 1])
    return out


class ScopeIndex(object):
    """A list of scopes indexed once, so ``overlaps(other)`` answers as ``ledger.scope_overlaps(other, scopes)``
    does in time linear in ``other``: a context checks every decision against every id it lists."""

    def __init__(self, scopes: Sequence[str]) -> None:
        self.exact = {n for n in (_norm_scope(s) for s in scopes) if n}
        self.prefixes: Set[str] = set()
        for n in self.exact:
            self.prefixes.update(_scope_prefixes(n))

    def overlaps(self, scopes: Sequence[str]) -> bool:
        for s in scopes:
            n = _norm_scope(s)
            if n and (n in self.prefixes or any(p in self.exact for p in _scope_prefixes(n))):
                return True
        return False


DECISION_RATIONALE = 200  # characters of a decision's rationale kept in a get, brief or context result


def _option_label(d: Dict[str, Any]) -> Optional[str]:
    """The label of the option a decision chose (``chosen_label`` when the item carries it), or None."""
    if d.get("chosen_label"):
        return str(d["chosen_label"])
    for o in as_list(d.get("options")):
        if isinstance(o, dict) and o.get("id") == d.get("chosen") and o.get("label"):
            return str(o["label"])
    return None


def _decision_item(d: Dict[str, Any]) -> Dict[str, Any]:
    """A decision as ``get``, ``brief`` and ``context`` return it: the chosen option's id and label (so a JSON reader
    never maps the id back), the choice in the user's words when given, and the rationale cut at
    ``DECISION_RATIONALE`` characters."""
    return {"id": d.get("id"), "question": d.get("question"), "chosen": d.get("chosen"),
            "chosen_label": _option_label(d), "chosen_text": d.get("chosen_text"),
            "rationale": render.trunc(d.get("rationale"), DECISION_RATIONALE) or None, "status": d.get("status"),
            "at": d.get("at"), "scope": list(d.get("scope") or [])}


def active_decisions(onto: Any) -> List[Dict[str, Any]]:
    """Every active decision, newest first, in the shape ``decisions_for`` returns (none without a repo, or when
    the ledger cannot be read)."""
    if onto.repo is None:
        return []
    try:
        return [_decision_item(d) for d in ledger.read_decisions(onto.repo, scope=None, active=True)]
    except (OntoError, OSError, ValueError):
        return []


def decisions_for(onto: Any, scope_ids: Sequence[str], unscoped: bool = False) -> List[Dict[str, Any]]:
    """Active decisions whose scope overlaps ``scope_ids`` (read with ``decision_scope``, so ``g2t/goal:x`` and
    ``goal:x`` match alike, and matched as ``ledger.scope_overlaps`` does), newest first. An empty list reads none.
    ``unscoped`` adds the decisions recorded with no scope, which hold for the whole topic."""
    if onto.repo is None or not (scope_ids or unscoped):
        return []
    index = ScopeIndex(decision_scope(onto, scope_ids))
    return [d for d in active_decisions(onto) if (unscoped and not d["scope"]) or index.overlaps(d["scope"])]


def decision_choice(d: Dict[str, Any]) -> str:
    """What a decision chose, in words: ``chosen_text``, else the chosen option's label, else its id (as ``onto
    decisions`` prints it). Takes a ledger record or a ``decisions_for`` item."""
    return str(d.get("chosen_text") or _option_label(d) or d.get("chosen") or "")


def _is_proposed(rec: Optional[Dict[str, Any]]) -> bool:
    return bool(rec) and rec.get("status") == "proposed"


def class_map(onto: Any, drafts: bool = True) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    """``(same_as, classes)`` as the graph holds them, or, without ``drafts``, rebuilt over the ``same_as`` links
    that are neither drafts nor touch a draft node (cached per graph), so a class never rests on a proposed link."""
    if drafts:
        return onto.same_as, onto.classes
    cached = onto._cache.get("queries.classes.confirmed")
    if cached is not None:
        return cached
    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    for eid in sorted(onto.edges):
        edge = onto.edges[eid]
        if edge.get("rel") != "same_as" or not onto.active(eid) or _is_proposed(edge):
            continue
        a, b = edge.get("src"), edge.get("dst")
        if not (isinstance(a, str) and isinstance(b, str) and onto.node(a) is not None and onto.node(b) is not None):
            continue
        if not (onto.active(a) and onto.active(b)) or _is_proposed(onto.node(a)) or _is_proposed(onto.node(b)):
            continue
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        ra, rb = find(a), find(b)
        if ra != rb:
            lo, hi = sorted((ra, rb))
            parent[hi] = lo
    groups: Dict[str, List[str]] = {}
    for member in parent:
        groups.setdefault(find(member), []).append(member)
    same: Dict[str, str] = {}
    classes: Dict[str, List[str]] = {}
    for members in groups.values():
        ordered = sorted(members)
        classes[ordered[0]] = ordered
        for m in ordered:
            same[m] = ordered[0]
    cached = (same, classes)
    onto._cache["queries.classes.confirmed"] = cached
    return cached


def class_members(onto: Any, node_id: str, drafts: bool = True) -> List[str]:
    """The ``same_as`` class of ``node_id`` (itself alone when it has none); without ``drafts``, only over links
    that are not drafts (``class_map``)."""
    same, classes = class_map(onto, drafts)
    cid = same.get(node_id)
    return list(classes[cid]) if cid else [node_id]


def members_by_ns(onto: Any, node_id: str, drafts: bool = True) -> "OrderedDict[str, List[str]]":
    """The ``same_as`` class of a node grouped by namespace (``self`` first), or empty when it has none (without
    ``drafts``, a class of links that are not drafts)."""
    members = class_members(onto, node_id, drafts)
    if len(members) < 2:
        return OrderedDict()
    groups: "OrderedDict[str, List[str]]" = OrderedDict()
    for m in sorted(members, key=lambda i: (onto.ns_of(i) != "self", onto.ns_of(i), i)):
        groups.setdefault(onto.ns_of(m), []).append(m)
    return groups


def _kind_filter(onto: Any, kinds: Any) -> Optional[Set[str]]:
    if not kinds:
        return None
    names = kinds if isinstance(kinds, (list, tuple)) else str(kinds).split(",")
    out: Set[str] = set()
    bad = []
    for raw in names:
        name = str(raw).strip()
        if not name:
            continue
        if name.lower() in ("source", "sources"):
            out.add("source")
            continue
        key = onto.registry.kind_key(name) or onto.registry.plural_alias(name)
        if key is None:
            bad.append(name)
        else:
            out.add(key)
    if bad:
        raise UsageError("unknown kind %s; known kinds: %s, source" % (
            ", ".join(repr(b) for b in bad), ", ".join(onto.registry.kinds())))
    return out or None


def _ns_filter(onto: Any, ns: Optional[str]) -> Optional[str]:
    if not ns:
        return None
    if ns in ("self", onto.ns):
        return "self"
    if ns in _import_namespaces(onto):
        return ns
    raise UsageError("unknown namespace %r; known: %s" % (ns, ", ".join(["self"] + sorted(_import_namespaces(onto)))))


def _rel_filter(rels: Any) -> Optional[List[str]]:
    if not rels:
        return None
    names = rels if isinstance(rels, (list, tuple)) else str(rels).split(",")
    return [str(r).strip() for r in names if str(r).strip()] or None


def _rel_wanted(onto: Any, edge: Dict[str, Any], label: str, wanted: List[str]) -> bool:
    """True when ``wanted`` names the edge's relation, the label it reads with, or ``<ns>/<rel>`` for the import
    namespace whose declaration governs it (``--rels tea/yields``)."""
    rel = str(edge.get("rel") or "")
    if rel in wanted or label in wanted:
        return True
    ns = onto.rel_ns(edge)
    return bool(ns) and ("%s/%s" % (ns, rel) in wanted or "%s/%s" % (ns, label) in wanted)


# search ----------------------------------------------------------------------------------------------------------
# Suffixes stripped by ``stem``, tried in this order (at most two passes), each only when its number of letters
# stay: -ion needs 5 and -ation 4, so "station" and "nation" keep their own stem instead of "stat" (state) and "nat".
_STEM_SUFFIXES = (("ation", 4), ("ing", 3), ("ion", 5), ("ment", 3), ("ed", 3))
_WORD_RE = re.compile(r"[a-z0-9]+")
OR_RE = re.compile(r"\s+OR\s+")
STEM_PENALTY = 10  # a match found only through stems ranks just below a literal match of the same tier


@lru_cache(maxsize=None)
def stem(word: str) -> str:
    """A light English stem, the same for a word's common forms: plural, -ing, -ed, -ion, -ation and -ment are
    cut (while enough letters stay, ``_STEM_SUFFIXES``), a doubled last consonant is undone, a final y becomes i and
    a final e is dropped. "watering", "watered" and "waters" all give "water"; "policies" and "policy" give
    "polici". Words of 3 letters or fewer, and words with digits or punctuation, are returned lowercased and
    unchanged."""
    w = (word or "").lower()
    if len(w) <= 3 or not w.isalpha():
        return w
    if w.endswith("ies") and len(w) > 4:
        w = w[:-3] + "y"
    elif w.endswith("sses"):
        w = w[:-2]
    elif w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    for _pass in range(2):
        for suffix, keep in _STEM_SUFFIXES:
            if w.endswith(suffix) and len(w) - len(suffix) >= keep:
                w = w[: -len(suffix)]
                if len(w) > 3 and w[-1] == w[-2] and w[-1] not in "lsz":
                    w = w[:-1]
                break
        else:
            break
    if len(w) > 3 and w.endswith("y"):
        w = w[:-1] + "i"
    if len(w) > 3 and w.endswith("e"):
        w = w[:-1]
    return w


def _field_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(_field_text(v) for v in value)
    return render.fmt(value)


def _visible(text: str) -> str:
    """``text`` lowercased, its invisible characters (``util.invisible``: a soft hyphen or a zero-width space copied
    from a web page) dropped, so "basil" finds a stored "Ba\u00adsil"."""
    if any(util.invisible(ch) for ch in text):
        text = "".join(ch for ch in text if not util.invisible(ch))
    return text.lower()


def _search_text(onto: Any, node_id: str) -> str:
    """The lowercased id, name and the text fields the kind declares (cached per graph)."""
    cache = onto._cache.setdefault("queries.text", {})
    hit = cache.get(node_id)
    if hit is None:
        n = onto.node(node_id) or {}
        parts = [node_id, str(n.get("name") or "")]
        for field in onto.registry.text_fields(onto.kind_of(node_id)):
            if field == "name":
                continue
            if field == "summary":
                parts.append(str(n.get("summary") or ""))
            elif field == "aliases":
                parts.extend(str(a) for a in as_list(n.get("aliases")))
            elif field.startswith("attrs."):
                parts.append(_field_text(as_dict(n.get("attrs")).get(field[6:])))
        hit = _visible(" ".join(p for p in parts if p))
        cache[node_id] = hit
    return hit


def _stem_words(onto: Any, node_id: str, key: str, raw: str) -> frozenset:
    """The stems of the words of 4 letters or more in ``raw`` (a node's id, name or text), cached per graph. A word
    of 3 letters or fewer matches only literally, so "noted" (stem "not") finds "note" and "notes", never "not"."""
    cache = onto._cache.setdefault("queries.stems", {})
    hit = cache.get((node_id, key))
    if hit is None:
        hit = frozenset(stem(w) for w in _WORD_RE.findall((raw or "").lower()) if len(w) > 3)
        cache[(node_id, key)] = hit
    return hit


def _has(words: frozenset, raw: str, token: str, stemmed: str) -> bool:
    """A token matches literally (a substring, as in the literal pass) or its stem equals the stem of a whole word:
    never a stem inside another word ("rat" of "rating" is not a match for "generated")."""
    return token in raw or stemmed in words


def _score(onto: Any, node_id: str, q: str, tokens: List[str], stems: Optional[List[str]]) -> int:
    """The tier score of one node for one query (0 = no match). With ``stems`` (the tokens' stems), each word of the
    word tiers matches literally or as a whole stemmed word of the id, name or text (``_has``)."""
    n = onto.node(node_id) or {}
    nid = node_id.lower()
    name = _visible(str(n.get("name") or ""))
    if stems is None:
        if nid == q:
            return 1000
        if nid.endswith(":" + q) or nid.endswith("." + q) or nid.endswith("/" + q):
            return 600
        if q and q in nid:
            return 400
        if tokens and all(t in nid for t in tokens):
            return 300
        if q and q in name:
            return 250
        if tokens and all(t in name or t in nid for t in tokens):
            return 150
        hay = _search_text(onto, node_id)
        if tokens and all(t in hay for t in tokens):
            return 20 + min(30, sum(hay.count(t) for t in tokens))
        return 0
    if stems == tokens:
        return 0  # no word has another form: the literal pass already said everything
    pairs = list(zip(tokens, stems))
    sid, sname = _stem_words(onto, node_id, "id", nid), _stem_words(onto, node_id, "name", name)
    if pairs and all(_has(sid, nid, t, s) for t, s in pairs):
        return 300 - STEM_PENALTY
    if pairs and all(_has(sname, name, t, s) or _has(sid, nid, t, s) for t, s in pairs):
        return 150 - STEM_PENALTY
    raw = _search_text(onto, node_id)
    hay = _stem_words(onto, node_id, "text", raw)
    if pairs and all(_has(hay, raw, t, s) for t, s in pairs):
        return 20 + min(30, sum(max(1, raw.count(t)) for t in tokens)) - STEM_PENALTY
    return 0


def search_alternatives(text: str) -> List[str]:
    """The alternatives of a query: ``a OR b`` (the word OR in capitals, between spaces) matches either."""
    return [a.strip() for a in OR_RE.split((text or "").strip()) if a.strip()]


def _parsed(onto: Any, text: str) -> List[Tuple[str, List[str], List[str]]]:
    """``[(query, tokens, stems)]``, one per alternative of ``text``, lowercased as ``search`` scores them."""
    out = []
    for alt in search_alternatives(text) or [""]:
        q = _visible(local_text(onto, alt))  # g2t/goal:x reads as goal:x inside g2t
        tokens = [t for t in q.replace(",", " ").split() if t]
        out.append((q, tokens, [stem(t) for t in tokens]))
    return out


NAME_SCORE = 150 - STEM_PENALTY  # the lowest score of an id or name tier; the text tiers score 50 or less


def _name_score(onto: Any, node_id: str, parsed: List[Tuple[str, List[str], List[str]]]) -> int:
    """The best id or name tier score of a node over the parsed alternatives (``NAME_SCORE`` for every word in one
    of its aliases), or 0: a match only in other text, such as a summary, does not name the node."""
    best = 0
    aliases: Optional[List[str]] = None
    for q, tokens, stems in parsed:
        if not tokens:
            continue
        score = _score(onto, node_id, q, tokens, None)
        if score < NAME_SCORE:
            score = max(score, _score(onto, node_id, q, tokens, stems))
        if score >= NAME_SCORE:
            best = max(best, score)
            continue
        if aliases is None:
            aliases = [_visible(str(a)) for a in as_list((onto.node(node_id) or {}).get("aliases"))]
        for alias in aliases:
            words = frozenset(stem(w) for w in _WORD_RE.findall(alias) if len(w) > 3)
            if all(_has(words, alias, t, s) for t, s in zip(tokens, stems)):
                best = max(best, NAME_SCORE)
    return best


def names(onto: Any, node_id: str, text: str) -> bool:
    """True when ``text`` (or one of its ``OR`` alternatives) names the node: its id or name, matched as the id and
    name tiers of ``search`` match them (whole-word stems included), or every word of it in one alias."""
    return bool(_name_score(onto, node_id, _parsed(onto, text)))


def archived_named(onto: Any, text: str) -> List[str]:
    """The archived nodes ``text`` names (``names``), best match first. ``search`` leaves archived nodes out (and
    counts them); a brief or context names these, so a retired subject reads as archived, with the decision that
    retired it, never as "not in the ontology"."""
    parsed = _parsed(onto, text)
    found: List[Tuple[int, int, str]] = []
    for node_id, n in onto.nodes.items():
        if n.get("status") != "archived" or node_id in onto.virtual:
            continue
        score = _name_score(onto, node_id, parsed)
        if score:
            found.append((-score, len(node_id), node_id))
    return [node_id for _score_, _len, node_id in sorted(found)]


def search(onto: Any, text: str, kinds: Any = None, ns: Optional[str] = None, status: str = "any",
           include_archived: bool = False, suggest: bool = True) -> Dict[str, Any]:
    """Rank nodes for ``text`` (see the module docstring). ``kinds`` keeps those kinds (names, plurals or
    ``ns/kind``), ``ns`` one namespace (``self`` for local), ``status`` ``any``, ``confirmed`` or ``drafts``.

    Returns ``{query, total, results: [node_brief + score], by_kind, did_you_mean, archived_hidden}``, every match
    ranked (``dispatch`` pages ``results``)."""
    if status not in ("any", "confirmed", "drafts"):
        raise UsageError("status must be any, confirmed or drafts, got %r" % (status,))
    alternatives = search_alternatives(text) or [""]
    parsed = _parsed(onto, text)
    lowered = {q for q, _t, _s in parsed}
    kind_keys = _kind_filter(onto, kinds)
    only_ns = _ns_filter(onto, ns)
    results: List[Tuple[int, bool, str]] = []
    hidden = 0
    for node_id in onto.nodes:
        if kind_keys is not None and onto.kind_of(node_id) not in kind_keys:
            continue
        if only_ns is not None and onto.ns_of(node_id) != only_ns:
            continue
        n = onto.nodes[node_id]
        state = n.get("status")
        archived = state == "archived"
        if not archived and status == "confirmed" and state != "confirmed":
            continue
        if not archived and status == "drafts" and state != "proposed":
            continue
        exact = node_id.lower() in lowered
        # Each alternative scores its better match, literal or stemmed, and the node keeps its best alternative: a
        # weak literal text match in one alternative must not hide a stronger stemmed id or name match in another.
        score = 0
        for q, tokens, stems in parsed:
            alt_score = _score(onto, node_id, q, tokens, None)
            if alt_score < 300 - STEM_PENALTY:
                alt_score = max(alt_score, _score(onto, node_id, q, tokens, stems))
            score = max(score, alt_score)
        if not score:
            continue
        if archived and not include_archived and not exact:
            hidden += 1
            continue
        score += int(round(BOOST_POINTS * onto.registry.boost(onto.kind_of(node_id))))
        results.append((score, archived, node_id))
    # At the same score an active node ranks above an archived one.
    results.sort(key=lambda r: (-r[0], r[1], len(r[2]), r[2]))
    items = []
    for score, _archived, node_id in results:
        item = node_brief(onto, node_id)
        item["score"] = score
        items.append(item)
    out: Dict[str, Any] = {
        "query": text, "kinds": sorted(kind_keys) if kind_keys else [], "ns": ns, "status": status,
        "total": len(results), "results": items,
        "by_kind": dict(sorted(Counter(onto.kind_of(r[2]) for r in results).items())),
        "archived_hidden": hidden, "did_you_mean": None,
    }
    if len(alternatives) > 1:
        out["alternatives"] = alternatives
    records = _record_matches(onto, lowered, status)
    if records:
        out["records"] = records
    if suggest and not results and not hidden and not records:
        out["did_you_mean"] = did_you_mean(onto, text, kinds, ns, status=status,
                                           include_archived=include_archived) or None
    return out


def _record_matches(onto: Any, lowered: Set[str], status: str = "any") -> List[Dict[str, Any]]:
    """Records other than nodes whose id is exactly one of the searched texts: an edge (``status`` applied as for
    nodes), or a source that lives in an import (``import_source``). Search ranks nodes; these are named apart so a
    search for such an id never reads "not in the ontology"."""
    out: List[Dict[str, Any]] = []
    for text in sorted(lowered):
        upstream = import_source(onto, text)
        if upstream is not None:
            ns, entry = upstream
            item: Dict[str, Any] = {"id": entry["id"], "kind": "source", "ns": ns,
                                    "note": import_source_text(onto, upstream)}
            if entry.get("kind") != "import" and quote_untrusted(onto, entry["id"], ns):
                item["untrusted"] = True
            out.append(item)
            continue
        edge = onto.edges.get(text) if idmod.EDGE_RE.match(text) else None
        if edge is None or onto.node(text) is not None:
            continue
        state = edge.get("status")
        if state != "archived" and ((status == "confirmed" and state != "confirmed")
                                    or (status == "drafts" and state != "proposed")):
            continue
        item = {"id": text, "kind": "edge", "src": edge.get("src"), "rel": edge.get("rel"), "dst": edge.get("dst"),
                "status": state, "bridge": onto.is_bridge(edge)}
        item.update(render.flags(edge))
        out.append(item)
    return out


# did you mean ----------------------------------------------------------------------------------------------------
def _vocabulary(onto: Any) -> Dict[str, int]:
    """Every word (3+ characters) of the node ids and names, with how many nodes use it. Cached per graph."""
    cached = onto._cache.get("queries.vocab")
    if cached is not None:
        return cached
    counts: Counter = Counter()
    for node_id, n in onto.nodes.items():
        words = set(_WORD_RE.findall(node_id.lower())) | set(_WORD_RE.findall(str(n.get("name") or "").lower()))
        counts.update(w for w in words if len(w) >= 3 and not w.isdigit())
    vocab = dict(counts)
    onto._cache["queries.vocab"] = vocab
    return vocab


edit_distance = util.edit_distance  # Damerau distance with a cap (one implementation for the whole kit)


def _closest_word(vocab: Dict[str, int], word: str) -> Optional[str]:
    """The vocabulary word nearest ``word`` (at most 1 edit for words up to 5 letters, else 2), or None. Ties go to
    the word closest in length, then the one more nodes use, then alphabetical order."""
    cap = 1 if len(word) <= 5 else 2
    best: Optional[Tuple[int, int, int, str]] = None
    for cand, count in vocab.items():
        if abs(len(cand) - len(word)) > cap or cand == word:
            continue
        d = edit_distance(word, cand, cap)
        key = (d, abs(len(cand) - len(word)), -count, cand)
        if d <= cap and (best is None or key < best):
            best = key
    return best[3] if best else None


def correct_text(onto: Any, text: str) -> Optional[str]:
    """``text`` with each word (3+ letters) that no id or name uses replaced by the nearest one that is used, or
    None when nothing changes. "tomatoe" becomes "tomato", "crop:tomatoe" becomes "crop:tomato"."""
    vocab = _vocabulary(onto)
    changed = [False]

    def fix(match: "re.Match[str]") -> str:
        word = match.group(0)
        low = word.lower()
        if len(low) < 3 or low.isdigit() or low in vocab or low == "or":
            return word
        near = _closest_word(vocab, low)
        if not near:
            return word
        changed[0] = True
        return near

    fixed = re.sub(r"[A-Za-z0-9]+", fix, text or "")
    return fixed if changed[0] else None


def did_you_mean(onto: Any, text: str, kinds: Any = None, ns: Optional[str] = None,
                 limit: int = DID_YOU_MEAN_IDS, status: str = "any", include_archived: bool = False) -> Dict[str, Any]:
    """For a search that found nothing: a corrected query (nearest words of ids and names by edit distance) and the
    first ids it finds under the same filters (``kinds``, ``ns``, ``status``, ``include_archived``), or {} when no
    correction finds anything."""
    fixed = correct_text(onto, text)
    if not fixed:
        return {}
    res = search(onto, fixed, kinds, ns, status=status, include_archived=include_archived, suggest=False)
    if not res["total"]:
        return {}
    return {"query": fixed, "ids": [r["id"] for r in res["results"][:limit]], "total": res["total"]}


# get -------------------------------------------------------------------------------------------------------------
def relations(onto: Any, node_id: str, include_archived: bool = False) -> Dict[str, Any]:
    """``{relations: {label: [items]}, relation_totals: {label: n}}``: every link of a node, one row per (label,
    other id); background links form their own ``<label> (background)`` groups after the others. Each item is the
    other node's brief plus ``edge``, ``conf``, ``bridge`` and ``background``; its flags mark a draft, untrusted or
    archived link or node."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for e in onto.edges_of(node_id, include_archived=include_archived):
        edge = e["edge"]
        item = node_brief(onto, e["other"])
        item.update(link_flags(edge))
        item.update({"edge": edge.get("id"), "conf": edge.get("conf"), "bridge": onto.is_bridge(edge),
                     "background": bool(edge.get("background"))})
        if edge.get("note"):
            item["note"] = edge.get("note")
        label = e["label"] + (" (background)" if edge.get("background") else "")
        groups.setdefault(label, []).append(item)
    ordered: Dict[str, List[Dict[str, Any]]] = OrderedDict()
    for label in sorted(groups, key=lambda lab: (lab.endswith(" (background)"), lab)):
        items = groups[label]
        items.sort(key=lambda i: (-(i.get("conf") if isinstance(i.get("conf"), (int, float)) else 0),
                                  str(i["kind"]), str(i["id"])))
        ordered[label] = items
    collapsed, totals = render.collapse_relations(ordered)
    return {"relations": collapsed, "relation_totals": totals}


def page_relations(res: Dict[str, Any], limit: int, offset: int = 0) -> Dict[str, Any]:
    """A ``get`` result with each relation group cut to ``limit`` rows from ``offset`` (0 = no cap)."""
    out = dict(res)
    out["relations"] = OrderedDict(
        (label, render.page(items, limit, offset)[0]) for label, items in (res.get("relations") or {}).items())
    return out


MALFORMED = "the record is malformed, so its needs are unknown (onto validate names the problem)"


def safe_needs(onto: Any, node_id: str) -> Optional[Dict[str, Any]]:
    """``needs.needs`` of one node, or None when a malformed record (an ``attrs`` that is not an object, say)
    stops it: the read goes on and ``validate`` reports the record."""
    try:
        return needs_mod.needs(onto, node_id)
    except (AttributeError, TypeError, ValueError, KeyError):
        return None


def _needs_of(onto: Any, node_id: str) -> Optional[Dict[str, Any]]:
    if onto.node(node_id) is None or not onto.active(node_id) or node_id in onto.virtual:
        return None
    return safe_needs(onto, node_id)


def record_view(onto: Any, record_id: str, rec: Dict[str, Any]) -> Dict[str, Any]:
    """A stored record as ``get`` returns it: every stored field (an imported record with its qualified id), plus
    ``ns`` and the ``untrusted`` and ``draft`` flags. An archived record's ``archived`` block is its archived flag."""
    out: Dict[str, Any] = OrderedDict((k, rec[k]) for k in sorted(rec))
    out["id"] = record_id
    out["ns"] = onto.ns_of(record_id)
    for flag, value in render.flags(rec).items():
        if flag != "archived":
            out[flag] = value
    return out


def _get_node(onto: Any, node_id: str, include_archived: bool) -> Dict[str, Any]:
    n = onto.node(node_id) or {}
    ns = onto.ns_of(node_id)
    node = record_view(onto, node_id, n)
    facts: Dict[str, Any] = OrderedDict()
    facts["summary"] = n.get("summary") or ""
    for key, value in sorted(as_dict(n.get("attrs")).items()):
        facts[key] = value
    out: Dict[str, Any] = OrderedDict([("id", node_id), ("kind", onto.kind_of(node_id)), ("node", node),
                                       ("facts", facts), ("prov", prov_items(onto, n, ns))])
    if as_list(n.get("gaps")):
        out["gaps"] = [dict(g) for g in as_list(n.get("gaps")) if isinstance(g, dict)]
    if as_dict(n.get("archived")):
        out["archived"] = dict(n["archived"])
    members = members_by_ns(onto, node_id)
    if members:
        out["members"] = members
    out.update(relations(onto, node_id, include_archived))
    out["needs"] = _needs_of(onto, node_id)
    if out["needs"] is None and onto.active(node_id) and node_id not in onto.virtual:
        out["needs_note"] = MALFORMED
    out["decisions"] = decisions_for(onto, onto.members(node_id))
    return out


def _get_edge(onto: Any, edge_id: str) -> Dict[str, Any]:
    edge = onto.edges[edge_id]
    ns = onto.edge_origin.get(edge_id, ("self", ""))[0]
    item = record_view(onto, edge_id, edge)
    item["ns"] = ns
    item["inverse"] = onto.inverse_of(edge_id)
    item["bridge"] = onto.is_bridge(edge)
    ends = OrderedDict([("src", node_brief(onto, str(edge.get("src")))),
                        ("dst", node_brief(onto, str(edge.get("dst"))))])
    out: Dict[str, Any] = OrderedDict([("id", edge_id), ("kind", "edge"), ("edge", item), ("ends", ends),
                                       ("facts", OrderedDict([("note", edge.get("note") or "")])),
                                       ("prov", prov_items(onto, edge, ns))])
    if as_dict(edge.get("archived")):
        out["archived"] = dict(edge["archived"])
    out["relations"] = OrderedDict()
    out["relation_totals"] = {}
    out["needs"] = None
    out["decisions"] = decisions_for(onto, [edge_id, str(edge.get("src")), str(edge.get("dst"))])
    return out


def parse_lines(value: Any) -> Tuple[int, int]:
    """``"a-b"`` (or ``"La-Lb"``, or one number) as a 1-based inclusive span; ``UsageError`` otherwise."""
    m = _LINES_ARG_RE.match(str(value or ""))
    if not m:
        raise UsageError("lines must look like a-b (for example 3-9), got %r" % (value,))
    a = int(m.group(1))
    b = int(m.group(2) or m.group(1))
    if b < a:
        raise UsageError("lines %s: the end comes before the start" % value)
    return a, b


def _skeleton(line: str) -> Tuple[str, List[int]]:
    """``line`` as it reads, to spot fence-like text: compatibility forms folded (NFKD: fullwidth and styled
    letters), characters that print nothing dropped (Unicode Cc, Cf, Mn and Me, tab kept), blank-printing ones read
    as a space, case folded, and the lookalikes of ``_LOOKALIKE`` read as the letter or ``[`` they pass for. Returns
    it with, for each of its characters, the index in ``line`` it came from."""
    if line.isascii():  # the usual line: nothing to fold but the case
        return line.lower(), list(range(len(line)))
    out: List[str] = []
    where: List[int] = []
    for i, ch in enumerate(line):
        for c in unicodedata.normalize("NFKD", ch):
            if c in _BLANKS:
                c = " "
            elif c != "\t" and unicodedata.category(c) in _HIDDEN:
                continue
            for f in c.casefold():
                out.append(_LOOKALIKE.get(f, f))
                where.append(i)
    return "".join(out), where


def _shown_chars(text: str) -> str:
    """``text`` without its Unicode Cc and Cf characters (tab and newline kept): what a fence-like line keeps."""
    return "".join(c for c in text if c in "\t\n" or unicodedata.category(c) not in ("Cc", "Cf"))


def _defuse_line(line: str) -> str:
    skel, where = _skeleton(line)
    hits = list(_SKELETON_FENCE_RE.finditer(skel))
    if not hits:
        return line
    parts: List[str] = []
    last = 0
    skel_end = -1
    for m in hits:
        if not m.group(0) and m.start() == skel_end:  # the "untrusted" a bracket just before it already defused
            continue
        skel_end = m.end()
        start = where[m.start()] if m.start() < len(where) else len(line)
        end = where[m.end()] if m.end() < len(where) else len(line)  # the "u" of "untrusted"
        if start < last:
            continue
        if m.group(0):  # a bracket (or its lookalike) and what sits before "untrusted": one safe bracket
            parts += [line[last:start], _FENCE_SAFE]
        else:  # "untrusted src" with no bracket before it
            parts += [line[last:start], "(quoted) "]
        last = end
    parts.append(line[last:])
    return _shown_chars("".join(parts))  # nothing invisible is left to hide a marker again


def defuse(body: str) -> str:
    """``body`` with the control and bidi characters of ``render.CONTROL_RE`` removed first (``plain_block``, as the
    text renderer prints it), then every fence-like marker defused, line by line. A line is checked as it reads
    (``_skeleton``), so invisible characters (bidi, zero-width, controls), fullwidth or styled forms, case and
    lookalike letters cannot hide a marker: a ``[`` followed by ``untrusted``, with any spaces between, becomes
    ``[(quoted) untrusted``, and ``untrusted src`` after any other character becomes ``(quoted) untrusted src``. A
    defused line also loses its Unicode Cc and Cf characters."""
    return "\n".join(_defuse_line(line) for line in plain_block(body or "").split("\n"))


def fence(src_id: str, loc: str, body: str) -> str:
    """Source text between untrusted fences, defused (``defuse``) so the text cannot close its own fence or open a
    new one. The JSON form carries the same text the text renderer prints."""
    return "\n".join([FENCE_OPEN % (src_id, loc), defuse(body), FENCE_CLOSE % src_id])


def _source_text(onto: Any, src_id: str, full: bool, chunk: Optional[int], lines: Any,
                 body: bool = True, text_chars: int = 0, mcp: bool = False) -> Dict[str, Any]:
    """The part of a source's text a read returns, fenced. Without ``body`` (a read that pages the links), the part
    is named but not sent: ``skipped: true`` with ``read``, the arguments that return it. With ``text_chars`` (an
    MCP read, ``SOURCE_TEXT_CHARS``), a part longer than that stops at the last whole line that fits (at least one
    line): ``loc`` and the fences give the lines actually sent, and ``cut`` gives the range asked for, the next
    range (``next``) and the call that reads it (``next_call``), so no read is cut later without saying where."""
    try:
        text = sources_mod.read(onto.repo, src_id) if onto.repo is not None else None
    except NotFound as exc:
        text, missing = None, exc.message
    else:
        missing = None if text is not None else "no repo to read from"
    if text is None:
        return {"missing": True, "note": missing, "untrusted": True, "body": None}
    all_lines = text.split("\n")
    if all_lines and all_lines[-1] == "":
        all_lines.pop()
    total = len(all_lines)
    chunks = sources_mod.chunks_of(text)
    index = [{"n": c["n"], "loc": c["loc"], "chars": len(c["text"])} for c in chunks]
    n: Optional[int] = None
    read: Dict[str, Any] = {}
    if lines:
        a, b = parse_lines(lines)
        if a > max(total, 1):
            raise UsageError("%s has %d line(s); lines %s is past the end" % (src_id, total, lines))
        b = min(b, total)
        loc, part = "L%d-L%d" % (a, b), "\n".join(all_lines[a - 1: b])
        read = {"lines": "%d-%d" % (a, b)}
    elif chunk is not None:
        if not isinstance(chunk, int) or isinstance(chunk, bool) or chunk < 1 or chunk > max(len(chunks), 1):
            raise UsageError("%s has %d chunk(s); chunk %r does not exist" % (src_id, len(chunks), chunk))
        if not chunks:
            n, loc, part = 1, "L1-L1", ""
        else:
            n, loc, part = chunk, chunks[chunk - 1]["loc"], chunks[chunk - 1]["text"]
        read = {"chunk": n}
    elif full or len(chunks) <= 1:
        n = None if full and len(chunks) > 1 else 1
        loc, part = "L1-L%d" % max(total, 1), "\n".join(all_lines)
        read = {"full": True} if n is None else {}
    else:
        n, loc, part = 1, chunks[0]["loc"], chunks[0]["text"]
        read = {"chunk": 1}
    if not body:
        return {"skipped": True, "loc": loc, "chunk": n, "chunks": len(chunks), "lines": total, "untrusted": True,
                "body": None, "read": read}
    out = {"loc": loc, "chunk": n, "chunks": len(chunks), "lines": total, "index": index, "untrusted": True}
    if text_chars and len(part) > text_chars:
        a, b = parse_lines(loc)
        last, used = a - 1, 0  # the last line sent, and the characters sent so far
        while last < b and (last < a or used + len(all_lines[last]) + 1 <= text_chars):
            used += len(all_lines[last]) + 1
            last += 1
        if last < b:
            nxt = {"lines": "%d-%d" % (last + 1, b)}
            out["cut"] = OrderedDict([("asked", loc), ("chars", text_chars), ("next", nxt),
                                      ("next_call", render.call(mcp, "get", id=src_id, **nxt))])
            loc, part = "L%d-L%d" % (a, last), "\n".join(all_lines[a - 1: last])
            out["loc"], out["chunk"] = loc, None
    out["body"] = fence(src_id, loc, part)
    return out


def _get_source(onto: Any, src_id: str, full: bool, chunk: Optional[int], lines: Any,
                include_archived: bool, body: bool = True, text_chars: int = 0, mcp: bool = False) -> Dict[str, Any]:
    entry = dict(onto.sources.get(src_id) or {})
    cited = []
    seen: Set[Tuple[str, str]] = set()
    for rid, loc in sorted(onto.prov_index.get(src_id, []), key=lambda p: (p[0], p[1])):
        if (rid, loc) in seen:
            continue
        seen.add((rid, loc))
        rec = onto.record(rid)
        if rec is not None and rec.get("status") == "archived" and not include_archived:
            continue
        if rid in onto.edges:
            item = {"id": rid, "kind": "edge", "rel": rec.get("rel") if rec else None, "status": (rec or {}).get(
                "status", "")}
            item.update(render.flags(rec))
        else:
            item = node_brief(onto, rid)
        item["loc"] = loc
        cited.append(item)
    entry.update(render.flags(onto.node(src_id)))
    out: Dict[str, Any] = OrderedDict([("id", src_id), ("kind", "source"), ("source", entry),
                                       ("cited_by", cited), ("cited_total", len(cited))])
    out.update(relations(onto, src_id, include_archived))
    out["text"] = _source_text(onto, src_id, full, chunk, lines, body, text_chars, mcp)
    out["needs"] = None
    out["decisions"] = decisions_for(onto, [src_id])
    return out


def _get_import_source(onto: Any, upstream: Tuple[str, Dict[str, Any]], include_archived: bool) -> Dict[str, Any]:
    """A source of an import (``import_source``): its index entry, the records citing it (the import's own and
    local ones) and a text note, in the shape of a local source result."""
    ns, entry = upstream
    src = str(entry["id"])
    entry = dict(entry, ns=ns)
    if entry.get("kind") != "import" and quote_untrusted(onto, src, ns):
        entry["untrusted"] = True
    cited: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str]] = set()
    pairs = list(onto.prov_index.get(src, []))
    for rid in sorted(onto.nodes):
        if onto.ns_of(rid) == ns and rid not in onto.virtual:
            pairs += [(rid, str(p.get("loc") or "")) for p in as_list(onto.nodes[rid].get("prov"))
                      if isinstance(p, dict) and p.get("src") == src]
    for eid in sorted(onto.edge_origin):
        if onto.edge_origin[eid][0] == ns:
            pairs += [(eid, str(p.get("loc") or "")) for p in as_list(onto.edges[eid].get("prov"))
                      if isinstance(p, dict) and p.get("src") == src]
    for rid, loc in sorted(pairs):
        if (rid, loc) in seen:
            continue
        seen.add((rid, loc))
        rec = onto.record(rid)
        if rec is not None and rec.get("status") == "archived" and not include_archived:
            continue
        if rid in onto.edges:
            item = {"id": rid, "kind": "edge", "rel": (rec or {}).get("rel"), "status": (rec or {}).get("status", "")}
            item.update(render.flags(rec))
        else:
            item = node_brief(onto, rid)
        item["loc"] = loc
        cited.append(item)
    where = onto.import_label(ns)
    out: Dict[str, Any] = OrderedDict([
        ("id", src), ("kind", "source"), ("ns", ns), ("source", entry), ("cited_by", cited),
        ("cited_total", len(cited)), ("relations", OrderedDict()), ("relation_totals", {}),
        ("text", {"missing": True, "untrusted": True, "body": None,
                  "note": "an import's export lists its sources, not their text; the text stays in %s" % where
                  if entry.get("kind") != "import" else "the pinned export of %s" % where}),
        ("needs", None), ("decisions", decisions_for(onto, [src]))])
    if entry.get("kind") != "import":  # edges ending on it use the qualified id the graph loads it under
        qid = idmod.qualify(ns, src)
        if qid in onto.nodes:
            out.update(relations(onto, qid, include_archived))
    return out


def get(onto: Any, id: str, full: bool = False, chunk: Optional[int] = None, lines: Any = None,
        include_archived: bool = False, text: bool = True, mcp: Optional[bool] = None,
        text_chars: int = 0) -> Dict[str, Any]:
    """One node, edge or source (see the module docstring). ``chunk`` (1-based) or ``lines`` (``"a-b"``) pick the
    part of a source's text returned (default: chunk 1; ``full``: the whole text); ``text`` False names that part
    without sending it (``cmd_get`` when it pages the links); ``text_chars`` caps the characters of that part at
    a whole line (``_source_text``; ``cmd_get`` sets ``SOURCE_TEXT_CHARS`` over MCP). ``mcp`` words the follow-up
    calls of a miss and of a cut read. Relations come whole; ``cmd_get`` pages them per label."""
    note: Dict[str, Any] = {}
    upstream = import_source(onto, id)
    if upstream is not None:
        if chunk is not None or lines:
            raise UsageError("chunk and lines read local source text; %s is %s" % (
                upstream[1]["id"], import_source_text(onto, upstream)))
        out = _get_import_source(onto, upstream, include_archived)
        out["full"] = bool(full)
        return out
    rid = resolve_or_raise(onto, id, res=note, mcp=mcp)
    if (chunk is not None or lines) and rid not in onto.virtual:
        raise UsageError("chunk and lines apply to sources (src-...) only; %s is a %s" % (rid, onto.kind_of(rid)))
    if rid in onto.virtual:
        out = _get_source(onto, rid, full, chunk, lines, include_archived, text, text_chars, bool(mcp))
    elif rid in onto.edges and onto.node(rid) is None:
        out = _get_edge(onto, rid)
    else:
        out = _get_node(onto, rid, include_archived)
    out["full"] = bool(full)
    out.update(note)
    return out


# neighbors -------------------------------------------------------------------------------------------------------
def _key(onto: Any, node_id: str, drafts: bool = True) -> str:
    return class_map(onto, drafts)[0].get(node_id, node_id)


def _link_item(onto: Any, other: str, e: Dict[str, Any], member: str) -> Dict[str, Any]:
    """One link a walk reached a node by: the node's brief, then the link's label, id, the member it hangs from
    (``via``), ``bridge``, ``background`` and the link's own marker flags."""
    edge = e["edge"]
    item = node_brief(onto, other)
    item.update({"edge": e["label"], "edge_id": edge.get("id"), "via": member, "bridge": onto.is_bridge(edge),
                 "background": bool(edge.get("background"))})
    item.update(link_flags(edge))
    return item


LINK_FIELDS = ("edge", "edge_id", "via", "bridge", "background", "link_untrusted", "link_draft", "link_archived")


def _listed_item(onto: Any, entry: Dict[str, Any], ok: Callable[[str], bool], filtered: bool,
                 drafts: bool) -> Optional[Dict[str, Any]]:
    """The item a ``neighbors`` class entry lists, or None when a filter keeps none of its members. It is listed
    under the first member a link reached; with ``kinds`` or ``ns``, under the first member that passes them (a link
    that reaches such a member leads; else the member stands for its class under the first link). ``links`` holds
    every distinct link that reached the class at that depth when there is more than one (with a filter, those
    reaching a member that passes it, when any does)."""
    links = entry["links"]
    first = links[0]
    group = members_by_ns(onto, first["id"], drafts)
    if filtered:
        matching = [link for link in links if ok(link["id"])]
        if matching:
            links = matching
            item = dict(matching[0])
        else:
            members = [m for ids in group.values() for m in ids] if group else [first["id"]]
            chosen = next((m for m in members if ok(m)), None)
            if chosen is None:
                return None
            item = node_brief(onto, chosen)
            item.update((k, first[k]) for k in LINK_FIELDS if k in first)
            links = [item]
    else:
        item = dict(first)
    item["depth"] = entry["depth"]
    if group:
        item["members"] = group
    if len(links) > 1:
        item["links"] = [dict(link) for link in links]
    return item


def neighbors(onto: Any, id: str, depth: int = 1, rels: Any = None, kinds: Any = None, ns: Optional[str] = None,
              include_archived: bool = False, drafts: bool = True, mcp: Optional[bool] = None) -> Dict[str, Any]:
    """Nodes within ``depth`` hops (1 to 3) of ``id``: ``{start, depth, items: [{id, depth, edge, edge_id, via,
    kind, ns, ...}], not_expanded, hidden: {archived}, by_kind}``. A ``same_as`` class is one item, reached at its
    least depth; every distinct link (label, member reached, bridge or not) that reaches it at that depth is kept,
    and when there are several the item carries them all as ``links`` (each with the member it reaches and the
    link's label, id, ``via`` and flags), so a composed topic's bridge into a class is never lost behind another
    link. ``rels`` walks only those relations (names or inverse names); ``kinds`` and ``ns`` filter what is
    listed, not what is walked, and a class passes when any of its members does: it is listed under that member.
    ``drafts`` False walks neither a draft link nor a draft node (a node a draft reaches is still listed when a
    reviewed link reaches it), and adds ``hidden.drafts`` and ``drafts_hidden`` (the ids of the proposed records
    left out). An id with no record (the end of a bridge that is absent at the pinned import) is listed with
    ``dangling: true`` and never expanded, as ``get`` and ``path`` do not know it either."""
    note: Dict[str, Any] = {}
    start = resolve_or_raise(onto, id, res=note, mcp=mcp)
    try:
        depth = max(1, min(3, int(depth or 1)))
    except (TypeError, ValueError):
        raise UsageError("depth must be 1, 2 or 3")
    wanted = _rel_filter(rels)
    kind_keys = _kind_filter(onto, kinds)
    only_ns = _ns_filter(onto, ns)
    seen: Dict[str, int] = {_key(onto, start, drafts): 0}
    frontier = [start]
    entries: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()  # class key -> {depth, links, sigs, walked}
    not_expanded: List[str] = []
    hidden: Set[str] = set()
    hidden_drafts: Set[str] = set()
    for level in range(1, depth + 1):
        nxt: List[str] = []
        for node_id in frontier:
            if node_id != start and onto.registry.is_hub(onto.kind_of(node_id)):
                if node_id not in not_expanded:
                    not_expanded.append(node_id)
                continue
            for member in class_members(onto, node_id, drafts):
                for e in onto.edges_of(member, include_archived=True):
                    edge = e["edge"]
                    other = e["other"]
                    if edge.get("rel") == "same_as" and _key(onto, other, drafts) == _key(onto, node_id, drafts):
                        continue
                    if wanted is not None and not _rel_wanted(onto, edge, e["label"], wanted):
                        continue
                    other_rec = onto.node(other)
                    if not include_archived and (edge.get("status") == "archived" or (
                            other_rec is not None and other_rec.get("status") == "archived")):
                        if other_rec is not None and other_rec.get("status") == "archived":
                            hidden.add(other)
                        continue
                    if not drafts and (_is_proposed(edge) or _is_proposed(other_rec)):
                        hidden_drafts.update(i for i, r in ((str(edge.get("id")), edge), (other, other_rec))
                                             if _is_proposed(r))
                        continue
                    key = _key(onto, other, drafts)
                    if seen.get(key, level) != level:  # the start, or a class reached at a lesser depth
                        continue
                    entry = entries.get(key)
                    if entry is None:
                        seen[key] = level
                        entry = entries[key] = {"depth": level, "links": [], "sigs": set(), "walked": False}
                    sig = (other, e["label"], onto.is_bridge(edge), bool(edge.get("background")))
                    if sig in entry["sigs"]:
                        continue  # the same label into the same member again (another via): one row is enough
                    entry["sigs"].add(sig)
                    entry["links"].append(_link_item(onto, other, e, member))
                    # a background link is never walked through, nor an id with no record (a bridge whose end
                    # is absent at the pinned import): it is listed, marked, and the walk stops there unless
                    # another link of the same depth reaches the class
                    if not entry["walked"] and not edge.get("background") and other_rec is not None:
                        entry["walked"] = True
                        nxt.append(other)
        frontier = nxt

    def passes(node_id: str) -> bool:
        return ((kind_keys is None or onto.kind_of(node_id) in kind_keys)
                and (only_ns is None or onto.ns_of(node_id) == only_ns))

    filtered = kind_keys is not None or only_ns is not None
    listed = [item for item in (_listed_item(onto, entry, passes, filtered, drafts) for entry in entries.values())
              if item is not None]
    listed.sort(key=lambda f: (f["depth"], f["background"], f["edge"], str(f["kind"]), f["id"]))
    out: Dict[str, Any] = {"start": start, "depth": depth, "rels": wanted or [],
                           "kinds": sorted(kind_keys) if kind_keys else [], "ns": ns, "total": len(listed),
                           "items": listed, "not_expanded": not_expanded,
                           "hidden": {"archived": len(hidden)},
                           "by_kind": dict(sorted(Counter(f["kind"] for f in listed).items()))}
    if not drafts:
        out["hidden"]["drafts"] = len(hidden_drafts)
        out["drafts_hidden"] = sorted(hidden_drafts)
    members = members_by_ns(onto, start, drafts)
    if members:
        out["members"] = members
    out.update(note)
    return out


# path ------------------------------------------------------------------------------------------------------------
def _path_steps(onto: Any, node_id: str, wanted: Optional[List[str]],
                cache: Dict[str, List[Tuple[str, int, Dict[str, Any], str, str]]]) -> List[Tuple[str, int, Dict[
                    str, Any], str, str]]:
    """``[(other, cost, edge, label, direction)]`` over active non-background links, one per other node (a
    ``same_as`` link costs 0 and wins over a parallel link), sorted by the other id."""
    hit = cache.get(node_id)
    if hit is not None:
        return hit
    best: Dict[str, Tuple[int, str, str, Dict[str, Any], str, str]] = {}
    for e in onto.edges_of(node_id):
        edge = e["edge"]
        if edge.get("background"):
            continue
        rel = str(edge.get("rel") or "")
        cost = 0 if rel == "same_as" else 1
        if cost and wanted is not None and not _rel_wanted(onto, edge, e["label"], wanted):
            continue
        other = e["other"]
        if onto.node(other) is None:
            continue
        rank = (cost, e["label"], str(edge.get("id")), edge, e["label"], e["direction"])
        if other not in best or rank[:3] < best[other][:3]:
            best[other] = rank
    out = [(other, r[0], r[3], r[4], r[5]) for other, r in sorted(best.items())]
    cache[node_id] = out
    return out


def _distances(onto: Any, target: str, limit: int, wanted: Optional[List[str]],
               cache: Dict[str, Any]) -> Dict[str, int]:
    """0-1 BFS costs from ``target`` up to ``limit``; hubs other than the target are reached but not expanded."""
    dist = {target: 0}
    queue: deque = deque([target])
    while queue:
        u = queue.popleft()
        if u != target and onto.registry.is_hub(onto.kind_of(u)):
            continue
        for other, cost, _edge, _label, _way in _path_steps(onto, u, wanted, cache):
            d = dist[u] + cost
            if d > limit or dist.get(other, limit + 1) <= d:
                continue
            dist[other] = d
            if cost == 0:
                queue.appendleft(other)
            else:
                queue.append(other)
    return dist


def _path_step(onto: Any, node_id: str, edge: Optional[Dict[str, Any]] = None, label: Optional[str] = None,
               way: Optional[str] = None) -> Dict[str, Any]:
    """One step of a path: the node reached, the link that reached it (none for the first step) and the marker
    flags of both."""
    step: Dict[str, Any] = {"id": node_id, "edge": edge.get("id") if edge else None, "rel": label, "dir": way,
                            "bridge": bool(edge) and onto.is_bridge(edge)}
    step.update(render.flags(onto.node(node_id)))
    if edge:
        step.update(link_flags(edge))
    return step


def path(onto: Any, a: str, b: str, max_depth: int = 4, k: int = 3, rels: Any = None,
         mcp: Optional[bool] = None) -> Dict[str, Any]:
    """The ``k`` shortest paths from ``a`` to ``b`` of at most ``max_depth`` hops (see the module docstring):
    ``{from, to, paths: [[{id, edge, rel, dir, bridge, ...flags}]], hops: [n...]}``. The first step of a path is
    ``a`` with ``edge`` null; each later step names the link that reached it, read in the walking direction. Each
    step carries the node's marker flags (``untrusted``, ``draft``, ``archived``) and the link's (``link_untrusted``,
    ``link_draft``), as ``get`` and ``neighbors`` do."""
    note_a: Dict[str, Any] = {}
    note_b: Dict[str, Any] = {}
    start = resolve_or_raise(onto, a, res=note_a, mcp=mcp)
    end = resolve_or_raise(onto, b, res=note_b, mcp=mcp)
    try:
        max_depth = max(1, min(8, int(max_depth or 4)))
        k = max(1, min(10, int(k or 3)))
    except (TypeError, ValueError):
        raise UsageError("max_depth and k must be numbers")
    wanted = _rel_filter(rels)
    cache: Dict[str, Any] = {}
    out: Dict[str, Any] = {"from": start, "to": end, "max_depth": max_depth, "k": k, "rels": wanted or [],
                           "paths": [], "hops": [], "truncated": False}
    if note_a.get("resolved"):
        out["resolved_from"] = note_a["resolved"]
    if note_b.get("resolved"):
        out["resolved_to"] = note_b["resolved"]
    if start == end:
        out["paths"] = [[_path_step(onto, start)]]
        out["hops"] = [0]
        return out
    dist = _distances(onto, end, max_depth, wanted, cache)
    if start not in dist:
        return out
    found: List[Tuple[int, List[str], List[Dict[str, Any]]]] = []
    effort = [0]

    def walk(u: str, cost: int, bound: int, trail: List[str], steps: List[Dict[str, Any]], on: Set[str]) -> None:
        if len(found) >= k or effort[0] > MAX_PATH_STEPS:
            return
        if u != start and onto.registry.is_hub(onto.kind_of(u)):
            return
        for other, w, edge, label, way in _path_steps(onto, u, wanted, cache):
            effort[0] += 1
            if other in on:
                continue
            total = cost + w
            if total + dist.get(other, bound + 1) > bound:
                continue
            step = _path_step(onto, other, edge, label, way)
            if other == end:
                if total == bound:
                    found.append((total, trail + [other], steps + [step]))
                    if len(found) >= k:
                        return
                continue
            on.add(other)
            walk(other, total, bound, trail + [other], steps + [step], on)
            on.discard(other)
            if len(found) >= k:
                return

    first = _path_step(onto, start)
    for bound in range(dist[start], max_depth + 1):
        walk(start, 0, bound, [start], [first], {start})
        if len(found) >= k or effort[0] > MAX_PATH_STEPS:
            break
    found.sort(key=lambda f: (f[0], f[1]))
    out["paths"] = [steps for _cost, _trail, steps in found[:k]]
    out["hops"] = [cost for cost, _trail, _steps in found[:k]]
    out["truncated"] = effort[0] > MAX_PATH_STEPS
    return out


# handlers --------------------------------------------------------------------------------------------------------
def _limit(args: Dict[str, Any], default: int) -> int:
    value = args.get("limit")
    return default if value is None else max(0, int(value))


def cmd_search(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """``search``; with ``find``, the read-only finder of ``onto erase --find`` (``mutate.find_text``), which also
    reads quotes, source texts, proposals, decisions and the log (over MCP without the raw input, ``raw=False``)."""
    if args.get("find"):
        from . import mutate

        found = mutate.find_text(ctx.repo, str(args.get("text") or ""), raw=not bool(getattr(ctx, "mcp", False)))
        return {"query": args.get("text") or "", "find": found}
    return search(ctx.onto(), args.get("text") or "", args.get("kinds"), args.get("ns"), args.get("status") or "any",
                  bool(args.get("include_archived")))


def cmd_get(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """``get`` paged: relation groups (and a source's citing records) cut to ``limit`` from ``offset``. A source read
    sends each thing once: a read of a chunk or lines leaves the citing records out (``cited_by_listed: false``;
    the first read lists them), and a read that pages the links (``offset``) names the text part without it."""
    limit, offset = _limit(args, 10), max(0, int(args.get("offset") or 0))
    part = args.get("chunk") is not None or bool(args.get("lines"))
    mcp = bool(getattr(ctx, "mcp", False))
    res = get(ctx.onto(), args.get("id") or "", bool(args.get("full")), args.get("chunk"), args.get("lines"),
              bool(args.get("include_archived")), text=not offset, mcp=mcp,
              text_chars=SOURCE_TEXT_CHARS if mcp else 0)
    out = page_relations(res, limit, offset)
    if res.get("kind") == "source":
        if part and not offset:
            out["cited_by"] = []
            out["cited_by_listed"] = False
        else:
            out["cited_by"] = render.page(res.get("cited_by") or [], limit, offset)[0]
    out["limit"], out["offset"] = limit, offset
    out["include_archived"] = bool(args.get("include_archived"))  # the next-links call pages the same list
    return out


def cmd_neighbors(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    return neighbors(ctx.onto(), args.get("id") or "", args.get("depth") or 1, args.get("rels"), args.get("kinds"),
                     args.get("ns"), bool(args.get("include_archived")), mcp=bool(getattr(ctx, "mcp", False)))


def cmd_path(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    return path(ctx.onto(), args.get("from") or "", args.get("to") or "", args.get("max_depth") or 4,
                args.get("k") or 3, args.get("rels"), mcp=bool(getattr(ctx, "mcp", False)))


# renderers -------------------------------------------------------------------------------------------------------
def _paging(result: Dict[str, Any], key: str) -> Tuple[int, int]:
    """``(total, offset)`` of a paged list key."""
    total = (result.get("totals") or {}).get(key, len(result.get(key) or []))
    paging = result.get("paging") or {}
    offset = int(paging.get("offset") or 0) if paging.get("key", key) == key else 0
    return int(total), offset


def _archived_hint(n: int, mcp: bool) -> List[str]:
    if not n:
        return []
    how = "include_archived=true" if mcp else "--include-archived"
    return ["  %d archived not shown; %s lists %s" % (n, how, "it" if n == 1 else "them")]


def render_search(result: Dict[str, Any], mode: str, ctx: Any) -> List[str]:
    mcp = bool(getattr(ctx, "mcp", False))
    if "find" in result:
        from . import cmd_core

        return cmd_core.render_find(result["find"], mode, mcp)
    total, offset = _paging(result, "results")
    kinds = " ".join("%s=%d" % kv for kv in sorted((result.get("by_kind") or {}).items()))
    lines = ["search %s: %d%s" % (render.quote(str(result.get("query") or "")), total,
                                  " (%s)" % kinds if kinds else "")]
    for r in result.get("results") or []:
        name = plain(r.get("name"), 60 if mode == "compact" else 200)
        if mode == "text":
            lines.append("  %-12s %s  %s  (score %s)" % (r.get("kind"), render.mark(r), name, r.get("score")))
        else:
            lines.append("  %s%s" % (render.mark(r), "  " + name if name else ""))
    more = render.more(total, len(result.get("results") or []), offset)
    if more:
        lines.append("  " + more)
    lines += _archived_hint(int(result.get("archived_hidden") or 0), mcp)
    for e in result.get("records") or []:
        follow = render.call(mcp, "get", id=e.get("id"))
        if e.get("kind") == "edge":
            arrow = "~%s~>" % e.get("rel") if e.get("bridge") else "-%s->" % e.get("rel")
            lines.append("  that is an edge: %s  %s %s %s (%s)" % (render.mark(e), e.get("src"), arrow, e.get("dst"),
                                                                  follow))
        else:
            lines.append("  %s is %s (%s)" % (render.mark(e), plain(e.get("note")), follow))
    hint = result.get("did_you_mean")
    if hint:
        rest = int(hint.get("total") or 0) - len(hint.get("ids") or [])
        lines.append("  did you mean %s? It finds %s%s" % (render.quote(plain(hint["query"])), ", ".join(hint["ids"]),
                                                           ", +%d more" % rest if rest > 0 else ""))
    elif not total and not result.get("archived_hidden") and not result.get("records"):
        query = str(result.get("query") or "")
        lines.append("  not in the ontology (searched ids, names, aliases and text fields; not quotes or source "
                     "texts: %s lists every place a text sits)" % (
                         render.call(True, "search", text=query, find=True) if mcp
                         else render.call(False, "erase", find=query)))
    return lines


def _fact_mark(rec: Dict[str, Any]) -> str:
    """``[untrusted] `` before each fact line of an untrusted record (its summary, attributes, aliases, known
    unknowns, needs or link note), as the head line and a brief's summary line carry it: such a line holds the
    record's own stored text (the asks quote its name), and may hold an instruction or a command the source slipped
    in."""
    return "[untrusted] " if render.flags(rec).get("untrusted") else ""


def _fact_value(value: Any, cuts: render.Cuts, full: bool, width: int = render.WIDTH) -> str:
    """A fact on one line (``plain``): whole in full and text mode, else cut at ``width`` and counted."""
    text = plain(render.fmt(value))
    return text if full else cuts.cut(text, width)


def _prov_line(p: Dict[str, Any], cuts: render.Cuts, full: bool) -> str:
    head = "%s %s by %s" % (plain(p.get("src")), plain(p.get("loc")), plain(p.get("by")))
    if p.get("via"):
        head += " via %s" % plain(p["via"])
    quote = p.get("quote")
    if not quote:
        return "  prov %s" % head
    text = plain(quote)
    shown = text if full else cuts.cut(text, render.WIDTH)
    return "  prov %s: %s\"%s\"" % (head, "[untrusted] " if p.get("untrusted") else "", shown)


def _relation_lines(result: Dict[str, Any], offset: int, mcp: bool) -> List[str]:
    lines = []
    totals = result.get("relation_totals") or {}
    for label, items in (result.get("relations") or {}).items():
        total = int(totals.get(label, len(items)))
        shown = []
        for i in items:
            text = rel_mark(i)
            if i.get("bridge"):
                text += " (bridge)"
            if i.get("count", 1) > 1:
                text += " x%d" % i["count"]
            shown.append(text)
        more = render.more(total, len(items), offset)
        lines.append("  %s (%d): %s%s" % (label, total, ", ".join(shown) or "-", " " + more if more else ""))
    return lines


def _needs_lines(result: Dict[str, Any], mode: str, mcp: bool) -> List[str]:
    needs = result.get("needs")
    if not needs:
        return ["  needs: %s" % result["needs_note"]] if result.get("needs_note") else []
    gaps = needs.get("gaps") or []
    if not gaps:
        return ["  needs: none"]
    shown = gaps if mode == "text" else gaps[:3]
    parts = []
    for g in shown:
        what = plain(g["type"])
        if g.get("field"):
            what += " " + plain(g["field"])
        if g.get("rel"):
            what += " %s(%s)" % (plain(g["rel"]), plain(g.get("dir") or "out"))
        if g.get("ask"):
            what += ": %s" % render.quote(plain(g["ask"], 90 if mode == "compact" else 400))
        parts.append(what)
    rest = len(gaps) - len(shown)
    tail = "; +%d more (%s)" % (rest, render.call(mcp, "gaps", node=result.get("id"))) if rest > 0 else ""
    # the asks quote the record's name and fields: an untrusted record's needs line is marked as its facts are
    return ["  %sneeds (%d): %s%s" % (_fact_mark(result.get("node") or {}), len(gaps), "; ".join(parts), tail)]


def _decision_lines(result: Dict[str, Any], mode: str) -> List[str]:
    items = result.get("decisions") or []
    if not items:
        return []
    width = 70 if mode == "compact" else 400
    return ["  decision %s %s -> %s" % (plain(d.get("id")), render.quote(plain(d.get("question"), width)),
                                         render.quote(plain(decision_choice(d), width))) for d in items]


def _source_render(result: Dict[str, Any], mode: str, mcp: bool) -> List[str]:
    entry = result.get("source") or {}
    meta = [str(entry.get("kind") or "source")]
    if result.get("ns") not in (None, "self"):
        meta.append("in the import %s" % result.get("ns"))
    for key, fmt_ in (("lines", "%s lines"), ("bytes", "%s bytes"), ("captured_at", "captured %s"),
                      ("commit", "commit %s")):
        if entry.get(key) is not None:
            meta.append(fmt_ % (str(entry[key])[:12] if key == "commit" else entry[key]))
    if entry.get("url"):
        meta.append("url %s" % plain(entry["url"]))
    if entry.get("erased"):
        meta.append("erased")
    if entry.get("supersedes"):
        meta.append("supersedes %s" % plain(entry["supersedes"]))
    lines = ["%s  %s  [%s]" % (result["id"], render.mark(entry, plain(entry.get("title"), 80)),
                               plain(", ".join(str(m) for m in meta)))]
    cited = result.get("cited_by") or []
    total = int(result.get("cited_total") or len(cited))
    offset = int(result.get("offset") or 0)
    if result.get("cited_by_listed") is False and total:
        # a chunk or line read: the citing records came with the first read, and are not sent again with each part
        lines.append("  cited by (%d): not repeated with a part of the text (%s lists them)" % (
            total, render.call(mcp, "get", id=result["id"])))
    else:
        shown = ["%s %s" % (render.mark(c), plain(c.get("loc"))) for c in cited]
        more = render.more(total, len(cited), offset)
        lines.append("  cited by (%d): %s%s" % (total, ", ".join(shown) or "nothing yet", " " + more if more else ""))
    lines += _relation_lines(result, offset, mcp)
    lines += _next_links(result, mcp)
    lines += _decision_lines(result, mode)
    text = result.get("text") or {}
    if text.get("missing"):
        lines.append("  text: missing (%s)" % text.get("note"))
        return lines
    if text.get("skipped"):  # a read that pages the links: the text part is named, not sent again
        where = ("chunk %s of %s (%s)" % (text.get("chunk"), text.get("chunks"), text.get("loc"))
                 if text.get("chunk") is not None else "lines %s of %s" % (text.get("loc"), text.get("lines")))
        lines.append("  text: %s not repeated while paging links (%s reads it)" % (
            where, render.call(mcp, "get", id=result["id"], **dict(text.get("read") or {}))))
        return lines
    if text.get("chunk") is not None:
        where = "chunk %s of %s (%s)" % (text.get("chunk"), text.get("chunks"), text.get("loc"))
    else:
        where = "lines %s of %s" % (text.get("loc"), text.get("lines"))
    lines.append("  %s; other chunks: %s" % (where, render.call(mcp, "get", id=result["id"], chunk="N"))
                 if (text.get("chunks") or 0) > 1 else "  %s" % where)
    lines.extend(plain_block(text.get("body")).split("\n"))  # inside its fences; controls removed
    cut = text.get("cut")
    if cut:  # a long part stopped at a whole line: say which lines came and the call that reads on
        shown, asked = parse_lines(text.get("loc")), parse_lines(cut.get("asked"))
        lines.append("[page] source lines %d-%d of %d-%d (cut to fit %s characters); next: %s" % (
            shown[0], shown[1], asked[0], asked[1], cut.get("chars"),
            render.call(mcp, "get", id=result["id"], **dict(cut.get("next") or {}))))
    return lines


def render_get(result: Dict[str, Any], mode: str, ctx: Any) -> List[str]:
    mcp = bool(getattr(ctx, "mcp", False))
    full = bool(result.get("full")) or mode == "text"
    lines = resolved_lines(result)
    if result.get("kind") == "source":
        return lines + _source_render(result, mode, mcp)
    cuts = render.Cuts()
    offset = int(result.get("offset") or 0)
    if result.get("kind") == "edge":
        e = result.get("edge") or {}
        ends = result.get("ends") or {}
        src, dst = ends.get("src") or {}, ends.get("dst") or {}
        arrow = "~%s~>" % e.get("rel") if e.get("bridge") else "-%s->" % e.get("rel")
        head = "%s  %s %s %s  [edge, %s, trust %s, conf %s]" % (
            render.mark(e), rel_mark(src), arrow, rel_mark(dst), e.get("status"), e.get("trust"),
            render.fmt(e.get("conf")))
        lines.append(head)
        lines.append("  read back: %s %s %s" % (dst.get("id"), plain(e.get("inverse")), src.get("id")))
        told = _fact_mark(e)
        for key in ("key", "note", "background"):
            if e.get(key) not in (None, "", False):
                lines.append("  %s%s: %s" % (told, key, _fact_value(e.get(key), cuts, full)))
    else:
        node = result.get("node") or {}
        head = "%s  %s  [%s, %s, trust %s, conf %s%s]" % (
            render.mark(node), plain(node.get("name"), 80), result.get("kind"), node.get("status"),
            node.get("trust"), render.fmt(node.get("conf")),
            ", visibility local" if node.get("visibility") == "local" else "")
        lines.append(head)
        told = _fact_mark(node)
        for key, value in (result.get("facts") or {}).items():
            if key == "summary" and not value:
                lines.append("  summary: (none yet)")
                continue
            lines.append("  %s%s: %s" % (told, plain(key), _fact_value(value, cuts, full)))
        if as_list(node.get("aliases")):
            lines.append("  %saliases: %s" % (told, ", ".join(plain(a) for a in as_list(node.get("aliases")))))
        for g in result.get("gaps") or []:
            lines.append("  %sknown unknown: %s: %s" % (told, plain(g.get("field")),
                                                        _fact_value(g.get("note"), cuts, full)))
        members = result.get("members") or {}
        if members:
            lines.append("  same as: %s" % "; ".join("%s: %s" % (ns, ", ".join(ids)) for ns, ids in members.items()))
    archived = result.get("archived")
    if archived:
        lines.append("  ARCHIVED %s: %s; replaced by %s; decision %s" % (
            plain(archived.get("on")), _fact_value(archived.get("reason"), cuts, full),
            ", ".join(plain(i) for i in as_list(archived.get("superseded_by"))) or "none",
            plain(archived.get("decision")) or "none"))
    for p in result.get("prov") or []:
        lines.append(_prov_line(p, cuts, full))
    lines += _relation_lines(result, offset, mcp)
    lines += _next_links(result, mcp)
    lines += _needs_lines(result, mode, mcp)
    lines += _decision_lines(result, mode)
    if not full:
        lines += cuts.whole_text_line(result["id"], mcp)
    return lines


def _next_links(result: Dict[str, Any], mcp: bool) -> List[str]:
    """``(next links: <call>)`` when a relation group (or a source's citing records) goes on past this page. The call
    keeps ``include_archived`` and ``full``: without the first it would page a shorter list (no archived links), and
    each archived link on the pages read would push one link past both pages."""
    limit = int(result.get("limit") or 0)
    offset = int(result.get("offset") or 0)
    if not limit:
        return []
    totals = dict(result.get("relation_totals") or {})
    lists = dict(result.get("relations") or {})
    if result.get("kind") == "source" and result.get("cited_by_listed") is not False:
        totals["cited_by"] = int(result.get("cited_total") or 0)
        lists["cited_by"] = result.get("cited_by") or []
    if not any(int(totals.get(k, 0)) > offset + len(v) for k, v in lists.items()):
        return []
    args: Dict[str, Any] = {"id": result["id"]}
    if result.get("include_archived"):
        args["include_archived"] = True
    if result.get("full"):
        args["full"] = True
    if limit != 10:
        args["limit"] = limit
    args["offset"] = offset + limit
    return ["  (next links: %s)" % render.call(mcp, "get", **args)]


def render_neighbors(result: Dict[str, Any], mode: str, ctx: Any) -> List[str]:
    mcp = bool(getattr(ctx, "mcp", False))
    lines = resolved_lines(result)
    total, offset = _paging(result, "items")
    kinds = " ".join("%s=%d" % kv for kv in sorted((result.get("by_kind") or {}).items()))
    lines.append("neighbors of %s, depth %d: %d%s" % (result.get("start"), result.get("depth") or 1, total,
                                                      " (%s)" % kinds if kinds else ""))
    members = result.get("members") or {}
    if members:
        lines.append("  same as: %s" % "; ".join("%s: %s" % (ns, ", ".join(ids)) for ns, ids in members.items()))
    items = result.get("items") or []
    if mode == "text":
        for n in items:
            for link in n.get("links") or [n]:  # one line per link that reached the node (a class may have several)
                lines.append("  %d %-20s %-12s %s  %s  (via %s)" % (
                    n["depth"], link["edge"] + (" (background)" if link.get("background") else ""), link["kind"],
                    rel_mark(link) + (" (bridge)" if link.get("bridge") else ""), plain(link.get("name"), 60),
                    link.get("via")))
    else:
        groups: Dict[Tuple[int, bool, str], Dict[str, Tuple[str, str]]] = {}
        for n in items:
            for link in n.get("links") or [n]:  # a class reached by several links is listed under each label
                text = rel_mark(link) + (" (bridge)" if link.get("bridge") else "")
                if n.get("members"):
                    text += " [same as %s]" % ", ".join(m for ids in n["members"].values() for m in ids
                                                       if m != link["id"])
                row = groups.setdefault((n["depth"], bool(link.get("background")), link["edge"]), {})
                row.setdefault(text, (str(link.get("kind")), str(link.get("id"))))
        for (depth, background, label), row in sorted(groups.items(), key=lambda kv: kv[0]):
            ids = sorted(row, key=lambda t: row[t] + (t,))
            lines.append("  %d %s%s: %s" % (depth, label, " (background)" if background else "", ", ".join(ids)))
    more = render.more(total, len(items), offset)
    if more:
        lines.append("  " + more)
    if offset:  # the hubs and hidden counts are the same on every page: the first one says so
        return lines
    if result.get("not_expanded"):
        lines.append("  hubs not expanded: %s" % ", ".join(result["not_expanded"]))
    lines += _archived_hint(int((result.get("hidden") or {}).get("archived") or 0), mcp)
    return lines


def path_text(onto_or_none: Any, steps: List[Dict[str, Any]]) -> str:
    """``a -rel-> b ~rel~> c =same_as= d``: bridges as ``~rel~>``, ``same_as`` hops as ``=same_as=``. Each id carries
    its markers as ``get`` prints a related node (``rel_mark``): ``[untrusted] b (draft)``, or ``c (draft link)``
    when only the link that reached it is a draft."""
    parts = []
    for i, step in enumerate(steps):
        if i:
            rel = str(step.get("rel") or "")
            if rel == "same_as":
                parts.append("=same_as=")
            elif step.get("bridge"):
                parts.append("~%s~>" % rel)
            else:
                parts.append("-%s->" % rel)
        parts.append(rel_mark(step, str(step.get("id"))))
    return " ".join(parts)


def render_path(result: Dict[str, Any], mode: str, ctx: Any) -> List[str]:
    lines = []
    for key in ("resolved_from", "resolved_to"):
        if result.get(key):
            lines += resolved_lines({"resolved": result[key]})
    paths = result.get("paths") or []
    lines.append("paths %s -> %s (at most %d hops): %d" % (result.get("from"), result.get("to"),
                                                            result.get("max_depth") or 4, len(paths)))
    if not paths:
        lines.append("  no path within %d hops over active links%s" % (
            result.get("max_depth") or 4, " of %s" % ", ".join(result["rels"]) if result.get("rels") else ""))
    for hops, steps in zip(result.get("hops") or [], paths):
        bridges = sum(1 for s in steps if s.get("bridge"))
        lines.append("  %d hop%s%s: %s" % (hops, "" if hops == 1 else "s",
                                            ", %d bridge%s" % (bridges, "" if bridges == 1 else "s") if bridges else "",
                                            path_text(None, steps)))
    if result.get("truncated"):
        lines.append("  (search stopped early on a dense graph; narrow it with rels or max_depth)")
    return lines


__all__ = ["search", "get", "neighbors", "path", "resolve_or_raise", "node_brief", "stem", "search_alternatives",
           "did_you_mean", "correct_text", "edit_distance", "relations", "page_relations", "fence", "rel_mark",
           "link_flags", "absent_mark", "plain", "plain_block", "ScopeIndex", "active_decisions", "decision_choice",
           "names", "archived_named", "cmd_search",
           "cmd_get", "cmd_neighbors", "cmd_path", "render_search", "render_get", "render_neighbors", "render_path"]
