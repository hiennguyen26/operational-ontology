"""One-call answers in a token budget: ``brief`` (a subject), ``context`` (a task before writing a deliverable) and
node cards (``card_for``, ``build_cards``).

**Scope** (``resolve_scope``): the subject is tried as an id first (``graph.resolve``: exact, alias, kind prefix,
suffix). An id-shaped subject that matches several ids (the same id in two imports) takes them all as its scope;
one that is absent answers "not in the ontology" with a question to ask, and is never searched as words. Otherwise
the whole phrase is searched; when that finds nothing, each word that is not a stopword is searched and nodes rank
by words matched x 1000 + score. Neither search takes an interview source (its title is the kit's "Answer to
q...."). Up to 3 nodes are primary; every other match is named in ``scope.more``. A phrase or word that names no
active node by id, name or alias but names an archived one lists it in ``scope.archived``, and the scope line names
it with its date, reason, replacement and decision (``crop:mint (archived 2026-09-28: <reason>; decision <id>)``):
it is retired, never "not in the ontology", and the decisions scoped to it are shown. Words that match nothing are
listed as not in the ontology. A JSON result carries the first ``MAX_MORE_SHOWN`` of ``more`` with ``more_total``
and ``more_call`` (the search that lists them all), as the text does.

**Brief units**, in printed order: the scope line (the head, always shown); each primary node (id, kind, status,
confidence, key attributes, relation counts by label, top sources, top 2 needs, and its summary, marked
``[untrusted]`` when the node is); related nodes up to 2 hops over the relations packs mark ``brief``, nearest first
over all primaries, leaving out the ids a primary line already names (an id absent at its pinned import is never
walked through): one hop grouped by label and the node the ids hang from (``related 1 owns via <id>: ...``), the
second hop grouped by label alone (``related 2 has_part via <id>: ...``, or ``via N nodes``); bridges touching the
scope with their ``same_as`` class members; up to 3 quotes (marked ``[untrusted]`` unless they come from an
interview); the active decisions whose scope overlaps the primary nodes or an archived match, each with the choice
in words; and the open gaps from ``needs`` (at most ``MAX_GAPS`` lines, each node marked as ``gaps`` marks it, then
one line naming a ``gaps --node`` call per node left out, which ``omitted`` lists too). ``render.select`` fills the
budget (tokens are characters / 4, the version line counted) by rank, as a strict prefix: the primary nodes, the
decisions, each primary's first quote, the open gaps, the related nodes one hop out, the bridges, the second hop,
then the other quotes; the first unit that does not fit stops the rest, so a larger budget never takes out what a
smaller one showed. The footer names each left-out group (in rank order) with the exact call that returns it, then
the call for everything (``budget=0``); ``omitted`` lists each group's call and at most
``render.MAX_SIMILAR_LISTED`` similar ones, as the footer does. When not one primary summary fits, the node's card
is returned instead (after the scope line, with the decisions) if it fits. ``drafts=false``
hides proposed records, walks no draft link and says how many it hid (a subject named by its id is still shown);
when only drafts match, the scope line says so instead of "not in the ontology".

**Context**: the deliverable template (named, else the one whose section kinds best match the task's subject
nodes, else ``brief``); units for the template headings with the ids per section (essential; at most
``SECTION_IDS`` ids in ``HEADING_ID_CHARS`` characters, then the call that lists the rest), the goals (each with
its key attributes, such as its horizon and success, and the metrics that measure it with their targets:
``goal <id>: <summary> | horizon ...; success ... | measured_by metric:x (target ...)``), the constraints, the
active decisions (for the topic, the task scope, any listed id, or recorded with no scope), the subject nodes, each
followed by the records one hop from it with their summaries (``linked <subject> <label> <id>: <summary>``, at most
``MAX_LINKED``, then the call that lists the rest), and the open points: the gaps of the subjects and the
template's kinds plus the open questions near the task (linked to its scope, then linked to a listed goal,
constraint or linked record, then to a template id, then matching a word of the task), open questions first and
single sources last. The same budget engine takes them by rank, not as a strict prefix: the headings, the decisions
and the goals
(essential, so a lower budget, such as the one a JSON result is refilled at, never drops them for later lines), the
subjects, the constraints near the task, the linked records, the other constraints, then the open points; a
linked record left out is named in the footer with the ``get`` call that returns it. A heading reads "none in the
ontology" only when the ontology holds nothing for it; active decisions outside the task read "none for this scope
(N active: <call>)", and interview sources are never listed as data. In JSON the headings are sent once, as
``template.sections`` (at most ``JSON_IDS`` ids each, with ``more_call`` past that), never again in ``sections``;
only the headings the budget took are sent (``sections_total`` counts them all when some are left out, and
``omitted`` names those with the ``budget=0`` call), as the text shows them.

``budget`` (``tokens``, ``chars``, ``estimated_tokens``) measures the text form of a brief or context, and says so
(``measures: "text"``): a JSON reply carries the same units in a longer form.

Record text (names, summaries, quotes, notes, asks) is printed through ``queries.plain``: one line, control and bidi
characters removed.

**Cards** (C.18): at most 1,000 characters with the follow-up line, for active shared nodes of kinds whose pack
sets ``card: true``. An untrusted record's card marks every line of its own text (head, summary, attrs and the needs
asks, which quote its name) with ``[untrusted]``. ``build/cards.json`` is served only while its ``meta.data_hash``
and ``meta.kit`` match the loaded data; otherwise a card is built from the loaded data and ``source`` says why.

In a topic with imports, briefs and contexts print local ids with the topic's namespace (``g2t/goal:x``) so every id
names its namespace; follow-up calls use the stored ids, and ``queries.resolve_or_raise`` reads both forms.
Deterministic: the same data and arguments give the same bytes.
"""

from __future__ import annotations

import json
import os
import re
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import __version__, queries, render, store, util
from . import ids as idmod
from . import needs as needs_mod
from . import sources as sources_mod
from .errors import NotFound, UsageError

DEFAULT_BUDGET = 1500
CONTEXT_BUDGET = 1000
MCP_BUDGET = 5000  # what budget=0 means over MCP (no cap on the CLI)
MAX_PRIMARY = 3
MAX_QUOTES = 3
MAX_GAPS = 6
MAX_MORE_SHOWN = 6
MAX_ARCHIVED_SHOWN = 3  # archived matches named on a scope line, each with its date, reason and decision
RELATED_PER_LINE = 8
SECTION_IDS = 8  # ids printed per template heading
HEADING_ID_CHARS = 320  # and at most this many characters of them (at least one id), so long ids leave room
JSON_IDS = 20  # ids kept per template section in JSON (``total`` counts them all, ``more_call`` lists them)
MAX_LINKED = 10  # records linked to a context subject printed with their summaries (the rest: one line and a call)
CARD_CHARS = render.CARD_CHARS
CARDS_FILE = "build/cards.json"
SUMMARY = 100
CARD_SUMMARY = 300
STOPWORDS = frozenset(
    "a about all an and any are as at be been but by can could did do does for from had has have how i if in into "
    "is it its me my no not of on or our out over so than that the their them then there these they this those to "
    "up us was we were what when where which who whom why will with would you your".split())
TASK_WORDS = frozenset(
    "write draft prepare make create update produce build compose outline summarize summarise review edit finish "
    "start help need want new next".split())


class Answer(dict):
    """A result dict that also carries its rendered text (``lines``, without the version line). JSON output is the
    dict itself, so the text is never sent twice."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.lines: List[str] = []


# ids as briefs print them ----------------------------------------------------------------------------------------
def shown_id(onto: Any, node_id: str) -> str:
    """``<ns>/<id>`` for a local node in a topic with imports (so every id names its namespace), else the id."""
    if (onto.exports and onto.ns and node_id in onto.nodes and node_id not in onto.virtual
            and onto.ns_of(node_id) == "self"):
        return "%s/%s" % (onto.ns, node_id)
    return node_id


def marked(onto: Any, node_id: str, rec: Optional[Dict[str, Any]] = None) -> str:
    """The shown id with its markers: ``[untrusted] <id> (draft)``."""
    return render.mark(rec if rec is not None else (onto.record(node_id) or {}), shown_id(onto, node_id))


def _quoted(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def summary_text(rec: Dict[str, Any], width: int) -> str:
    """A record's summary on one line (``queries.plain``, cut at ``width``), led by ``[untrusted]`` when the record
    is untrusted, so the line keeps its marker wherever it is printed; ``(no summary yet)`` when it has none."""
    summary = queries.plain(rec.get("summary"), width)
    if not summary:
        return "(no summary yet)"
    return ("[untrusted] " if render.flags(rec).get("untrusted") else "") + summary


def decision_text(d: Dict[str, Any]) -> str:
    """``decision <id> "<question>" -> "<choice>" (active)``: the choice in words (``queries.decision_choice``: the
    user's words, else the chosen option's label), quoted unless it is one plain word."""
    return "decision %s %s -> %s (active)" % (queries.plain(d.get("id")), _quoted(queries.plain(d.get("question"), 80)),
                                              render.quote(queries.plain(queries.decision_choice(d), 60)))


def _is_draft(onto: Any, record_id: str) -> bool:
    rec = onto.record(record_id)
    return bool(rec) and rec.get("status") == "proposed"


# scope -----------------------------------------------------------------------------------------------------------
def id_shaped(text: str) -> bool:
    """True for text in the id grammar: ``kind:slug``, ``ns/kind:slug`` or a record id (``src-...``, ``e:...``)."""
    low = (text or "").strip().lower()
    return bool(idmod.LOCAL_RE.match(low) or idmod.QUAL_RE.match(low) or idmod.is_record(low))


def _ask(onto: Any, text: str) -> str:
    """The interview question that would fill a miss."""
    thing = text.strip()
    if id_shaped(thing) and ":" in thing:
        thing = thing.rsplit(":", 1)[1].replace("-", " ")
    return "What is %s, and how does it fit %s?" % (thing, onto.title())


def task_words(text: str, skip_words: Sequence[str] = ()) -> List[str]:
    """The words of ``text`` a scope search uses: 3 letters or more, not a stopword, not in ``skip_words``."""
    return [w for w in re.findall(r"[A-Za-z0-9]+", (text or "").lower())
            if len(w) > 2 and w not in STOPWORDS and w not in skip_words]


def interview_source(onto: Any, record_id: str) -> bool:
    """True for an interview source (an answer, or the title given at init): the user's own words, cited by the
    records they made. Its title is written by the kit ("Answer to q.frame.goal"), so a word search never takes it
    into a scope, and a context never lists it as data."""
    return record_id in onto.virtual and str((onto.sources.get(record_id) or {}).get("kind")) == "interview"


def _archived_item(onto: Any, node_id: str) -> Dict[str, Any]:
    """An archived node as a scope names it: ``{id, on, reason, superseded_by, decision}`` from its archive block,
    plus its marker flags."""
    rec = onto.node(node_id) or {}
    block = queries.as_dict(rec.get("archived"))
    later = [str(i) for i in queries.as_list(block.get("superseded_by"))]
    item: Dict[str, Any] = OrderedDict([("id", node_id), ("on", block.get("on")), ("reason", block.get("reason")),
                                        ("superseded_by", later), ("decision", block.get("decision"))])
    item.update(render.flags(rec))
    return item


def _archived_text(onto: Any, item: Dict[str, Any]) -> str:
    """``crop:mint (archived 2026-09-28: <reason>; replaced by <ids>; decision <id>)``, led by ``[untrusted]`` when
    the record is untrusted."""
    head = " ".join(["archived"] + ([queries.plain(item["on"])] if item.get("on") else []))
    reason = queries.plain(item.get("reason"), 60)
    parts = [head + (": %s" % reason if reason else "")]
    if item.get("superseded_by"):
        parts.append("replaced by %s" % ", ".join(shown_id(onto, i) for i in item["superseded_by"]))
    if item.get("decision"):
        parts.append("decision %s" % queries.plain(item["decision"]))
    text = "%s (%s)" % (shown_id(onto, item["id"]), "; ".join(parts))
    return ("[untrusted] " if item.get("untrusted") else "") + text


def archived_ids(scope: Dict[str, Any]) -> List[str]:
    """The ids of the archived nodes a scope names: decisions scoped to them hold for the brief or context too."""
    return [str(a["id"]) for a in scope.get("archived") or []]


def resolve_scope(onto: Any, subject: str, drafts: bool = True, skip_words: Sequence[str] = ()) -> Dict[str, Any]:
    """What a brief or context is about: ``{input: {text, mode}, primary, more, not_in_ontology, archived,
    resolved, ask, did_you_mean, hidden}``. ``mode`` is one of:

    - ``id``: the subject names one record (an edge id briefs its two ends);
    - ``ambiguous``: an id-shaped subject that matches several ids (the same id in two imports, say); they are the
      scope, up to 3 primary and the rest in ``more``;
    - ``import_source``: a source that lives in an import (its records cite it; only ``get`` reads it);
    - ``missing``: an id-shaped subject that is absent (never searched as words);
    - ``phrase`` or ``words``: the whole phrase, or failing that each word, found nodes;
    - ``drafts_only``: without ``drafts``, every match was a draft (so nothing is "not in the ontology");
    - ``archived_only``: every word that matched names an archived record (so it is retired, not missing);
    - ``none``: nothing matched, and ``ask`` holds the interview question that would fill it.

    ``archived`` (``_archived_item``) lists the archived nodes the phrase, or a word, names by id, name or alias
    (``queries.archived_named``) when no active match is named by it: search leaves archived records out, and
    without this a retired subject would read "not in the ontology" and the decision that retired it would not be
    shown. Such a word is never listed as not in the ontology.

    ``skip_words`` are ignored in the word search (context drops task verbs such as "write"). Without ``drafts``,
    proposed nodes found by search are left out and listed in ``hidden``; a subject named by its id is kept. The
    phrase and word searches never take an interview source (``interview_source``); its id still names it."""
    text = (subject or "").strip()
    if not text:
        raise UsageError("give a subject: an id or a few words")
    scope: Dict[str, Any] = OrderedDict([("input", {"text": text, "mode": None}), ("primary", []), ("more", []),
                                         ("not_in_ontology", []), ("archived", []), ("resolved", None),
                                         ("ask", None), ("did_you_mean", None), ("hidden", [])])
    hidden: Set[str] = set()
    lookup = queries.local_text(onto, text)
    try:
        found = onto.resolve(lookup)
    except NotFound:  # a qualified id whose import does not hold it
        found = {"id": None, "candidates": [], "ambiguous": False, "note": None}
    ranked: List[str] = []
    mode = None
    upstream = None if found["id"] else queries.import_source(onto, lookup)
    if found["id"]:
        rid = found["id"]
        mode = "id"
        if rid in onto.edges and onto.node(rid) is None:
            edge = onto.edges[rid]
            ranked = [i for i in (str(edge.get("src")), str(edge.get("dst"))) if onto.node(i) is not None]
            scope["input"]["edge"] = rid
        else:
            ranked = [rid]
        if rid != lookup and rid.lower() != lookup.lower():
            scope["resolved"] = {"query": text, "id": rid, "also": queries._also(onto, lookup, rid)}
            if found.get("note"):
                scope["resolved"]["note"] = found["note"]
    elif upstream is not None:
        mode = "import_source"
        scope["input"]["source"] = OrderedDict([("id", upstream[1]["id"]), ("ns", upstream[0]),
                                                ("note", queries.import_source_text(onto, upstream))])
    elif id_shaped(lookup) and found.get("ambiguous") and found.get("candidates"):
        mode = "ambiguous"  # the same id in two imports, say: every match is the scope, never "not in the ontology"
        ranked = list(found["candidates"])
    elif id_shaped(lookup):
        mode = "missing"
        scope["not_in_ontology"] = [text]
        if found.get("candidates"):
            scope["did_you_mean"] = list(found["candidates"])[:5]
    else:
        words = task_words(text, skip_words)
        # a task ("write the weekly menu") is searched without its verbs and stopwords ("weekly menu")
        phrase = " ".join(words) if skip_words and words else text
        archived: List[str] = []

        def visible(ids: List[str]) -> List[str]:
            if drafts:
                return ids
            hidden.update(i for i in ids if _is_draft(onto, i))
            return [i for i in ids if not _is_draft(onto, i)]

        retired_by: List[str] = []  # the phrase or words that named them (the search that lists them all)

        def retired(piece: str, piece_hits: List[str]) -> bool:
            """Add to ``archived`` the archived nodes ``piece`` names when no active match is named by it (a
            match only in a summary does not count); True when there are any."""
            if any(queries.names(onto, h, piece) for h in piece_hits[:3]):
                return False
            gone = queries.archived_named(onto, piece)
            archived.extend(i for i in gone if i not in archived)
            if gone:
                retired_by.append(piece)
            return bool(gone)

        res = queries.search(onto, phrase)
        hits = [r["id"] for r in res["results"] if not interview_source(onto, r["id"])]
        scope["input"]["search"] = phrase
        if hits:
            ranked = visible(hits)
            mode = "phrase" if ranked else "drafts_only"
            retired(phrase, hits)
        else:
            tally: Dict[str, List[int]] = {}
            missing = []
            matched = []
            for word in dict.fromkeys(words):
                word_hits = [r for r in queries.search(onto, word, suggest=False)["results"]
                             if not interview_source(onto, r["id"])]
                if retired(word, [r["id"] for r in word_hits]) and not word_hits:
                    continue  # the word names an archived record: it is in the ontology, retired
                if not word_hits:
                    missing.append(word)
                    continue
                matched.append(word)
                for r in word_hits:
                    if r["id"] not in visible([r["id"]]):
                        continue
                    entry = tally.setdefault(r["id"], [0, 0])
                    entry[0] += 1
                    entry[1] += int(r["score"])
            ranked = sorted(tally, key=lambda i: (-(tally[i][0] * 1000 + tally[i][1]), len(i), i))
            mode = "words" if ranked else ("drafts_only" if matched else ("archived_only" if archived else "none"))
            scope["input"]["search"] = " OR ".join(matched) if matched else phrase
            scope["not_in_ontology"] = missing if (missing or matched or archived) else [text]
            if mode == "none" and res.get("did_you_mean"):
                scope["did_you_mean"] = list(res["did_you_mean"].get("ids") or [])
        scope["archived"] = [_archived_item(onto, i) for i in archived]
        if retired_by:
            scope["input"]["archived_search"] = " OR ".join(retired_by)
    scope["input"]["mode"] = mode
    if mode == "drafts_only":
        scope["input"]["drafts_hidden"] = len(hidden)
    scope["primary"] = ranked[:MAX_PRIMARY]
    scope["more"] = ranked[MAX_PRIMARY:]
    if mode in ("missing", "none"):
        scope["ask"] = _ask(onto, " ".join(scope["not_in_ontology"]) or text)
    scope["hidden"] = sorted(hidden)
    return scope


def _scope_lines(onto: Any, scope: Dict[str, Any], what: str, mcp: bool) -> List[str]:
    """The head line: ``scope "<text>" -> ids | not in the ontology: "word"``."""
    text = scope["input"]["text"]
    mode = scope["input"]["mode"]
    head = "%s %s" % (what, _quoted(text))
    absent = ""
    if scope.get("not_in_ontology") and mode not in ("missing", "none"):
        absent = " | not in the ontology: %s" % ", ".join(_quoted(w) for w in scope["not_in_ontology"])
    retired = _archived_part(onto, scope, mcp)
    if mode == "archived_only":
        return ["%s: only archived records match: %s%s" % (head, retired, absent)]
    if retired:
        absent = " | archived: %s%s" % (retired, absent)
    if mode in ("missing", "none"):
        searched = ("ids, aliases and id suffixes" if mode == "missing"
                    else "ids, names and text: the whole phrase, then each word")
        line = "%s: not in the ontology (searched %s)" % (head, searched)
        if scope.get("did_you_mean"):
            line += "; did you mean %s" % ", ".join(scope["did_you_mean"])
        if scope.get("ask"):
            line += "; ask the user: %s" % _quoted(scope["ask"])
        return [line]
    if mode == "import_source":
        src = scope["input"].get("source") or {}
        return ["%s: %s is %s (%s)" % (head, src.get("id"), src.get("note"),
                                       render.call(mcp, "get", id=src.get("id")))]
    if mode == "drafts_only":
        return ["%s: only drafts match (%d hidden; %s shows them)%s" % (
            head, int(scope["input"].get("drafts_hidden") or 0), render.call(mcp, "brief", subject=text), absent)]
    shown = [marked(onto, i) for i in scope["primary"]]
    if mode == "ambiguous":
        line = "%s -> ambiguous: matches %s" % (head, ", ".join(shown))
    else:
        line = "%s -> %s" % (head, ", ".join(shown))
    if scope["input"].get("edge"):
        line += " (the ends of %s)" % scope["input"]["edge"]
    resolved = scope.get("resolved")
    if resolved and resolved.get("note"):
        line += " (%s)" % resolved["note"]
    more = scope.get("more") or []
    if more:
        listed = ", ".join(shown_id(onto, i) for i in more[:MAX_MORE_SHOWN])
        rest = int(scope.get("more_total") or len(more)) - MAX_MORE_SHOWN
        line += " (more: %s%s)" % (listed, "; +%d more (%s)" % (rest, _more_search(scope, mcp)) if rest > 0 else "")
    return [line + absent]


def _more_search(scope: Dict[str, Any], mcp: bool) -> str:
    """The search call that lists every match of a scope."""
    return render.call(mcp, "search", text=scope["input"].get("search") or scope["input"]["text"])


def _archived_search(scope: Dict[str, Any], mcp: bool) -> str:
    """The search call that lists every archived match of a scope: the phrase or words that named them
    (``input.archived_search``), archived records included."""
    text = scope["input"].get("archived_search") or scope["input"].get("search") or scope["input"]["text"]
    return render.call(mcp, "search", text=text, include_archived=True)


def _archived_part(onto: Any, scope: Dict[str, Any], mcp: bool) -> str:
    """The archived nodes a scope names (``_archived_text``, at most ``MAX_ARCHIVED_SHOWN``, then ``+N more`` with
    the search that lists them), or ``""``."""
    items = list(scope.get("archived") or [])
    if not items:
        return ""
    text = ", ".join(_archived_text(onto, a) for a in items[:MAX_ARCHIVED_SHOWN])
    total = int(scope.get("archived_total") or len(items))
    if total > MAX_ARCHIVED_SHOWN:
        text += "; +%d more archived (%s)" % (total - MAX_ARCHIVED_SHOWN, _archived_search(scope, mcp))
    return text


def json_scope(scope: Dict[str, Any], mcp: bool) -> Dict[str, Any]:
    """The scope as a JSON result carries it: ``more`` cut to the ``MAX_MORE_SHOWN`` ids the text names, with
    ``more_total`` (every match) and, when some are cut, ``more_call`` (the search that lists them all). A broad
    subject can match hundreds of nodes; the whole list would crowd out the budgeted content. ``archived`` is cut
    the same way (``archived_total``, ``archived_call``)."""
    more = list(scope.get("more") or [])
    out: Dict[str, Any] = OrderedDict(scope)
    out["more"] = more[:MAX_MORE_SHOWN]
    out["more_total"] = int(scope.get("more_total") or len(more))
    if out["more_total"] > MAX_MORE_SHOWN:
        out["more_call"] = _more_search(scope, mcp)
    archived = list(scope.get("archived") or [])
    if len(archived) > MAX_MORE_SHOWN:
        out["archived"] = archived[:MAX_MORE_SHOWN]
        out["archived_total"] = len(archived)
        out["archived_call"] = _archived_search(scope, mcp)
    return out


# brief pieces ----------------------------------------------------------------------------------------------------
def _relation_counts(onto: Any, node_id: str, drafts: bool = True,
                     hidden: Optional[Set[str]] = None) -> "OrderedDict[str, List[Dict[str, Any]]]":
    """``{label: [related node brief plus link flags]}`` over active non-background links, one row per id. The
    ``same_as`` links of a class are left out: the class is printed as its own "same as" line. Without ``drafts``,
    a draft node or link is left out and its id added to ``hidden``."""
    groups: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    for e in sorted(onto.edges_of(node_id), key=lambda x: (x["label"], x["other"], str(x["edge"].get("id")))):
        edge = e["edge"]
        if edge.get("background"):
            continue
        if not drafts and (edge.get("status") == "proposed" or _is_draft(onto, e["other"])):
            if hidden is not None:
                hidden.update(i for i in (str(edge.get("id")), e["other"]) if _is_draft(onto, i))
            continue
        if edge.get("rel") == "same_as" and onto.same_as.get(e["other"]) == onto.same_as.get(node_id):
            continue
        rows = groups.setdefault(e["label"], [])
        if any(r["id"] == e["other"] for r in rows):
            continue
        row = queries.node_brief(onto, e["other"])
        row.update(queries.link_flags(edge))
        rows.append(row)
    return groups


def _rel_text(onto: Any, row: Dict[str, Any]) -> str:
    return queries.rel_mark(row, shown_id(onto, row["id"]))


def _key_attrs(rec: Dict[str, Any], n: int = 3, width: int = 40) -> List[str]:
    """``key value`` for the first ``n`` set attributes by key, each value cut at ``width`` characters."""
    out = []
    for key, value in sorted(queries.as_dict(rec.get("attrs")).items()):
        if value in (None, "", [], {}):
            continue
        out.append("%s %s" % (queries.plain(key, 40), queries.plain(render.fmt(value), width)))
        if len(out) >= n:
            break
    return out


def _sources_of(rec: Dict[str, Any]) -> "OrderedDict[str, int]":
    """``{src: quotes}`` in citation order."""
    out: "OrderedDict[str, int]" = OrderedDict()
    for p in queries.as_list(rec.get("prov")):
        if isinstance(p, dict) and p.get("src"):
            out[str(p["src"])] = out.get(str(p["src"]), 0) + (1 if str(p.get("quote") or "").strip() else 0)
    return out


def _gap_label(g: Dict[str, Any], node_text: str = "") -> str:
    """``missing_relation owns(in)``: the gap type, the node (when given), and its field or relation."""
    parts = [queries.plain(g["type"])] + ([node_text] if node_text else [])
    if g.get("field"):
        parts.append(queries.plain(g["field"]))
    if g.get("rel"):
        parts.append("%s(%s)" % (queries.plain(g["rel"]), queries.plain(g.get("dir") or "out")))
    return " ".join(parts)


def _gap_text(onto: Any, node_id: str, g: Dict[str, Any], with_id: bool = True, width: int = 90) -> str:
    """The gap label, then ``-> ask "<the question that closes it>"``. The node is printed with its markers
    (``[untrusted] <id> (draft)``, as ``gaps`` prints it): the ask quotes the node's name, which is the record's own
    text."""
    text = _gap_label(g, marked(onto, node_id) if with_id else "")
    if g.get("ask"):
        text += " -> ask %s" % _quoted(queries.plain(g["ask"], width))
    return text


def _needs(onto: Any, node_id: str, every: bool = False) -> Optional[Dict[str, Any]]:
    """``needs`` of one active node, read from ``needs.all_needs`` when that cache is filled (the version line's
    richness fills it) or when ``every`` asks to fill it; imported nodes are computed on their own."""
    if node_id in onto.virtual or onto.node(node_id) is None or not onto.active(node_id):
        return None
    cached = onto._cache.get("needs")
    if cached is None and every:
        cached = _all_needs(onto)
    if cached is not None and node_id in cached:
        return cached[node_id]
    return queries.safe_needs(onto, node_id)


def _all_needs(onto: Any) -> Optional[Dict[str, Any]]:
    """``needs.all_needs``, or None when a malformed record stops it (each node is then computed on its own)."""
    try:
        return needs_mod.all_needs(onto)
    except (AttributeError, TypeError, ValueError, KeyError):
        return None


def _primary_unit(onto: Any, node_id: str, mcp: bool, drafts: bool = True,
                  hidden: Optional[Set[str]] = None) -> render.Unit:
    rec = onto.node(node_id) or {}
    kind = onto.kind_of(node_id)
    item: Dict[str, Any] = OrderedDict([("id", node_id), ("shown", shown_id(onto, node_id)), ("kind", kind),
                                        ("name", rec.get("name")), ("status", rec.get("status")),
                                        ("conf", rec.get("conf")), ("summary", rec.get("summary") or "")])
    item.update(render.flags(rec))
    if node_id in onto.virtual:
        entry = onto.sources.get(node_id) or {}
        cited = sorted({rid for rid, _loc in onto.prov_index.get(node_id, [])})
        head = "%s  source, %s | %s | cited by %d record%s" % (
            marked(onto, node_id, rec), queries.plain(entry.get("kind")),
            _quoted(queries.plain(entry.get("title"), 60)), len(cited), "" if len(cited) == 1 else "s")
        item["cited_by"] = cited
        lines = [head]
        return render.Unit("primary", lines, item, "primary",
                           render.call(mcp, "get", id=node_id), essential=True)
    parts = ["%s  %s, %s, conf %s" % (marked(onto, node_id, rec), onto.registry.display(kind),
                                      rec.get("status"), render.fmt(rec.get("conf")))]
    attrs = _key_attrs(rec)
    if attrs:
        parts.append("; ".join(attrs))
    rels = _relation_counts(onto, node_id, drafts, hidden)
    item["relations"] = OrderedDict((label, len(ids)) for label, ids in rels.items())
    named: Set[str] = set()  # the linked ids this line names, which the related lines leave out
    if rels:
        rel_parts = []
        for label, rows in list(rels.items())[:5]:
            if len(rows) <= 2:
                rel_parts.append("%s %s" % (label, ", ".join(_rel_text(onto, r) for r in rows)))
                named.update(r["id"] for r in rows)
            else:
                rel_parts.append("%s %d" % (label, len(rows)))
        if len(rels) > 5:
            rel_parts.append("+%d more labels" % (len(rels) - 5))
        parts.append("; ".join(rel_parts))
    srcs = _sources_of(rec)
    item["sources"] = list(srcs)
    if srcs:
        top = ["%s%s" % (s, " (%d quote%s)" % (q, "" if q == 1 else "s") if q else "") for s, q in
               list(srcs.items())[:2]]
        parts.append("%d source%s: %s%s" % (len(srcs), "" if len(srcs) == 1 else "s", ", ".join(top),
                                            " +%d more" % (len(srcs) - 2) if len(srcs) > 2 else ""))
    else:
        parts.append("no sources")
    need = _needs(onto, node_id)
    top_needs = (need or {}).get("gaps", [])[:2]
    item["needs"] = top_needs
    if top_needs:
        parts.append("needs: %s" % ", ".join(_gap_label(g) for g in top_needs))
    lines = [" | ".join(parts)]
    lines.append("  %s" % summary_text(rec, SUMMARY))
    archived = rec.get("archived")
    if isinstance(archived, dict):
        lines.append("  archived %s: %s; replaced by %s" % (
            queries.plain(archived.get("on")), queries.plain(archived.get("reason"), 60),
            ", ".join(shown_id(onto, str(i)) for i in queries.as_list(archived.get("superseded_by"))) or "none"))
    members = queries.members_by_ns(onto, node_id, drafts)
    if not drafts and hidden is not None:
        hidden.update(_class_drafts(onto, node_id))
    if members:
        item["members"] = members
        lines.append("  same as: %s" % "; ".join("%s: %s" % (ns, ", ".join(ids)) for ns, ids in members.items()))
        settled = _settled_conflicts(onto, node_id)
        if settled:  # the class disagrees on these fields, and the user said what holds (or left it open)
            item["settled_conflicts"] = settled
            lines.extend("  %s" % _settled_text(c) for c in settled[:3])
            if len(settled) > 3:
                lines.append("  +%d more settled conflicts (%s)" % (len(settled) - 3,
                                                                    render.call(mcp, "decisions", scope=[node_id])))
    unit = render.Unit("primary", lines, item, "primary", render.call(mcp, "get", id=node_id), essential=True)
    unit.named = named  # type: ignore[attr-defined]
    return unit


def _settled_conflicts(onto: Any, node_id: str) -> List[Dict[str, Any]]:
    """``[{field, by}]`` of the class's settled conflicts (``needs.settled_conflicts``), without the disagreeing
    values (record text a line would have to mark); empty when the check fails (a brief never fails on it)."""
    try:
        return [{"field": str(c["field"]), "by": str(c["by"])} for c in needs_mod.settled_conflicts(onto, node_id)]
    except Exception:
        return []


def _settled_text(c: Dict[str, Any]) -> str:
    """``conflict attrs.x settled by dec-...`` or ``conflict attrs.x left open by the user (n/a)``."""
    if c.get("by") == "n/a":
        return "conflict %s left open by the user (n/a)" % c["field"]
    return "conflict %s settled by %s" % (c["field"], c["by"])


def _class_drafts(onto: Any, node_id: str) -> Set[str]:
    """The proposed records of a node's ``same_as`` class: draft members and draft ``same_as`` links."""
    out: Set[str] = set()
    for m in onto.members(node_id):
        if _is_draft(onto, m):
            out.add(m)
        for e in onto.edges_of(m, rels=["same_as"]):
            if e["edge"].get("status") == "proposed":
                out.add(str(e["edge"].get("id")))
    return out


def _drafts_around(onto: Any, node_id: str) -> bool:
    """True when a node's card would show a draft: a draft link or draft neighbour, or a draft in its class."""
    for e in onto.edges_of(node_id):
        if e["edge"].get("status") == "proposed" or _is_draft(onto, e["other"]):
            return True
    return bool(_class_drafts(onto, node_id))


def _brief_relations(onto: Any) -> List[str]:
    names = []
    for rel in onto.registry.relations():
        if onto.registry.is_brief(rel):
            bare = rel.split("/", 1)[-1]
            if bare not in names:
                names.append(bare)
    return names


def _related(onto: Any, primary: List[str], drafts: bool, hidden: Set[str], named: Sequence[str] = (),
             reached: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
    """Nodes up to 2 hops from the primary nodes over ``brief`` relations, nearest first. Left out: the ids a
    primary line already names (``named``), the primary nodes' own ``same_as`` members, background links and ids
    with no record (a bridge end absent at the pinned import is named on the primary line and in the open gaps,
    never as a related node). Every primary is walked before any id is kept, so an id takes its least depth from
    any primary (the first primary breaks a tie), and ``via`` names the node it hangs from. ``reached`` gets every
    id the walk reached, the named ones included (the bridges near the scope). Without ``drafts`` the walk crosses
    no draft link or draft node (``queries.neighbors(drafts=False)``), and the proposed records it left out go into
    ``hidden``."""
    rels = _brief_relations(onto)
    if not rels:
        return []
    core: Set[str] = set()
    for pid in primary:
        core.update(queries.class_members(onto, pid, drafts))
    skip = core | set(named)
    best: Dict[str, Tuple[int, int, Dict[str, Any]]] = {}
    for order, pid in enumerate(primary):
        res = queries.neighbors(onto, pid, 2, rels=rels, drafts=drafts)
        hidden.update(res.get("drafts_hidden") or [])
        for it in res["items"]:
            # a class reached by several links (``links``) is related through its first link that is not background
            walked = [link for link in it.get("links") or [it] if not link.get("background")]
            if it["id"] in core or not walked or it.get("dangling"):
                continue
            if walked[0].get("edge_id") != it.get("edge_id"):
                it = {k: v for k, v in it.items() if k not in queries.LINK_FIELDS}
                it.update((k, walked[0][k]) for k in queries.LINK_FIELDS if k in walked[0])
            if not drafts and (it.get("draft") or it.get("link_draft")):  # the walk already leaves these out
                hidden.update(i for i in (it["id"], str(it.get("edge_id"))) if _is_draft(onto, i))
                continue
            if reached is not None:
                reached.add(it["id"])
            if it["id"] in skip:
                continue
            known = best.get(it["id"])
            if known is None or (it["depth"], order) < known[:2]:
                best[it["id"]] = (it["depth"], order, dict(it, root=pid))
    items = [entry[2] for entry in best.values()]
    items.sort(key=lambda i: (i["depth"], i["edge"], shown_id(onto, str(i.get("via"))), i["id"]))
    return items


def _related_units(onto: Any, items: List[Dict[str, Any]], primary: Sequence[str],
                   mcp: bool) -> List[render.Unit]:
    """``related 1 <label> via <node>: <ids>``, grouped by label and the node they hang from (``via`` left out only
    where it can be nothing else: a direct link of the one primary node), then ``related 2 <label> via <node>:
    <ids>`` grouped by label alone: ``via <node>`` when one node holds them all, else ``via N nodes`` (the related 1
    lines and the primary lines name those nodes), so a hub's second hop costs a line per label, not per node."""
    groups: "OrderedDict[Tuple[int, str, str], List[Dict[str, Any]]]" = OrderedDict()
    for it in items:
        via = str(it.get("via") or it["root"])
        groups.setdefault((it["depth"], it["edge"], via if it["depth"] == 1 else ""), []).append(it)
    units = []
    for (depth, label, via), members in groups.items():
        vias = list(OrderedDict.fromkeys(str(m.get("via") or m["root"]) for m in members))
        if len(vias) > 1:
            members = sorted(members, key=lambda m: shown_id(onto, m["id"]))
        if depth == 1 and len(primary) == 1 and via == primary[0]:
            where = ""
        elif len(vias) == 1:
            where = " via %s" % marked(onto, vias[0])
        else:
            where = " via %d nodes" % len(vias)
        for start in range(0, len(members), RELATED_PER_LINE):
            chunk = members[start:start + RELATED_PER_LINE]
            line = "related %d %s%s: %s" % (depth, label, where, ", ".join(queries.rel_mark(m, shown_id(onto, m["id"]))
                                                                        for m in chunk))
            follow = render.call(mcp, "neighbors", id=chunk[0]["root"], depth=2)
            item = {"depth": depth, "label": label, "via": vias[0] if len(vias) == 1 else "",
                    "ids": [m["id"] for m in chunk]}
            if len(vias) > 1:
                item["vias"] = vias
            units.append(render.Unit("related", [line], item, "related", follow, count=len(chunk)))
    return units


def _bridge_text(onto: Any, edge: Dict[str, Any]) -> str:
    rel = str(edge.get("rel") or "")
    src, dst = str(edge.get("src")), str(edge.get("dst"))
    if onto.symmetric(edge):
        arrow = "=%s=" % rel
    else:
        arrow = "-%s->" % rel
    text = "%s %s %s" % (marked(onto, src), arrow, marked(onto, dst))
    if edge.get("status") == "proposed":
        text += " (draft)"
    if edge.get("trust") == "untrusted":
        text = "[untrusted] " + text
    return text


def _bridge_units(onto: Any, primary: List[str], reached: Set[str], drafts: bool, hidden: Set[str],
                  mcp: bool) -> List[render.Unit]:
    """The bridges touching the primary nodes (first) or a node the related walk reached (``reached``, the ids a
    primary line names included), with their ``same_as`` classes."""
    core: Set[str] = set()
    for pid in primary:
        core.update(queries.class_members(onto, pid, drafts))
    near = core | set(reached)
    for rid in list(near):
        near.update(queries.class_members(onto, rid, drafts))
    picked = []
    for eid in sorted(onto.bridges):
        edge = onto.edges.get(eid) or {}
        if edge.get("background") or not onto.active(eid):
            continue
        src, dst = str(edge.get("src")), str(edge.get("dst"))
        if not (onto.active(src) and onto.active(dst)) or not (src in near or dst in near):
            continue
        if not drafts and (edge.get("status") == "proposed" or _is_draft(onto, src) or _is_draft(onto, dst)):
            hidden.update(i for i in (eid, src, dst) if _is_draft(onto, i))
            continue
        picked.append((0 if (src in core or dst in core) else 1, eid, edge))
    picked.sort(key=lambda p: (p[0], p[1]))
    units = []
    for _rank, eid, edge in picked:
        line = "bridge %s" % _bridge_text(onto, edge)
        classes = []
        for end in (str(edge.get("src")), str(edge.get("dst"))):
            members = queries.class_members(onto, end, drafts)
            if len(members) > (2 if edge.get("rel") == "same_as" else 1):
                groups = queries.members_by_ns(onto, end, drafts)
                classes.append("; ".join("%s: %s" % (ns, ", ".join(ids)) for ns, ids in groups.items()))
        if classes:
            line += " (class %s)" % " | ".join(dict.fromkeys(classes))
        units.append(render.Unit("bridges", [line], {"edge": eid, "src": edge.get("src"), "rel": edge.get("rel"),
                                                     "dst": edge.get("dst"), "status": edge.get("status")},
                                 "bridges", render.call(mcp, "get", id=eid)))
    return units


def _quote_units(onto: Any, primary: List[str], mcp: bool) -> List[render.Unit]:
    per_node = []
    for pid in primary:
        rec = onto.node(pid) or {}
        items = [p for p in queries.prov_items(onto, rec, onto.ns_of(pid)) if str(p.get("quote") or "").strip()]
        items.sort(key=lambda p: (bool(p.get("untrusted")),))  # the user's own words first
        per_node.append((pid, items))
    ordered: List[Tuple[str, Dict[str, Any]]] = []
    depth = 0
    while any(depth < len(items) for _pid, items in per_node):
        for pid, items in per_node:
            if depth < len(items):
                ordered.append((pid, items[depth]))
        depth += 1
    units = []
    firsts: Set[str] = set()
    for pid, p in ordered[:MAX_QUOTES]:
        text = queries.plain(p.get("quote"), render.WIDTH)
        line = "quote %s: %s%s (%s %s)" % (shown_id(onto, pid), "[untrusted] " if p.get("untrusted") else "",
                                           _quoted(text), queries.plain(p.get("src")), queries.plain(p.get("loc")))
        unit = render.Unit("quotes", [line], dict(p, record=pid), "quotes", render.call(mcp, "get", id=pid, full=True))
        unit.first = pid not in firsts  # type: ignore[attr-defined]  # a node's first quote ranks with decisions
        firsts.add(pid)
        units.append(unit)
    rest = ordered[MAX_QUOTES:]
    if rest:
        by_node: "OrderedDict[str, int]" = OrderedDict()
        for pid, _p in rest:
            by_node[pid] = by_node.get(pid, 0) + 1
        calls = [render.call(mcp, "get", id=pid, full=True) for pid in by_node]
        line = "quotes: +%d more (%s)" % (len(rest), "; ".join(calls))
        units.append(render.Unit("quotes", [line], {"more": len(rest), "records": list(by_node)}, "quotes",
                                 calls[0] if len(calls) == 1 else "", count=len(rest)))
    return units


def _decision_units(onto: Any, scope_ids: List[str], mcp: bool) -> List[render.Unit]:
    units = []
    for d in queries.decisions_for(onto, scope_ids):
        line = decision_text(d)
        units.append(render.Unit("decisions", [line], d, "decisions",
                                 render.call(mcp, "decisions", scope=queries.decision_scope(onto, scope_ids))))
    return units


CONTEXT_TIERS = {"open_question": 0, "single_source": 2}  # context: open questions first, single sources last


def _gap_units(onto: Any, node_ids: List[str], mcp: bool, limit: int = MAX_GAPS, questions: Sequence[str] = (),
               tiers: Optional[Dict[str, int]] = None) -> List[render.Unit]:
    """The open gaps of the nodes, most severe first (a node's own order, then the node order, breaks ties), and
    one "+N more" unit for the rest. ``questions`` adds the ``open_question`` gaps (only those) of more nodes, after
    ``node_ids`` in the node order. ``tiers`` ranks gap types before severity (tier 1 when not listed).

    The "+N more" unit names a ``gaps --node`` call per node it leaves out (at most ``render.MAX_CALLS``, then
    ``gaps`` for the rest), and carries the same as ``capped`` entries for the result's ``omitted`` list
    (``_add_capped``), so a cap is reported even when the budget has none."""
    ranked = []
    every = len(node_ids) > 20
    listed = list(node_ids) + [q for q in questions if q not in node_ids]
    for order, nid in enumerate(listed):
        need = _needs(onto, nid, every)
        for rank, g in enumerate((need or {}).get("gaps") or []):
            if order >= len(node_ids) and g.get("type") != "open_question":
                continue
            tier = tiers.get(str(g.get("type")), 1) if tiers is not None else 0
            ranked.append((tier, -int(g.get("severity") or 0), rank, order, nid, g))
    ranked.sort(key=lambda r: r[:4] + (r[4],))
    units = []
    for _tier, _sev, _rank, _order, nid, g in ranked[:limit]:
        line = "open: %s" % _gap_text(onto, nid, g)
        item = dict(g, node=nid)
        item.update(render.flags(onto.record(nid)))  # untrusted and draft, as the gaps command's items carry them
        units.append(render.Unit("gaps", [line], item, "gaps", render.call(mcp, "gaps", node=nid)))
    rest = ranked[limit:]
    if rest:
        counts: "OrderedDict[str, int]" = OrderedDict()
        for r in rest:
            counts[r[4]] = counts.get(r[4], 0) + 1
        groups = [(marked(onto, nid), n, render.call(mcp, "gaps", node=nid))
                  for nid, n in list(counts.items())[:render.MAX_CALLS]]
        others = list(counts.items())[render.MAX_CALLS:]
        if others:
            groups.append(("%d more node%s" % (len(others), "" if len(others) == 1 else "s"),
                           sum(n for _nid, n in others), render.call(mcp, "gaps")))
        capped = [{"what": "gaps of %s" % of, "count": n, "call": call} for of, n, call in groups]
        text = "; ".join("%d gap%s of %s (%s)" % (n, "" if n == 1 else "s", of, call) for of, n, call in groups)
        follow = capped[0]["call"] if len(capped) == 1 else render.call(mcp, "gaps")
        item = {"more": len(rest), "nodes": list(counts)[:render.MAX_CALLS], "nodes_total": len(counts)}
        unit = render.Unit("gaps", ["open: +%d more: %s" % (len(rest), text)], item, "gaps", follow, count=len(rest))
        unit.capped = capped  # type: ignore[attr-defined]
        units.append(unit)
    return units


def _add_capped(selected: Dict[str, Any]) -> None:
    """Add to ``selected["omitted"]`` the gaps a chosen "+N more" unit left out (``_gap_units``): the budget
    engine reports only the units it did not take."""
    for unit in selected["chosen"]:
        selected["omitted"].extend(getattr(unit, "capped", None) or [])


def _warning(onto: Any, ids: Sequence[str]) -> Optional[str]:
    """One line: stale sources cited by the scope, and load problems of the data."""
    parts = []
    stale = set()
    for nid in ids:
        rec = onto.node(nid) or {}
        for src in _sources_of(rec):
            entry = onto.sources.get(src)
            if entry and sources_mod.stale(entry):
                stale.add(src)
    if stale:
        parts.append("stale source%s in scope: %s" % ("" if len(stale) == 1 else "s", ", ".join(sorted(stale))))
    if onto.problems:
        parts.append("the data has %d load problem%s (run onto validate)" % (
            len(onto.problems), "" if len(onto.problems) == 1 else "s"))
    return "warning: %s" % "; ".join(parts) if parts else None


def _sections(chosen: List[render.Unit]) -> "OrderedDict[str, List[Any]]":
    out: "OrderedDict[str, List[Any]]" = OrderedDict()
    for unit in chosen:
        out.setdefault(unit.section, []).append(unit.item)
    return out


def _cap_calls(omitted: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """``omitted`` with each group's ``calls`` cut to its call and ``render.MAX_SIMILAR_LISTED`` more, as the text
    footer names them (``similar`` still counts them all)."""
    out = []
    for g in omitted:
        g = dict(g)
        if isinstance(g.get("calls"), list):
            g["calls"] = g["calls"][:1 + render.MAX_SIMILAR_LISTED]
        out.append(g)
    return out


def _select_ranked(head: List[str], ranked: List[render.Unit], display: List[render.Unit], tail: List[str],
                   budget: int, everything: str, mcp: bool, prefix: bool = True) -> Dict[str, Any]:
    """``render.select`` over ``ranked``, the units taken printed in ``display`` order (which may differ from the
    rank; the footer names the rest in rank order). With ``prefix`` every unit counts as essential, so the first
    unit that does not fit stops the rest: what fits in a budget then fits in any larger one, and a larger budget
    never takes out what a smaller one showed (a section never shrinks as the budget grows)."""
    if prefix:
        for unit in ranked:
            unit.essential = True
    selected = render.select(head, ranked, tail, budget, everything, mcp)
    taken = {id(u) for u in selected["chosen"]}
    shown = [u for u in display if id(u) in taken]
    body = sum(len(u.lines) for u in selected["chosen"])
    lines = selected["lines"]
    foot = lines[len(head) + body: len(lines) - len(tail)]
    selected["lines"] = list(head) + [line for u in shown for line in u.lines] + foot + list(tail)
    selected["chosen"] = shown
    selected["omitted"] = _cap_calls(selected["omitted"])
    return selected


def _finish(result: Answer, selected: Dict[str, Any], version_text: str) -> Answer:
    lines = selected["lines"]
    result.lines = lines[1:] if version_text else list(lines)
    if isinstance(result.get("budget"), dict):  # the budget measures the text form; a JSON reply is longer
        result["budget"] = dict(result["budget"], measures="text")
    return result


# brief -----------------------------------------------------------------------------------------------------------
def brief(onto: Any, subject: str, budget: int = DEFAULT_BUDGET, drafts: bool = True, mcp: bool = False,
          version_text: str = "", use_card: bool = True) -> Answer:
    """The brief for ``subject`` (an id or words) in ``budget`` tokens (0 = no cap). ``version_text`` is the version
    line the output starts with: it is counted against the budget and left out of ``lines``. ``use_card`` False
    keeps the full form even when not one primary summary fits."""
    budget = max(0, int(budget or 0))
    scope = resolve_scope(onto, subject, drafts)
    hidden: Set[str] = set(scope.pop("hidden"))
    primary = list(scope["primary"])
    every_args: Dict[str, Any] = {"subject": subject, "budget": 0}
    if not drafts:
        every_args["drafts"] = False
    everything = render.call(mcp, "brief", **every_args)
    warning = _warning(onto, primary)
    head = ([version_text] if version_text else []) + ([warning] if warning else [])
    head += _scope_lines(onto, scope, "scope", mcp)
    primary_units: List[render.Unit] = [_primary_unit(onto, pid, mcp, drafts, hidden) for pid in primary]
    named: Set[str] = set()
    for unit in primary_units:
        named.update(getattr(unit, "named", ()))
    reached: Set[str] = set()
    related = _related(onto, primary, drafts, hidden, sorted(named), reached) if primary else []
    related_units = _related_units(onto, related, primary, mcp)
    bridge_units = _bridge_units(onto, primary, reached, drafts, hidden, mcp)
    quote_units = _quote_units(onto, primary, mcp)
    scope_ids: List[str] = []
    for pid in primary:
        for m in queries.class_members(onto, pid, drafts):
            if m not in scope_ids:
                scope_ids.append(m)
    scope_ids += [i for i in archived_ids(scope) if i not in scope_ids]  # the decision that retired one holds
    decision_units = _decision_units(onto, scope_ids, mcp)  # an empty scope reads no decisions
    gap_units = _gap_units(onto, primary, mcp)
    # printed in the usual order; taken by rank: the decisions and each node's first quote, then the open gaps,
    # then the related nodes one hop out, the bridges, the second hop and the other quotes
    units = primary_units + related_units + bridge_units + quote_units + decision_units + gap_units
    first_quotes = [u for u in quote_units if getattr(u, "first", False)]
    ranked = (primary_units + decision_units + first_quotes + gap_units
              + [u for u in related_units if u.item["depth"] == 1] + bridge_units
              + [u for u in related_units if u.item["depth"] != 1]
              + [u for u in quote_units if not getattr(u, "first", False)])
    tail = []
    if scope_ids and not decision_units:
        tail.append("decisions: none active for this scope")
    if hidden:
        tail.append("drafts hidden: %d (drafts=false)" % len(hidden))
    selected = _select_ranked(head, ranked, units, tail, budget, everything, mcp)
    _add_capped(selected)
    chosen = selected["chosen"]
    if (use_card and budget and primary and not any(u.section == "primary" for u in chosen)
            and (drafts or not _drafts_around(onto, primary[0]))):  # a card shows drafts
        carded = _card_brief(onto, subject, scope, primary[0], decision_units, tail, budget, mcp, version_text,
                             warning, everything, len(units))
        if carded is not None:
            carded["drafts"] = bool(drafts)
            carded["drafts_hidden"] = len(hidden)
            return carded
    result = Answer(OrderedDict([
        ("subject", subject), ("form", "brief"), ("scope", json_scope(scope, mcp)), ("sections", _sections(chosen)),
        ("omitted", selected["omitted"]), ("budget", selected["budget"]), ("warning", warning),
        ("drafts", bool(drafts)), ("drafts_hidden", len(hidden)),
    ]))
    return _finish(result, selected, version_text)


def _card_brief(onto: Any, subject: str, scope: Dict[str, Any], node_id: str, decision_units: List[render.Unit],
                tail: List[str], budget: int, mcp: bool, version_text: str, warning: Optional[str],
                everything: str, units_total: int) -> Optional[Answer]:
    """The brief as the scope line, the first primary node's card and the active decisions, or None when it has no
    card or the scope line and the card do not fit ``budget``."""
    card, source = serve_card(onto, node_id, mcp)
    if card is None:
        return None
    head = ([version_text] if version_text else []) + ([warning] if warning else [])
    head += _scope_lines(onto, scope, "scope", mcp)  # every match stays named, as in the full brief
    head.append("its card: the full brief does not fit %d tokens (%s)" % (budget, everything))
    head += card_lines(card, mcp)
    selected = render.select(head, decision_units, list(tail), budget, everything, mcp)
    text_chars = selected["budget"]["chars"]
    if text_chars > budget * render.CHARS_PER_TOKEN:
        return None
    rest = {"what": "the full brief", "count": units_total, "call": everything}
    result = Answer(OrderedDict([
        ("subject", subject), ("form", "card"), ("scope", json_scope(scope, mcp)),
        ("card", dict(card, source=source)),
        ("sections", OrderedDict([("card", [card["id"]]), ("decisions", [u.item for u in selected["chosen"]])])),
        ("omitted", [rest] + _cap_calls(selected["omitted"])), ("budget", selected["budget"]), ("warning", warning),
    ]))
    return _finish(result, selected, version_text)


# context ---------------------------------------------------------------------------------------------------------
FALLBACK_TEMPLATE = {"title": "Brief", "sections": [
    {"title": "Subject", "kinds": []}, {"title": "Active decisions", "decisions": True},
    {"title": "Open points", "gaps": True}]}


def _bare(kind: str) -> str:
    return kind.split("/", 1)[-1]


def _section_kinds(template: Dict[str, Any]) -> Set[str]:
    out: Set[str] = set()
    for section in template.get("sections") or []:
        out.update(str(k) for k in section.get("kinds") or [])
    return out


def pick_template(onto: Any, deliverable: Optional[str], scope_ids: Sequence[str]) -> Tuple[str, Dict[str, Any]]:
    """``(name, template)``: the named deliverable template (by key or title), else the first whose section kinds
    overlap most with the kinds of ``scope_ids``, else ``brief``, else the first, else the built-in fallback, which
    is also what the name ``brief`` gives when no pack declares a ``brief`` template."""
    templates = onto.registry.deliverables()
    if deliverable:
        want = util.name_key(deliverable)
        for name, decl in templates.items():
            if util.name_key(name) == want or util.name_key(str(decl.get("title") or "")) == want:
                return name, decl
        if want == "brief":  # the built-in fallback answers to its own name when no pack declares one
            return "brief", dict(FALLBACK_TEMPLATE)
        known = sorted(templates) + ([] if "brief" in templates else ["brief (built in)"])
        raise UsageError("unknown deliverable template %r; known: %s" % (deliverable, ", ".join(known)))
    kinds = [_bare(onto.kind_of(i)) for i in scope_ids]
    best: Optional[Tuple[int, str]] = None
    for name, decl in templates.items():
        wanted = {_bare(k) for k in _section_kinds(decl)}
        overlap = sum(1 for k in kinds if k in wanted)
        if overlap and (best is None or overlap > best[0]):
            best = (overlap, name)
    if best is not None:
        return best[1], templates[best[1]]
    if "brief" in templates:
        return "brief", templates["brief"]
    if templates:
        name = next(iter(templates))
        return name, templates[name]
    return "brief", dict(FALLBACK_TEMPLATE)


def _nodes_of_kinds(onto: Any, kinds: Set[str]) -> List[str]:
    if not kinds:
        return []
    bare = {_bare(k) for k in kinds}
    out = []
    for nid in onto.nodes:
        if nid in onto.virtual or not onto.active(nid):
            continue
        kind = onto.kind_of(nid)
        if kind in kinds or _bare(kind) in bare:
            out.append(nid)
    return out


def _ranked(onto: Any, ids: List[str], near: Set[str]) -> List[str]:
    return sorted(ids, key=lambda i: (i not in near, onto.ns_of(i) != "self", i))


def _more_call(onto: Any, ids: List[str], mcp: bool) -> str:
    """A search that lists every id of a template section: each id's own prefix (``role:``), OR-ed, over the
    kinds of those ids. Every id matches its prefix, so the call returns them all (paged)."""
    prefixes = sorted({i.rsplit(":", 1)[0] + ":" for i in ids})
    kinds = sorted({onto.kind_of(i) for i in ids})
    return render.call(mcp, "search", text=" OR ".join(prefixes), kinds=kinds)


def _template_units(onto: Any, template: Dict[str, Any], near: Set[str], decisions: List[Dict[str, Any]],
                    active_total: int, gaps_text: str, everything: str,
                    mcp: bool) -> Tuple[List[render.Unit], List[Dict[str, Any]]]:
    """One essential ``## <Title>: ids`` unit per template section, and the sections as JSON. "none in the
    ontology" is printed only for what the ontology does not hold: a decisions heading with active decisions none
    of which touch the task reads "none for this scope (N active: <call>)", and the data heading leaves out
    interview sources (the user's own answers, not data)."""
    units: List[render.Unit] = []
    sections_out = []
    for section in template.get("sections") or []:
        title = str(section.get("title") or "")
        kinds = {str(k) for k in section.get("kinds") or []}
        ids = _ranked(onto, _nodes_of_kinds(onto, kinds), near)
        entry: Dict[str, Any] = OrderedDict([("title", title), ("kinds", sorted(kinds)), ("ids", ids[:JSON_IDS]),
                                             ("total", len(ids))])
        if len(ids) > JSON_IDS:
            entry["more_call"] = _more_call(onto, ids, mcp)
        parts = []
        if ids:
            names: List[str] = []
            for i in ids[:SECTION_IDS]:
                if names and len(", ".join(names + [marked(onto, i)])) > HEADING_ID_CHARS:
                    break
                names.append(marked(onto, i))
            rest = len(ids) - len(names)
            parts.append(", ".join(names) + ("; +%d more (%s)" % (rest, _more_call(onto, ids, mcp)) if rest > 0
                                             else ""))
        if section.get("sources"):
            cited = [s for s in needs_mod.cited_sources(onto) if not interview_source(onto, s)]
            entry["sources"] = cited
            if cited:
                parts.append("sources %s%s" % (", ".join(cited[:5]), " +%d more" % (len(cited) - 5)
                                               if len(cited) > 5 else ""))
        if section.get("decisions"):
            entry["decisions"] = [d["id"] for d in decisions]
            entry["active_total"] = active_total
            if decisions:
                parts.append("decisions %s" % ", ".join(d["id"] for d in decisions))
            elif active_total:
                parts.append("none for this scope (%d active: %s)" % (active_total, render.call(mcp, "decisions")))
        if section.get("gaps"):
            entry["gaps"] = True
            parts.append(gaps_text)
        sections_out.append(entry)
        text = "## %s: %s" % (title, "; ".join(parts) if parts else "none in the ontology")
        units.append(render.Unit("template", [text], entry, "template headings", everything, essential=True))
    return units, sections_out


def _open_questions(onto: Any, near: Set[str], task: str, about: Sequence[Set[str]] = ()) -> List[str]:
    """The local active question nodes near a task, in this order (then by id): in or linked to its scope
    (``near``); linked to a record the context lists, each set of ``about`` in turn (its goals, constraints and
    linked records, then its template ids), so a question about a listed constraint is never left out; then the
    ones matching a word of the task."""
    questions = [n for n in onto.local_nodes(active_only=True) if _bare(onto.kind_of(n)) == "question"]
    if not questions:
        return []
    words = task_words(task, TASK_WORDS)
    matched: Set[str] = set()
    if words:
        matched = {r["id"] for r in queries.search(onto, " OR ".join(words), suggest=False)["results"]}

    def rank(q: str) -> Optional[int]:
        if q in near:
            return 0
        others = {e["other"] for e in onto.edges_of(q) if not e["edge"].get("background")}
        for i, ids in enumerate(about, start=1):
            if others & ids:
                return i
        return len(about) + 1 if q in matched else None

    ranked = [(rank(q), q) for q in questions]
    return [q for r, q in sorted((r, q) for r, q in ranked if r is not None)]


MAX_GOAL_METRICS = 4  # metrics named on a context's goal line (the rest: "+N more" and the goal's get call)
GOAL_ATTR = 120  # characters of each goal attribute (horizon, success) a context prints


def _goal_unit(onto: Any, gid: str, mcp: bool) -> render.Unit:
    """A context's goal line, essential: ``goal <id>: <summary> | <attrs> | measured_by <metric> (target <t>)``.
    The goal's key attributes (its horizon and success, ``GOAL_ATTR`` characters each) and the metrics that measure
    it travel with it, so a context for a deliverable that serves the goal carries what it is measured against."""
    rec = onto.node(gid) or {}
    follow = render.call(mcp, "get", id=gid)
    parts = ["goal %s: %s" % (marked(onto, gid, rec), queries.plain(rec.get("summary") or rec.get("name"), SUMMARY))]
    attrs = _key_attrs(rec, 3, GOAL_ATTR)
    if attrs:
        parts.append("; ".join(attrs))
    metrics = [row for row in _relation_counts(onto, gid).get("measured_by", []) if row["id"] not in onto.virtual]
    if metrics:
        shown = []
        for row in metrics[:MAX_GOAL_METRICS]:
            target = queries.as_dict((onto.node(row["id"]) or {}).get("attrs")).get("target")
            shown.append(_rel_text(onto, row) + (" (target %s)" % queries.plain(render.fmt(target), 60)
                                                 if target not in (None, "", [], {}) else ""))
        rest = len(metrics) - len(shown)
        parts.append("measured_by %s%s" % (", ".join(shown), " +%d more (%s)" % (rest, follow) if rest > 0 else ""))
    item: Dict[str, Any] = OrderedDict([("id", gid), ("summary", rec.get("summary"))])
    if queries.as_dict(rec.get("attrs")):
        item["attrs"] = queries.as_dict(rec.get("attrs"))
    if metrics:
        item["measured_by"] = [row["id"] for row in metrics]
    item.update(render.flags(rec))
    return render.Unit("goals", [" | ".join(parts)], item, "goals", follow, essential=True)


def _linked_units(onto: Any, sid: str, skip: Set[str], mcp: bool) -> List[render.Unit]:
    """The records one hop from a context subject (over active non-background links, one row per label and id), each
    with its summary and up to two key attributes: ``linked <subject> <label> <id>: <summary> | <attrs>``. The ids
    in ``skip`` (the subjects and their classes, the goals and the constraints, which have lines of their own) are
    left out; after ``MAX_LINKED`` rows one line names how many more and the call that lists them."""
    rows = [(label, row) for label, items in _relation_counts(onto, sid).items() for row in items
            if row["id"] not in skip and row["id"] not in onto.virtual]
    units = []
    for label, row in rows[:MAX_LINKED]:
        rec = onto.node(row["id"]) or {}
        text = queries.plain(rec.get("summary") or rec.get("name"), SUMMARY) or "(no summary yet)"
        if render.flags(rec).get("untrusted"):  # the record's own text keeps its marker
            text = "[untrusted] " + text
        attrs = _key_attrs(rec, 2)
        line = "linked %s %s %s: %s%s" % (shown_id(onto, sid), label, _rel_text(onto, row), text,
                                          " | %s" % "; ".join(attrs) if attrs else "")
        item: Dict[str, Any] = OrderedDict([("id", row["id"]), ("from", sid), ("label", label),
                                            ("summary", rec.get("summary") or "")])
        item.update(render.flags(rec))
        units.append(render.Unit("linked", [line], item, "linked records", render.call(mcp, "get", id=row["id"])))
    rest = rows[MAX_LINKED:]
    if rest:
        call = render.call(mcp, "get", id=sid)
        units.append(render.Unit("linked", ["linked %s: +%d more (%s)" % (shown_id(onto, sid), len(rest), call)],
                                 {"from": sid, "more": len(rest), "ids": [r["id"] for _label, r in rest]},
                                 "linked records", call, count=len(rest)))
    return units


def context(onto: Any, task: str, deliverable: Optional[str] = None, budget: int = CONTEXT_BUDGET, mcp: bool = False,
            version_text: str = "") -> Answer:
    """What to know before writing ``task``: the template headings with their ids, goals, constraints, active
    decisions, the subject nodes and the open gaps, in ``budget`` tokens (0 = no cap).

    The decisions are the active ones whose scope touches the topic, the task's scope (primary and more, with their
    ``same_as`` classes), any id a template heading, goal, constraint, open question or linked record lists, or that
    were recorded with no scope. Each subject is followed by the records one hop from it (``_linked_units``). The
    open points take the gaps of the subjects and of the local nodes of the template's kinds, plus the
    ``open_question`` gaps of the question nodes near the task (``_open_questions``); open questions rank first and
    ``single_source`` last (``CONTEXT_TIERS``)."""
    budget = max(0, int(budget or 0))
    scope = resolve_scope(onto, task, True, skip_words=TASK_WORDS)
    scope.pop("hidden", None)
    subjects = list(scope["primary"])
    more = list(scope["more"])
    name, template = pick_template(onto, deliverable, subjects + more[:10])
    near: Set[str] = set()
    for sid in subjects + more:
        near.update(onto.members(sid))
        near.update(e["other"] for e in onto.edges_of(sid))
    template_kinds = _section_kinds(template)
    template_ids = _nodes_of_kinds(onto, template_kinds)
    goals = _ranked(onto, _nodes_of_kinds(onto, {"goal"}), near)
    constraints = _ranked(onto, _nodes_of_kinds(onto, {"constraint"}), near)
    linked_ids = [row["id"] for sid in subjects for rows in _relation_counts(onto, sid).values() for row in rows
                  if row["id"] not in onto.virtual]  # the records printed under each subject
    questions = _open_questions(onto, near, task, [set(goals) | set(constraints) | set(linked_ids),
                                                   set(template_ids)])
    listed: "OrderedDict[str, bool]" = OrderedDict()
    metric_ids = [row["id"] for gid in goals for row in _relation_counts(onto, gid).get("measured_by", [])
                  if row["id"] not in onto.virtual]  # named on the goal lines
    for nid in subjects + more + template_ids + goals + metric_ids + constraints + questions + linked_ids:
        for m in onto.members(nid):
            listed[m] = True
    for nid in archived_ids(scope):  # a subject retired under a decision: that decision holds for the task
        listed[nid] = True
    topic_scope = ["topic:%s" % onto.ns, "%s/" % onto.ns] if onto.ns else []
    decisions = queries.decisions_for(onto, topic_scope + list(listed), unscoped=True)
    active_total = len(decisions) if decisions else len(queries.active_decisions(onto))
    every_args: Dict[str, Any] = {"task": task, "deliverable": deliverable or name, "budget": 0}
    everything = render.call(mcp, "context", **every_args)
    warning = _warning(onto, subjects)
    head = ([version_text] if version_text else []) + ([warning] if warning else [])
    line = _scope_lines(onto, scope, "context", mcp)[0]
    head.append("%s | template %s (%s)" % (line, name, template.get("title") or name))
    goal_units = [_goal_unit(onto, gid, mcp) for gid in goals]
    constraint_units: List[render.Unit] = []
    for cid in constraints:
        rec = onto.node(cid) or {}
        text = "constraint %s: %s" % (marked(onto, cid, rec), queries.plain(rec.get("summary") or rec.get("name"),
                                                                            SUMMARY))
        unit = render.Unit("constraints", [text], {"id": cid, "summary": rec.get("summary")}, "constraints",
                           render.call(mcp, "get", id=cid))
        unit.near = cid in near  # type: ignore[attr-defined]
        constraint_units.append(unit)
    decision_units = [render.Unit("decisions", [decision_text(d)], d, "decisions",
                                  render.call(mcp, "decisions", text=d["id"]), essential=True) for d in decisions]
    subject_units: List[render.Unit] = []
    linked_units: List[render.Unit] = []
    subject_rows: List[render.Unit] = []  # each subject, then its linked records, as printed
    skip = set(goals) | set(constraints)
    for sid in subjects:
        skip.update(onto.members(sid))
    for sid in subjects:
        unit = _primary_unit(onto, sid, mcp)
        unit.section, unit.group, unit.essential = "subjects", "subject summaries", False
        subject_units.append(unit)
        linked = _linked_units(onto, sid, skip, mcp)
        linked_units += linked
        subject_rows += [unit] + linked
    gap_nodes = _ranked(onto, [i for i in template_ids if onto.ns_of(i) == "self"], near)
    gap_units = _gap_units(onto, subjects + [i for i in gap_nodes if i not in subjects], mcp, questions=questions,
                           tiers=CONTEXT_TIERS)
    display = goal_units + constraint_units + decision_units + subject_rows + gap_units
    # taken by rank: the decisions and goals (essential, so a lower budget, such as the one a JSON result is
    # refilled at, never drops them for later lines), the subjects, the constraints near the task, the records
    # linked to each subject, the other constraints, then the open points; printed in the usual order
    ranked = (decision_units + goal_units + subject_units + [u for u in constraint_units if getattr(u, "near", False)]
              + linked_units + [u for u in constraint_units if not getattr(u, "near", False)] + gap_units)
    # The gaps heading says "below" only when a gap line is shown. When none fits, it counts them and names the
    # call that shows them all, which stays true whatever the second fill takes. With no gaps it says so.
    gaps_text = "open gaps below" if gap_units else "none in the ontology"
    headings, sections_out = _template_units(onto, template, near, decisions, active_total, gaps_text, everything,
                                             mcp)
    selected = _select_ranked(head, headings + ranked, headings + display, [], budget, everything, mcp, prefix=False)
    if gap_units and not any(u.section == "gaps" for u in selected["chosen"]):
        total = sum(u.count for u in gap_units)
        gaps_text = "%d open gap%s (%s)" % (total, "" if total == 1 else "s", everything)
        headings, sections_out = _template_units(onto, template, near, decisions, active_total, gaps_text,
                                                 everything, mcp)
        selected = _select_ranked(head, headings + ranked, headings + display, [], budget, everything, mcp,
                                  prefix=False)
    _add_capped(selected)
    # the template headings are sent once, as ``template.sections``, never again as ``sections.template``; only the
    # headings the budget took are sent (the others are named in ``omitted``, as the text names them)
    sent = [u.item for u in selected["chosen"] if u.section == "template"]
    template_out = OrderedDict([("name", name), ("title", template.get("title") or name), ("sections", sent)])
    if len(sent) < len(sections_out):
        template_out["sections_total"] = len(sections_out)
    sections = _sections([u for u in selected["chosen"] if u.section != "template"])
    result = Answer(OrderedDict([
        ("task", task), ("scope", json_scope(scope, mcp)), ("template", template_out),
        ("sections", sections), ("omitted", selected["omitted"]),
        ("budget", selected["budget"]), ("warning", warning),
    ]))
    return _finish(result, selected, version_text)


# cards -----------------------------------------------------------------------------------------------------------
def has_card(onto: Any, node_id: str) -> bool:
    """True for an active shared node whose kind's pack sets ``card: true``."""
    rec = onto.node(node_id)
    if rec is None or node_id in onto.virtual or rec.get("status") == "archived":
        return False
    if (rec.get("visibility") or "shared") != "shared":
        return False
    return onto.registry.has_card(onto.kind_of(node_id))


def _follow(node_id: str) -> List[Tuple[str, Dict[str, Any]]]:
    return [("get", {"id": node_id, "full": True}), ("neighbors", {"id": node_id, "depth": 2})]


def follow_text(node_id: str, mcp: bool) -> str:
    return render.follow_line(_follow(node_id), mcp)


def build_card(onto: Any, node_id: str) -> Dict[str, Any]:
    """The card of one node (C.18); ``follow`` is in MCP words, and the body leaves room for either form."""
    rec = onto.node(node_id) or {}
    kind = onto.kind_of(node_id)
    name = queries.plain(rec.get("name")) or node_id
    lines: List[Any] = [render.Line("%s  %s (%s, %s)" % (render.mark(rec, node_id), render.trunc(name, 120),
                                                         queries.plain(rec.get("status")),
                                                         queries.plain(rec.get("trust"))))]
    lines.append(render.Line(summary_text(rec, CARD_SUMMARY)))
    # the attrs and needs lines hold the record's own text (the asks quote its name): an untrusted record's are
    # marked as its head and summary lines are, and as get, brief and context mark them
    told = "[untrusted] " if render.flags(rec).get("untrusted") else ""
    attrs = ["%s %s" % (queries.plain(k, 60), queries.plain(render.fmt(v), 60))
             for k, v in sorted(queries.as_dict(rec.get("attrs")).items()) if v not in (None, "", [], {})]
    if attrs:
        lines.append(render.Line(told + "attrs: ", attrs, rank=3))
    # a card is committed in build/cards.json, so it never names a local-visibility node (only counts them)
    members = [m for m in onto.members(node_id) if m != node_id]
    shown_members = [m for m in members if not _local_only(onto, m)]
    if members:
        extra = len(members) - len(shown_members)
        lines.append(render.Line("same as: ", shown_members + (["+%d local" % extra] if extra else []), rank=1))
    for label, rows in _relation_counts(onto, node_id).items():
        shown = [queries.rel_mark(r) for r in rows if not _local_only(onto, r["id"])]
        extra = len(rows) - len(shown)
        lines.append(render.Line("%s: " % label, shown + (["+%d local" % extra] if extra else []), rank=2))
    srcs = _sources_of(rec)
    if srcs:
        lines.append(render.Line("sources: ", ["%s (%d quote%s)" % (s, q, "" if q == 1 else "s") if q else s
                                               for s, q in srcs.items()], rank=3))
    need = _needs(onto, node_id)
    gaps = (need or {}).get("gaps") or []
    if need is None:
        lines.append(render.Line("needs: unknown, as %s" % queries.MALFORMED.split(", so ")[0]))
    else:
        lines.append(render.Line(told + "needs: ", [_gap_text(onto, node_id, g, with_id=False, width=80)
                                                    for g in gaps], rank=2))
    follow = follow_text(node_id, True)
    room = CARD_CHARS - max(len(follow), len(follow_text(node_id, False))) - 1
    body = render.fit_card(lines, room)
    title = node_id if rec.get("trust") == "untrusted" else render.trunc(name, 200)
    return OrderedDict([("id", node_id), ("ns", onto.ns_of(node_id)), ("kind", kind), ("title", title),
                        ("chars", len(body) + 1 + len(follow)), ("body", body), ("follow", follow)])


def _local_only(onto: Any, nid: str) -> bool:
    """A local node kept out of exports (visibility ``local``, such as a person)."""
    rec = onto.node(nid) or {}
    return onto.ns_of(nid) == "self" and rec.get("visibility") == "local"


def build_cards(onto: Any) -> Dict[str, Any]:
    """Every card of the local topic, as ``build/cards.json`` holds them (C.18), sorted by id."""
    digest = store.data_hash(onto.repo) if onto.repo is not None else util.sha256_hex(b"")
    _all_needs(onto)  # one pass for every card
    cards = [build_card(onto, nid) for nid in sorted(onto.local_nodes(active_only=True)) if has_card(onto, nid)]
    return OrderedDict([("meta", OrderedDict([("data_hash", digest), ("kit", __version__)])), ("cards", cards)])


def stored_cards(onto: Any) -> Tuple[Optional[Dict[str, Dict[str, Any]]], str]:
    """``(cards by id, "")`` when ``build/cards.json`` matches the loaded data and this kit, else ``(None, why)``."""
    if onto.repo is None:
        return None, "no repo to read %s from" % CARDS_FILE
    path = onto.repo.path(CARDS_FILE)
    try:
        st = os.stat(path)
    except OSError:
        return None, "%s is missing" % CARDS_FILE
    key = (path, st.st_mtime_ns, st.st_size, store.data_hash(onto.repo))
    cached = onto._cache.get("answers.cards")
    if cached and cached[0] == key:
        return cached[1]
    result: Tuple[Optional[Dict[str, Dict[str, Any]]], str]
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        result = (None, "%s cannot be read (%s)" % (CARDS_FILE, render.trunc(str(exc), 60)))
    else:
        meta = doc.get("meta") if isinstance(doc, dict) else None
        if not isinstance(meta, dict):
            result = (None, "%s has no meta" % CARDS_FILE)
        elif meta.get("kit") != __version__:
            result = (None, "%s was built by kit %s, this is %s" % (CARDS_FILE, meta.get("kit"), __version__))
        elif meta.get("data_hash") != key[3]:
            result = (None, "%s was built from other data" % CARDS_FILE)
        else:
            result = (OrderedDict((c["id"], c) for c in doc.get("cards") or []
                                  if isinstance(c, dict) and isinstance(c.get("id"), str)), "")
    onto._cache["answers.cards"] = (key, result)
    return result


def serve_card(onto: Any, node_id: str, mcp: bool = True) -> Tuple[Optional[Dict[str, Any]], str]:
    """``(card, source)``: the stored card when ``build/cards.json`` is current, else one built now (``source``
    ``built from the loaded data: <why>``); ``(None, why)`` for a node without a card. ``follow`` is in the caller's
    words."""
    if not has_card(onto, node_id):
        return None, "no card"
    cards, why = stored_cards(onto)
    if cards is not None and node_id in cards:
        card, source = dict(cards[node_id]), CARDS_FILE
    else:
        built = onto._cache.setdefault("answers.built", {})
        if node_id not in built:
            built[node_id] = build_card(onto, node_id)
        card = dict(built[node_id])
        if cards is not None:
            why = ("imported nodes are not in %s" % CARDS_FILE if onto.ns_of(node_id) != "self"
                   else "%s has no card for it" % CARDS_FILE)
        source = "built from the loaded data: %s" % why
    card["follow"] = follow_text(node_id, mcp)
    return card, source


def card_for(onto: Any, id: str, mcp: bool = True) -> Optional[Dict[str, Any]]:
    """The card of a node id (``follow`` in the caller's words), or None when the node has no card."""
    return serve_card(onto, id, mcp)[0]


def card_lines(card: Dict[str, Any], mcp: bool) -> List[str]:
    """A card as served: its body (each line with control characters removed, as a stored card may come from
    another kit), then the follow-up line in the caller's words."""
    body = queries.plain_block(card.get("body")).split("\n")
    return body + [follow_text(str(card.get("id")), mcp)]


# handlers --------------------------------------------------------------------------------------------------------
def _budget(ctx: Any, args: Dict[str, Any], default: int) -> int:
    value = args.get("budget")
    budget = default if value is None else int(value)
    if budget < 0:
        raise UsageError("budget must be 0 (no cap) or more tokens")
    if budget == 0 and getattr(ctx, "mcp", False):
        budget = MCP_BUDGET
    return budget


def _version_text(ctx: Any) -> str:
    try:
        return render.version_line(ctx.stamp())
    except Exception:  # the version line never stops an answer
        return ""


def cmd_brief(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    return brief(ctx.onto(), args.get("subject") or "", _budget(ctx, args, DEFAULT_BUDGET),
                 drafts=args.get("drafts") is not False, mcp=bool(ctx.mcp), version_text=_version_text(ctx))


def cmd_context(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    return context(ctx.onto(), args.get("task") or "", args.get("deliverable"), _budget(ctx, args, CONTEXT_BUDGET),
                   mcp=bool(ctx.mcp), version_text=_version_text(ctx))


_CARD_URI = re.compile(r"^onto://([a-z][a-z0-9-]{0,31})/card/(.+)$")


def cmd_card(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    onto = ctx.onto()
    text = str(args.get("id") or "").strip()
    m = _CARD_URI.match(text)
    if m:
        text = m.group(2) if m.group(1) in ("self", onto.ns) else "%s/%s" % (m.group(1), m.group(2))
    note: Dict[str, Any] = {}
    node_id = queries.resolve_or_raise(onto, text, res=note, mcp=bool(getattr(ctx, "mcp", False)))
    card, source = serve_card(onto, node_id, bool(ctx.mcp))
    if card is None:
        kinds = [k for k in onto.registry.kinds() if onto.registry.has_card(k)]
        raise NotFound("no card for %s: cards cover active shared nodes of the kinds %s; %s briefs it" % (
            node_id, ", ".join(kinds) or "none", render.call(bool(ctx.mcp), "brief", subject=node_id)),
            [], searched=text)
    out: Dict[str, Any] = OrderedDict([("card", card), ("source", source)])
    out.update(note)
    return out


# renderers -------------------------------------------------------------------------------------------------------
def _lines_of(result: Dict[str, Any], rebuild: Any) -> List[str]:
    lines = getattr(result, "lines", None)
    if lines is None:
        lines = rebuild().lines
    return list(lines)


def render_brief(result: Dict[str, Any], mode: str, ctx: Any) -> List[str]:
    budget = int((result.get("budget") or {}).get("tokens") or 0)
    return _lines_of(result, lambda: brief(ctx.onto(), result.get("subject") or "", budget,
                                           result.get("drafts", True) is not False, bool(ctx.mcp),
                                           _version_text(ctx)))


def render_context(result: Dict[str, Any], mode: str, ctx: Any) -> List[str]:
    budget = int((result.get("budget") or {}).get("tokens") or 0)
    template = (result.get("template") or {}).get("name")
    return _lines_of(result, lambda: context(ctx.onto(), result.get("task") or "", template, budget,
                                             bool(ctx.mcp), _version_text(ctx)))


def render_card(result: Dict[str, Any], mode: str, ctx: Any) -> List[str]:
    lines = queries.resolved_lines(result)
    card = result.get("card") or {}
    lines += card_lines(card, bool(getattr(ctx, "mcp", False)))
    source = result.get("source")
    if source and source != CARDS_FILE:
        lines.append("(this card was %s)" % source)
    return lines


__all__ = ["brief", "context", "card_for", "build_cards", "build_card", "serve_card", "has_card", "resolve_scope",
           "pick_template", "shown_id", "json_scope", "interview_source", "task_words", "cmd_brief", "cmd_context",
           "cmd_card", "render_brief", "render_context", "render_card", "Answer"]
