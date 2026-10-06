"""Build outputs: the resolved export (``build/export.json``), the cards file (``build/cards.json``) and the
self-contained viewer (``build/index.html``).

``export(repo, version="unreleased")`` returns the resolved export (C.17): the topic's own shared nodes and the
edges between them and the imports (an edge touching a ``local`` node is left out), the sources those records cite
(erased sources are left out), the loaded packs with their sha256, the lock entries, counts, the richness badge and
every import flattened under ``bundled`` (from ``compose.bundle`` when that module is built). It carries no
timestamp other than the sources' ``captured_at``, so equal data gives equal bytes. ``version`` is ``unreleased``
unless ``release`` writes it.

``write(repo, out_dir=None, html=False)`` refuses while ``validate`` reports problems, then writes the export, the
cards file (from ``answers.build_cards`` when that module is built; cards of nodes left out of the export, or that
name one, are dropped) and, with ``html``, the viewer. Every file is written atomically under the write lock, and
all of them or none: a failure puts the old bytes back. ``check`` builds twice in memory and compares the bytes.

The viewer is ``viewer/template.html`` with three sentinels, each exactly once: ``/*@CSS@*/``, ``/*@JS@*/`` and
``@DATA@`` inside ``<script id="data" type="application/json">``. The data goes in last, with ``</`` escaped as
``<\\/`` (and ``<!--`` as its ``\\u003c`` form), so no text in the data can close the script or be read as a
sentinel. The page warns past 6 MB and is refused past 10 MB.

The viewer also carries, per exported node, the newest active decisions whose scope matches it (the same matching
as ``onto decisions --scope``), so the node's quick look and page can name them. Each one keeps only its date, its
question and the chosen answer, cut short.

Local records stay out of every published file: the export, the cards, the viewer and the release notes. A
left-out record is a local node whose visibility is not ``shared``. ``Redactor.scrub`` is the one path every
published text takes (``Redactor``): the prose of the shared records the export keeps, their names and aliases (id
forms only), source titles, cards (a card that names one is dropped), the viewer's decisions, needs and history
labels, and ``--notes``. It replaces the record's id in any case (bare, after ``@``, inside a path, under ``self/``
or the topic's own namespace, before a file extension, or with other text joined to it, as in
``person:rose-2026``, unless a record that is not left out has that longer id), the id joined to its kind (``person-rose``), a name or slug
after ``@`` in any case, and its names, aliases and id slugs of more than one word, which read ``[local record]``
(``a local record`` in a source title). A one-word name is matched only as written, so the same word in lower
case in ordinary prose stays. A name that a shared or imported record also has is in the export already and
stays, and a record's own name stays in its own text. An id under an import's namespace names that import's record
and stays. A decision whose every scope names a left-out record (or a place under one) is not shown; one whose
words named one gets a stand-in id that keeps its date and hash (``dec-YYYYMMDD-local-record-<hash>``), since its
id was made from those words; one whose words would still name one once redacted is not shown.
"""

from __future__ import annotations

import copy
import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import FORMAT, __version__, graph, history, ids, ledger, lockfile, needs, packs, records, render, store, util
from .mutate import ERASED_NAME
from . import validate as validate_mod
from .commands import Context, optional
from .errors import DataError, Refused, UsageError

KIT_DIR = os.path.dirname(os.path.abspath(__file__))
VIEWER_DIR = os.path.join(KIT_DIR, "viewer")
BUILD_DIR = "build"
EXPORT = "build/export.json"
CARDS = "build/cards.json"
VIEWER = "build/index.html"
FILE_NAMES = ("export.json", "cards.json", "index.html")

CSS_SENTINEL = "/*@CSS@*/"
JS_SENTINEL = "/*@JS@*/"
DATA_SENTINEL = "@DATA@"
DATA_TAG = '<script id="data" type="application/json">'
MAX_BYTES = 10 * 1024 * 1024
WARN_BYTES = 6 * 1024 * 1024
VERSION_RE = re.compile(r"^(?:unreleased|v[1-9][0-9]*)\Z")

STATUSES: Tuple[Dict[str, str], ...] = (
    {"id": "confirmed", "label": "Confirmed", "tone": "ok"},
    {"id": "proposed", "label": "Draft", "tone": "warn"},
    {"id": "archived", "label": "Archived", "tone": "neutral"},
)
EDGE_COLUMNS = ("src", "rel", "dst", "status", "background", "note", "untrusted")  # one viewer edge row
HISTORY_POINTS = 400  # newest points the viewer's sparkline keeps
NEEDS_PER_NODE = 3
NEED_TEXT = 200
DECISIONS_PER_NODE = 5
DECISION_TEXT = 300
LOCAL_MARK = "[local record]"  # what the viewer shows in place of a left-out record's id, name or alias
TITLE_MARK = "a local record"  # the same in a source title, which reads as prose
NAME_SHORTEST = 2  # a one-letter name is not looked for in free text: it would match ordinary words
STAND_IN_RE = re.compile(r"^dec-([0-9]{8})-.+-([0-9a-f]{4,8})\Z")
# what may stand between the words of a name in free text: spacing, "_", "-" and invisible characters, or nothing
NAME_SEP = r"[\s_\-\u00ad\u200b-\u200d\u2060\ufeff]*"
NAME_SEP_SPLIT_RE = re.compile(r"[\s_\-]+")
ONE_WORD_RE = re.compile(r"^[^\W\d_]+\Z")
# a run of words with "-", "_", "." or invisible characters (or nothing) between them: where a joined id may be
JOINED_RE = re.compile(r"[^\W_]+(?:[\-_.\u00ad\u200b-\u200d\u2060\ufeff]+[^\W_]+)*")
WORD_RE = re.compile(r"[^\W_]+")
HANDLE_RE = re.compile(r"@([^\W_]+)")  # "@" and one word, as a handle names someone
KIND_TAIL_RE = re.compile(r"(?:[a-z][a-z0-9-]{0,31}/)?[a-z][a-z0-9-]{0,31}\Z", re.I)  # the kind before a slug
KIND_HEAD_RE = re.compile(r"[a-z0-9-]{1,32}\Z", re.I)  # what may run on from a longer kind before a found id
ID_CHAR_RE = re.compile(r"[a-z0-9]", re.I)  # a character that goes on a word of an id (no boundary before it)
JOIN_CUT_RE = re.compile(r"[\-_.](?=[^\W_])")  # where a slug may end inside a joined run
PLACE = "\x01"  # stands for the mark while ``Redactor.scrub`` runs, so no pass reads the mark's own words
KEEP_OPEN, KEEP_STEP = "\x02", "\x03"  # a placeholder for a text a pass must leave as it is: no letter, no digit
KEEP_RE = re.compile("\x02(\x03+)\x02")
PLACE_RE = re.compile("[\x00-\x03]")  # control characters no text needs, dropped so no placeholder is forged


# the export ----------------------------------------------------------------------------------------------------
def _ns_of(endpoint: str) -> str:
    ns, _local = ids.split_ns(endpoint)
    return ns or "self"


def _is_shared(node: Dict[str, Any]) -> bool:
    return node.get("visibility") == "shared"


def exported_ids(onto: Any) -> Tuple[List[str], List[str]]:
    """(node ids, edge ids) of the export, sorted: local nodes whose visibility is ``shared``, and local edges
    (bridges included) with no end on a node left out."""
    node_ids = sorted(nid for nid in onto.local_ids if _is_shared(onto.nodes.get(nid) or {}))
    keep: Set[str] = set(node_ids)
    edge_ids = []
    for eid in onto.local_edge_ids:
        edge = onto.edges.get(eid) or {}
        ends = [edge.get("src"), edge.get("dst")]
        if all(isinstance(e, str) and _end_kept(onto, e, keep) for e in ends):
            edge_ids.append(eid)
    return node_ids, sorted(edge_ids)


def _end_kept(onto: Any, end: str, keep: Set[str]) -> bool:
    if ids.is_qualified(end):
        return True  # an imported node: shared upstream by definition
    if ids.SOURCE_RE.match(end):
        return True
    return end in keep


def _cited_sources(onto: Any, records_: Iterable[Dict[str, Any]], edges: Iterable[Dict[str, Any]]) -> List[str]:
    cited: Set[str] = set()
    for rec in list(records_) + list(edges):
        for p in rec.get("prov") or []:
            if isinstance(p, dict) and isinstance(p.get("src"), str) and ids.SOURCE_RE.match(p["src"]):
                cited.add(p["src"])
    for edge in edges:
        for end in (edge.get("src"), edge.get("dst")):
            if isinstance(end, str) and ids.SOURCE_RE.match(end):
                cited.add(end)
    return sorted(sid for sid in cited if sid in onto.sources and not (onto.sources[sid] or {}).get("erased"))


def _hidden_ids(onto: Any) -> Set[str]:
    """The local nodes an export leaves out (visibility not ``shared``, such as a person)."""
    return {nid for nid in onto.local_ids if not _is_shared(onto.nodes.get(nid) or {})}


def _scrub_title(title: Any, hidden: Optional[Set[str]] = None, names: Sequence[str] = (),
                 redactor: Optional["Redactor"] = None) -> Any:
    """A source title with every left-out id and name written as ``a local record`` (an interview answer's title
    names the node it was asked about). It goes through ``Redactor.scrub``, the one path every published text
    takes: ``redactor`` is the build's, or one made from ``hidden`` (the ids) and ``names`` (as ``Redactor.names``
    lists them)."""
    if not isinstance(title, str):
        return title
    if redactor is None:
        if not (hidden or names):
            return title
        redactor = Redactor(hidden=hidden or (), names=names)
    return redactor.scrub(title, TITLE_MARK)


def _name_parts(name: str) -> List[str]:
    return [w for w in NAME_SEP_SPLIT_RE.split(name) if w]


def _one_word(name: str) -> bool:
    """True for a name that is a single word of letters (``Rose``): it may be an ordinary word too, so free text
    matches it only as written, in its own case."""
    return len(_name_parts(name)) == 1 and ONE_WORD_RE.match(name) is not None


def _names_re(names: Sequence[str]) -> Optional["re.Pattern[str]"]:
    """One pattern for ``names``: each whole (not inside a longer word), the longest first. Between the words of a
    name any run of spacing, ``_``, ``-`` or invisible characters matches, or none (``Pat_Green``, ``PatGreen``).
    A name of several words matches in any case; a one-word name (``_one_word``) only in its own case, so a
    person called Rose leaves "rose bushes" alone."""
    words = sorted({" ".join(n.split()) for n in names if isinstance(n, str) and n.strip()},
                   key=lambda n: (-len(n), n))
    if not words:
        return None
    parts = []
    for n in words:
        body = NAME_SEP.join(re.escape(w) for w in _name_parts(n))
        parts.append(body if _one_word(n) else "(?i:%s)" % body)
    return re.compile(r"(?<![^\W_])(?:%s)(?![^\W_])" % "|".join(parts))


def _joined_key(nid: str) -> str:
    """The joined form of an id as one key: its kind and slug words with nothing between them, casefolded
    (``person:pat-green`` gives ``personpatgreen``)."""
    return "".join(w for w in WORD_RE.findall(nid)).casefold()


class Redactor(object):
    """The export's rules for local records, applied to every text a published file carries: the export's records
    and source titles, the cards, the viewer's decisions, needs and history labels, and a release's notes. A
    left-out record is a local node whose visibility is not ``shared`` (``_hidden_ids``). ``scrub`` is the one
    path: it writes a mark over the record's id (bare, ``self/`` or the topic's own namespace, after ``@``, after a
    path segment that is not an import's namespace, before a file extension, or with other text joined to it that
    no kept record's id spells: ``_id_sub``), in any case; over the id joined
    to its kind (``person-rose``, ``_joined_key``); over a one-word name or slug after ``@`` in any case; and
    over its name, its aliases and the slug part of its id when that has more than one word (``pat-green``). A
    name, alias or slug that a record the export keeps (or an import) also has is in the export already, so it
    stays; so do one-letter names and the name an erased node is left with. A one-word name is matched as written
    only (``_names_re``), so the same word in lower case in ordinary prose stays."""

    def __init__(self, onto: Any = None, *, hidden: Iterable[str] = (), names: Iterable[str] = ()) -> None:
        # Only the cheap sets are built here. The name keys and the id pattern (one alternation over every
        # left-out id) are built by ``_prepare`` on the first text that needs them, so a build that redacts no
        # text pays nothing for a topic with many local records.
        self._onto = onto
        self._imports: Set[str] = set()
        self._own = ""
        if onto is not None:
            hidden_l = sorted(_hidden_ids(onto))
            self._imports = {str(e.get("ns")).lower() for e in getattr(onto, "imports", None) or []
                             if isinstance(e, dict) and e.get("ns")}
            manifest = getattr(getattr(onto, "repo", None), "manifest", None)
            self._own = str((manifest or {}).get("ns") or "").lower()
            known = set(onto.nodes)
            self._given: Optional[List[str]] = None  # the left-out records' names, read from ``onto`` when needed
        else:
            hidden_l = sorted({str(h) for h in hidden if h})
            self._given = [str(n) for n in names if isinstance(n, str)]
            known = set(hidden_l)
        self.hidden = set(hidden_l)
        self._hidden_l = hidden_l
        self._hidden_low = {h.lower() for h in hidden_l}
        self._known = {k.lower() for k in known}
        self._scopes = {ledger.norm_scope(h) for h in hidden_l}
        self._ready = False

    @property
    def names(self) -> List[str]:
        """The left-out names, aliases and slugs ``scrub`` writes over (built on first use)."""
        if not self.hidden and not self._given:
            return []
        self._prepare()
        return self._names

    def _prepare(self) -> None:
        if self._ready:
            return
        onto, hidden_l = self._onto, self._hidden_l
        public: Set[str] = set()
        given = list(self._given or [])
        if onto is not None:
            hidden_set = self.hidden
            for nid in sorted(onto.nodes):
                if nid in hidden_set:
                    continue
                public.add(util.name_key(nid.split(":", 1)[-1]))
                for value in _names_of(onto.nodes.get(nid)):
                    public.add(util.name_key(value))
            given = [v for nid in hidden_l for v in _names_of(onto.nodes.get(nid))]
        found: Set[str] = set()
        for value in given:
            value = " ".join(value.split())
            key = util.name_key(value)
            if len(value) < NAME_SHORTEST or value == ERASED_NAME or not key or key in public:
                continue
            found.add(value)
        slugs: Set[str] = set()
        handles: Set[str] = set()
        for nid in hidden_l:
            slug = nid.split(":", 1)[-1]
            key = util.name_key(slug)
            if not key or key in public:
                continue
            if " " in key:
                slugs.add(slug)  # the bare slug of a hidden id (pat-green), when it has more than one word
            elif len(slug) >= NAME_SHORTEST:
                handles.add(slug)  # a one-word slug (rose) is a name only after "@"
        self._names = sorted(found | slugs)
        # the name keys a shown text must not hold once redacted: those of more than one word (a one-word name is
        # matched as written only, so "rose bushes" is no reason to hold a decision back)
        self._keys = sorted({k for k in (util.name_key(n) for n in self._names) if " " in k})
        # the keys of the joined ids, which a decision id's slug may hold (dec-...-ask-person-rose-...)
        self._id_keys = sorted({util.name_key(h) for h in hidden_l if " " in util.name_key(h)})
        # the id joined to its kind (person-rose, Person_Rose, personrose), and the one-word names and slugs a
        # handle may name in any case (@rose): looked up in a set, so a topic with many people compiles nothing
        self._joined = {_joined_key(h) for h in hidden_l if len(WORD_RE.findall(h)) > 1}
        self._handles = {n.casefold() for n in sorted(found | handles) if _one_word(n) or n in handles}
        self._ids_re = None
        if hidden_l:
            alternation = "|".join(re.escape(i) for i in sorted(hidden_l, key=lambda s: (-len(s), s)))
            # a letter or digit just before the id, or a letter just after it, makes another word (rosewood);
            # any other text joined to it is weighed by ``_id_sub``
            self._ids_re = re.compile(r"(?<![a-z0-9])((?:[^\s/]+/)*)(?:%s)(?![a-z])(?=([a-z0-9_.@-]*))"
                                      % alternation, re.I)
        self._names_re = _names_re(self._names)
        # the first word of each name, lower case: a text holds a name only where one of its words starts with
        # one, so most texts skip the long names pattern (None: a name starts with no word, so always run it)
        firsts = [WORD_RE.match(n) for n in self._names]
        self._firsts: Optional[Set[str]] = None if not all(firsts) else {f.group(0).lower() for f in firsts if f}
        self._first_lens = sorted({len(f) for f in self._firsts or ()})
        self._ready = True

    def _other_id(self, value: str) -> bool:
        """True when ``value`` is the id of a record that is not left out (lower case, as ``_known`` holds)."""
        low = value.lower()
        return low in self._known and low not in self._hidden_low

    def _longer_ids(self, found: "re.Match[str]") -> List[str]:
        """The longer ids that the text around one id found by ``_ids_re`` may spell: the found id with a longer
        kind before it (``x-person:rose``) and with more slug after it (``person:rose-hip``, ``person:rose.jr``),
        each ending where no letter or digit follows."""
        prefix, tail = found.group(1), found.group(2)
        whole = found.group(0)[len(prefix):]
        start = found.start() + len(prefix)
        heads = [""]
        if not prefix:
            run = KIND_HEAD_RE.search(found.string, 0, start)
            if run is not None:
                text = run.group(0)
                heads += [text[j:] for j in range(len(text)) if text[j].isalpha() and (j == 0 or text[j - 1] == "-")]
        tails = [""] + [tail[:i] for i in range(1, len(tail) + 1) if i == len(tail) or not ID_CHAR_RE.match(tail[i])]
        return [h + whole + t for h in heads for t in tails if h or t]

    def _id_sub(self, found: "re.Match[str]", kept: List[str]) -> str:
        """The mark for one id found by ``_ids_re``. A path before it stays, but for a last ``self/`` or the
        topic's own namespace. An id under an import's namespace (``ns/kind:slug``) names that import's record,
        and a longer id that a record which is not left out has (``person:rose.jr``, ``person:rose-hip``,
        ``x-person:rose``: ``_longer_ids``) names that record: both stay as they are (in ``kept``, behind a
        placeholder until the other passes are done). Any other text joined to the id before or after it
        (``notes/person:rose-2026.txt``, ``my_person:rose``, ``person:rose2``) stays, and the id part is
        replaced."""
        prefix = found.group(1)
        segment = prefix[:-1].rsplit("/", 1)[-1].lower().lstrip("([{\"'<") if prefix else ""
        if (prefix and segment in self._imports) or any(self._other_id(c) for c in self._longer_ids(found)):
            kept.append(found.group(0))
            return KEEP_OPEN + KEEP_STEP * len(kept) + KEEP_OPEN
        if prefix and segment in ("self", self._own):
            prefix = prefix[: -(len(segment) + 1)]
        return prefix + PLACE

    def _may_name(self, text: str) -> bool:
        if self._firsts is None:
            return True
        for word in WORD_RE.findall(text):
            word = word.lower()
            if any(word[:n] in self._firsts for n in self._first_lens):
                return True
        return False

    def _joined_sub(self, found: "re.Match[str]") -> str:
        """``JOINED_RE`` found a run of words: each longest stretch of them that joins to a left-out id
        (``_joined_key``) becomes the placeholder; the rest stays. The slug of a record's id (``term:person-rose``
        when a record that is not left out has that id) is that record's, so it stays."""
        token = found.group(0)
        start = found.start()
        if start and found.string[start - 1] == ":":
            kind = KIND_TAIL_RE.search(found.string, 0, start - 1)
            if kind is not None:
                head = kind.group(0) + ":" + token
                cuts = [m.start() for m in JOIN_CUT_RE.finditer(head)] + [len(head)]
                if any(self._other_id(head[:c]) for c in cuts):
                    return token
        words = [(m.start(), m.end()) for m in WORD_RE.finditer(token)]
        out: List[str] = []
        done = i = 0
        while i < len(words):
            hit = None
            for j in range(len(words) - 1, i - 1, -1):
                if "".join(token[a:b] for a, b in words[i:j + 1]).casefold() in self._joined:
                    hit = j
                    break
            if hit is None:
                i += 1
                continue
            out.extend((token[done:words[i][0]], PLACE))
            done, i = words[hit][1], hit + 1
        out.append(token[done:])
        return "".join(out)

    def _protect(self, out: str, values: Iterable[Any], kept: List[str]) -> str:
        """``out`` with each of ``values`` (a record's own names) behind a placeholder, when a left-out name is a
        word of it; most own names hold none, so most calls compile nothing."""
        if self._names_re is None:
            return out
        pattern = _names_re([v for v in values if isinstance(v, str) and len(v.strip()) >= NAME_SHORTEST
                             and self._names_re.search(v)])
        if pattern is None:
            return out

        def hold(found: "re.Match[str]") -> str:
            kept.append(found.group(0))
            return KEEP_OPEN + KEEP_STEP * len(kept) + KEEP_OPEN
        return pattern.sub(hold, out)

    def scrub(self, value: Any, mark: str = LOCAL_MARK, keep: Iterable[Any] = (), names: bool = True) -> str:
        """``value`` with every left-out id, joined id, handle, name, alias and slug written as ``mark``. ``keep``
        lists the record's own name and aliases, which stay in its own text even when a left-out name is a word of
        them (a shared "Rose bed" and a local person Rose). With ``names`` false only the id forms and handles are
        replaced (for a shared record's own name and aliases). Line breaks and spacing stay as they are."""
        out = PLACE_RE.sub("", "" if value is None else str(value))
        if not self.hidden and not self.names:
            return out
        self._prepare()
        kept: List[str] = []
        if self._ids_re is not None:
            out = self._ids_re.sub(lambda found: self._id_sub(found, kept), out)
        if self._joined:
            out = JOINED_RE.sub(self._joined_sub, out)
        if names:
            out = self._protect(out, keep, kept)
        if self._handles:
            out = HANDLE_RE.sub(lambda f: "@" + PLACE if f.group(1).casefold() in self._handles else f.group(0), out)
        if names and self._names_re is not None and self._may_name(out):
            out = self._names_re.sub(PLACE, out)
        if kept:
            out = KEEP_RE.sub(lambda found: kept[len(found.group(1)) - 1], out)
        return out.replace(PLACE, mark)

    @staticmethod
    def _flat(value: Any) -> str:
        return util.strip_invisible(render.plain(value))

    def text(self, value: Any, keep: Iterable[Any] = ()) -> str:
        """``value`` as one safe line (``render.plain``, invisible characters dropped), through ``scrub``."""
        return self.scrub(self._flat(value), LOCAL_MARK, keep)

    def names_one(self, value: Any, keep: Iterable[Any] = (), names: bool = True) -> bool:
        """True when ``scrub`` would change ``value``: it names a left-out record in a form ``scrub`` replaces."""
        if not isinstance(value, str):
            return False
        clean = PLACE_RE.sub("", value)
        return self.scrub(clean, LOCAL_MARK, keep, names) != clean

    def changed(self, value: Any) -> bool:
        return isinstance(value, str) and self.text(value) != self._flat(value)

    def leaks(self, value: Any) -> bool:
        """True when ``value``, once redacted, still holds a left-out name or slug of more than one word in a form
        ``text`` does not match (``Pat.Green``, ``Pat, Green``): such a text is not shown."""
        if not isinstance(value, str) or not self.names:
            return False
        self._prepare()
        if not self._keys:
            return False
        key = " %s " % util.name_key(self.text(value))
        return any(" %s " % k in key for k in self._keys)

    def scope_left_out(self, scope: Any) -> bool:
        """True when every scope in ``scope`` names a left-out record or a place under one (an empty list names
        none). A kind-wide or topic-wide scope names no record."""
        entries = []
        for item in scope if isinstance(scope, list) else []:
            norm = ledger.norm_scope(item)
            if norm.startswith("self/"):
                norm = norm[len("self/"):]
            if norm:
                entries.append(norm)
        return bool(entries) and all(any(k in self._scopes for k in _scope_keys(e)) for e in entries)

    @staticmethod
    def _texts(rec: Dict[str, Any]) -> List[Any]:
        texts = [rec.get(k) for k in ("question", "chosen", "chosen_text", "rationale")]
        return texts + [o.get("label") for o in rec.get("options") or [] if isinstance(o, dict)]

    def withheld(self, rec: Dict[str, Any]) -> bool:
        """True when the decision is not shown: its scope names only left-out records (``scope_left_out``), or one
        of its words would still name one once redacted (``leaks``). This backs the stand-in id: when the id was
        made from words that named a record, those words must have been redacted, or the decision stays out."""
        return self.scope_left_out(rec.get("scope")) or any(self.leaks(t) for t in self._texts(rec))

    def decision_named(self, rec: Dict[str, Any]) -> bool:
        """The decision's words (question, option labels, chosen text and rationale) or its id name a left-out
        record, or an erase replaced a name in them. The id names one when its slug holds a left-out name or slug
        of more than one word, or a left-out id joined to its kind (``person-rose``)."""
        if any(self.changed(t) or (isinstance(t, str) and ERASED_NAME in t) for t in self._texts(rec)):
            return True
        found = STAND_IN_RE.match(str(rec.get("id") or ""))
        if not found or (not self.hidden and not self._given):
            return False
        self._prepare()
        slug = " %s " % util.name_key(str(rec.get("id"))[len("dec-00000000-"):-(len(found.group(2)) + 1)])
        return any(" %s " % k in slug for k in self._keys + self._id_keys)

    def decision_id(self, rec: Dict[str, Any]) -> str:
        """The id the viewer shows for a decision: its own, or, when ``decision_named``, a stand-in that keeps its
        date and hash (``dec-YYYYMMDD-local-record-<hash>``)."""
        dec_id = str(rec.get("id"))
        if not self.decision_named(rec):
            return dec_id
        found = STAND_IN_RE.match(dec_id)
        if found:
            return "dec-%s-local-record-%s" % (found.group(1), found.group(2))
        return "dec-local-record-%s" % util.sha256_hex(dec_id.encode("utf-8"))[:8]

    def record(self, rec: Dict[str, Any], decisions: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """A shared node or edge as the export carries it: each of its free-text slots (``records.text_slots``:
        summary, note, an edge's key, attrs, quotes, gap notes) and each gap's field and the archive reason through
        ``scrub``, keeping its own name and aliases; its name and aliases through the id forms only; a left-out id
        dropped from ``archived.superseded_by``; and ``archived.decision`` shown as ``decision_id`` shows it (``decisions``: the ledger's records by id). Ids and other fields stay."""
        out = copy.deepcopy(rec)
        if not self.hidden and not self.names:
            return out
        own = _names_of(rec)
        shape = "edge" if "rel" in out or ids.EDGE_RE.match(str(out.get("id") or "")) else "node"
        for container, key, _role, path in records.text_slots(out, shape):
            if path[0] in ("name", "aliases"):
                container[key] = self.scrub(container[key], names=False)
            else:
                container[key] = self.scrub(container[key], LOCAL_MARK, own)
        for g in out.get("gaps") or [] if isinstance(out.get("gaps"), list) else []:
            for k in sorted(g) if isinstance(g, dict) else []:
                if k != "note" and isinstance(g[k], str):  # the note is a text slot; the field may hold words too
                    g[k] = self.scrub(g[k], LOCAL_MARK, own)
        block = out.get("archived")
        if isinstance(block, dict):
            if isinstance(block.get("reason"), str):
                block["reason"] = self.scrub(block["reason"], LOCAL_MARK, own)
            if isinstance(block.get("superseded_by"), list):
                block["superseded_by"] = [i for i in block["superseded_by"] if i not in self.hidden]
            if isinstance(block.get("decision"), str) and block["decision"]:
                known = (decisions or {}).get(block["decision"])
                block["decision"] = self.decision_id(known if isinstance(known, dict) else {"id": block["decision"]})
        return out


def redactor_for(onto: Any) -> Redactor:
    """The ``Redactor`` of a loaded graph, made once per graph object."""
    cache = getattr(onto, "_cache", None)
    if not isinstance(cache, dict):
        return Redactor(onto)
    if "build_redactor" not in cache:
        cache["build_redactor"] = Redactor(onto)
    return cache["build_redactor"]


def _names_of(node: Any) -> List[str]:
    if not isinstance(node, dict):
        return []
    aliases = node.get("aliases") if isinstance(node.get("aliases"), list) else []
    return [v for v in [node.get("name")] + aliases if isinstance(v, str) and v.strip()]


def _source_entry(entry: Dict[str, Any], redactor: Optional["Redactor"] = None) -> Dict[str, Any]:
    title = entry.get("title")
    return {
        "id": entry.get("id"),
        "kind": entry.get("kind"),
        "title": _scrub_title(title, redactor=redactor) if redactor is not None else title,
        "sha256": entry.get("sha256"),
        "bytes": entry.get("bytes"),
        "captured_at": entry.get("captured_at"),
        "url": entry.get("url"),
    }


def _packs_block(onto: Any) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for name in onto.registry.pack_names():
        pack = onto.registry.pack(name)
        if isinstance(pack, dict):
            out[name] = {"sha256": packs.sha_of(pack), "pack": copy.deepcopy(pack)}
    return out


def _richness(onto: Any, warnings: List[str]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """(badge ``{richness, band}`` or None, the whole summary or None)."""
    module = optional("richness")
    if module is None or not hasattr(module, "summary"):
        return None, None
    try:
        summary = module.summary(onto)
    except Exception as exc:  # a measure bug must not block a build; the report says so
        warnings.append("richness failed (%s); the export carries no richness badge" % type(exc).__name__)
        return None, None
    if not isinstance(summary, dict):
        return None, None
    score, band = summary.get("score"), summary.get("band")
    if isinstance(score, bool) or not isinstance(score, int) or not isinstance(band, str) or not band:
        return None, summary
    return {"richness": score, "band": band}, summary


def _check_bundle(bundled: Any, lock_ns: Sequence[str], where: str) -> Dict[str, Dict[str, Any]]:
    """The bundle a peer module returned, checked against C.17: one entry per lock entry, each ``{sha256, export}``
    where ``export`` has ``bundled`` set to ``{}`` and ``sha256`` is the sha of its canonical bytes."""
    if isinstance(bundled, dict) and set(bundled) == {"bundled"} and isinstance(bundled["bundled"], dict):
        bundled = bundled["bundled"]
    if not isinstance(bundled, dict):
        raise DataError("%s: the bundle is not an object" % where)
    if sorted(bundled) != sorted(lock_ns):
        raise DataError("%s: the bundle holds %s but the lock pins %s" % (
            where, ", ".join(sorted(bundled)) or "nothing", ", ".join(sorted(lock_ns)) or "nothing"))
    out: Dict[str, Dict[str, Any]] = {}
    for ns in sorted(bundled):
        item = bundled[ns]
        if not isinstance(item, dict) or not isinstance(item.get("export"), dict):
            raise DataError("%s: bundled %s is not {sha256, export}" % (where, ns))
        obj = dict(item["export"])
        obj["bundled"] = {}
        sha = lockfile.bundle_sha(obj)
        if item.get("sha256") != sha:
            raise DataError("%s: bundled %s: sha256 is not the sha of the flattened export" % (where, ns))
        out[ns] = {"sha256": sha, "export": obj}
    return out


def bundle_from_lock(repo: store.Repo) -> Dict[str, Dict[str, Any]]:
    """Every lock entry's vendored export, flattened (its own ``bundled`` set to ``{}``) and hashed: the C.17
    ``bundled`` block, read from the lock and the vendored files."""
    out: Dict[str, Dict[str, Any]] = {}
    for entry in sorted(lockfile.entries(lockfile.read(repo)), key=lambda e: str(e.get("ns") or "")):
        ns = entry.get("ns")
        if not isinstance(ns, str) or not ns:
            continue
        value = lockfile.read_export(repo, ns)
        if value is None:
            raise DataError("%s is missing; run onto validate" % (lockfile.EXPORT % ns), file=lockfile.EXPORT % ns)
        obj = dict(value)
        obj["bundled"] = {}
        out[ns] = {"sha256": lockfile.bundle_sha(obj), "export": obj}
    return out


def _bundled(repo: store.Repo) -> Dict[str, Dict[str, Any]]:
    lock_ns = [str(e.get("ns")) for e in lockfile.entries(lockfile.read(repo)) if e.get("ns")]
    if not lock_ns:
        return {}
    compose = optional("compose")
    if compose is not None and hasattr(compose, "bundle"):
        return _check_bundle(compose.bundle(repo), lock_ns, "compose.bundle")
    return bundle_from_lock(repo)


def _export(repo: store.Repo, version: str,
            onto: Any = None) -> Tuple[Dict[str, Any], Any, List[str], Optional[Dict[str, Any]]]:
    """(export, the graph it came from, warnings, the richness summary or None)."""
    if not isinstance(version, str) or not VERSION_RE.match(version):
        raise UsageError("version must be unreleased or vN, not %r" % (version,))
    if onto is None:
        onto = graph.Ontology.load(repo)
    warnings: List[str] = []
    node_ids, edge_ids = exported_ids(onto)
    nodes = [onto.nodes[nid] for nid in node_ids]
    edges = [onto.edges[eid] for eid in edge_ids]
    source_ids = _cited_sources(onto, nodes, edges)
    bridges = sum(1 for e in edges if _ns_of(str(e.get("src"))) != _ns_of(str(e.get("dst"))))
    badge, summary = _richness(onto, warnings)
    manifest = repo.manifest
    meta = {
        "format": FORMAT,
        "kit": __version__,
        "name": manifest.get("name"),
        "ns": manifest.get("ns"),
        "title": manifest.get("title"),
        "version": version,
        "data_hash": store.data_hash(repo),
        "last_change": ledger.last_change(repo, exclude=("checkpoint", "release")),
        "packs": _packs_block(onto),
        "imports": sorted((copy.deepcopy(e) for e in lockfile.entries(lockfile.read(repo))),
                          key=lambda e: str(e.get("ns") or "")),
        "counts": {"nodes": len(nodes), "edges": len(edges), "sources": len(source_ids), "bridges": bridges},
        "richness": badge,
        "statuses": [dict(s) for s in STATUSES],
    }
    redactor = redactor_for(onto)
    decisions = _archive_decisions(repo, nodes + edges) if redactor.hidden else {}
    out = {
        "meta": meta,
        "nodes": [redactor.record(n, decisions) for n in nodes],
        "edges": [redactor.record(e, decisions) for e in edges],
        "sources": [_source_entry(onto.sources[sid], redactor) for sid in source_ids],
        "bundled": _bundled(repo),
    }
    errors = records.check(dict(out, nodes=[], edges=[], bundled={}), "export")
    if errors:
        raise DataError("the export fails its schema: %s" % "; ".join(errors[:3]), problems=errors[:20])
    return out, onto, warnings, summary


def _archive_decisions(repo: store.Repo, recs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """The ledger's records of the decisions that archived ``recs``, by id (empty when none did)."""
    wanted = {(r.get("archived") or {}).get("decision") for r in recs if isinstance(r.get("archived"), dict)}
    if not any(wanted) or repo is None:
        return {}
    return {str(k): v for k, v in ledger.all_decisions(repo).items() if k in wanted and isinstance(v, dict)}


def export(repo: store.Repo, version: str = "unreleased") -> Dict[str, Any]:
    """The resolved export (C.17) of ``repo``; ``version`` is ``unreleased`` or the release tag ``vN``."""
    return _export(repo, version)[0]


def export_bytes(obj: Dict[str, Any]) -> bytes:
    """Canonical bytes (C.1), the form ``build/export.json`` is written in and importers verify."""
    return util.canonical_bytes(obj)


# cards ---------------------------------------------------------------------------------------------------------
def cards_for(repo: store.Repo, onto: Any, node_ids: Sequence[str],
              notes: List[str]) -> Optional[Dict[str, Any]]:
    """The cards file (C.18) from ``answers.build_cards``, or None when that module is not built. Only cards of
    exported, active nodes (or imported ones) are kept, and a card whose text names a node left out of the export
    in any form ``Redactor.scrub`` replaces is dropped, since the file is committed by a release (the card's own
    node's name stays in its own card). ``meta`` is set to the loaded data."""
    answers = optional("answers")
    if answers is None or not hasattr(answers, "build_cards"):
        notes.append("cards: not built (the answers module is missing); %s was not written" % CARDS)
        return None
    built = answers.build_cards(onto)
    cards = built.get("cards") if isinstance(built, dict) else None
    if not isinstance(cards, list):
        raise DataError("answers.build_cards returned no card list")
    exported = {nid for nid in node_ids if (onto.nodes.get(nid) or {}).get("status") != "archived"}
    redactor = redactor_for(onto)
    kept: List[Dict[str, Any]] = []
    dropped = 0
    for card in cards:
        cid = card.get("id") if isinstance(card, dict) else None
        if not isinstance(cid, str) or not (cid in exported or ids.is_qualified(cid)):
            dropped += 1
            continue
        own = _names_of(onto.nodes.get(cid))
        if (redactor.names_one(card.get("title"), names=False)
                or any(redactor.names_one(card.get(k), own) for k in ("body", "follow"))):
            dropped += 1
            continue
        kept.append(card)
    kept.sort(key=lambda c: str(c.get("id")))
    out = {"meta": {"data_hash": store.data_hash(repo), "kit": __version__}, "cards": kept}
    errors = records.check(out, "cards_file")
    if errors:
        raise DataError("the cards file fails its schema: %s" % "; ".join(errors[:3]), problems=errors[:20])
    if dropped:
        notes.append("cards: %d card(s) left out: their node is not exported or the text names one that is not"
                     % dropped)
    return out


# the viewer ----------------------------------------------------------------------------------------------------
def _read_viewer_parts() -> Tuple[str, str, str]:
    """``template.html``, ``viewer.css`` and ``viewer.js``, after checking the sentinel contract."""
    parts = []
    for name in ("template.html", "viewer.css", "viewer.js"):
        with open(os.path.join(VIEWER_DIR, name), encoding="utf-8") as fh:
            parts.append(fh.read())
    template, css, js = parts
    for sentinel in (CSS_SENTINEL, JS_SENTINEL, DATA_SENTINEL):
        found = template.count(sentinel)
        if found != 1:
            raise DataError("viewer/template.html must hold %s exactly once (found %d)" % (sentinel, found))
    if template.count(DATA_TAG + DATA_SENTINEL + "</script>") != 1:
        raise DataError("viewer/template.html must hold %s inside %s...</script>" % (DATA_SENTINEL, DATA_TAG))
    for name, text, closer in (("viewer.css", css, "</style"), ("viewer.js", js, "</script")):
        if any(s in text for s in (CSS_SENTINEL, JS_SENTINEL, DATA_SENTINEL)):
            raise DataError("viewer/%s must not hold a sentinel" % name)
        if closer in text.lower():
            raise DataError("viewer/%s must not hold %s" % (name, closer))
    return template, css, js


def embed_json(payload: Any) -> str:
    """The payload as JSON that is safe inside a ``<script>`` element: ``</`` becomes ``<\\/`` and ``<!--`` its
    ``\\u003c`` form, both still valid JSON."""
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return text.replace("</", "<\\/").replace("<!--", "\\u003c!--")


def extract_data(html: str) -> Any:
    """The payload embedded in a viewer page (the inverse of the embedding, for checks and tests)."""
    start = html.index(DATA_TAG) + len(DATA_TAG)
    end = html.index("</script>", start)
    return json.loads(html[start:end])


_EMPTY = (None, "", [], {})


def _slim_node(node: Dict[str, Any]) -> Dict[str, Any]:
    """A node for the viewer: every field except the empty ones."""
    return {k: v for k, v in node.items() if v not in _EMPTY or k in ("id", "name", "kind", "status")}


def _cited(export_obj: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """``{source id: {nodes: [ids], edges: count}}`` over the exported records."""
    out: Dict[str, Dict[str, Any]] = {}
    for kind, rows in (("nodes", export_obj.get("nodes") or []), ("edges", export_obj.get("edges") or [])):
        for rec in rows:
            seen = set()
            for p in rec.get("prov") or []:
                src = p.get("src") if isinstance(p, dict) else None
                if not isinstance(src, str) or src in seen:
                    continue
                seen.add(src)
                entry = out.setdefault(src, {"nodes": [], "edges": 0})
                if kind == "nodes":
                    entry["nodes"].append(rec.get("id"))
                else:
                    entry["edges"] += 1
    return out


def viewer_payload(export_obj: Dict[str, Any], cards: Optional[Dict[str, Any]] = None,
                   history_points: Optional[Sequence[Dict[str, Any]]] = None,
                   node_needs: Optional[Dict[str, Any]] = None, imported: Optional[Dict[str, Any]] = None,
                   richness: Optional[Dict[str, Any]] = None,
                   decisions: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """What the viewer shows, kept small: the export's meta, nodes (empty fields left out) and sources; the edges
    as ``EDGE_COLUMNS`` rows (``untrusted`` is 1 when the edge's trust is ``untrusted``, so the page marks it on
    every surface); which records cite each source; the namespaces of the
    bundled parents; the ids that have a card (the page names the call that reads it); the newest history
    points; the needs of each exported node; the names of imported bridge ends; the richness parts; and the
    decisions in each node's scope (``{items: {dec id: {at, question, chosen}}, nodes: {node id: [dec ids]}}``)."""
    points = []
    for p in list(history_points or [])[-HISTORY_POINTS:]:
        values = p.get("values") if isinstance(p.get("values"), dict) else {}
        points.append({"at": p.get("at"), "kind": p.get("kind"), "label": p.get("label"), "values": values})
    carded = sorted(c["id"] for c in (cards or {}).get("cards") or []
                    if isinstance(c, dict) and isinstance(c.get("id"), str))
    rich = None
    if isinstance(richness, dict):
        rich = {k: richness.get(k) for k in ("score", "band", "parts") if k in richness}
    edges = [[e.get("src"), e.get("rel"), e.get("dst"), e.get("status"), 1 if e.get("background") else 0,
              e.get("note") or "", 1 if e.get("trust") == "untrusted" else 0] for e in export_obj.get("edges") or []]
    return {
        "kit": __version__,
        "export": {"meta": export_obj.get("meta") or {},
                   "nodes": [_slim_node(n) for n in export_obj.get("nodes") or []],
                   "sources": list(export_obj.get("sources") or [])},
        "edges": edges,
        "cited": _cited(export_obj),
        "bundled": sorted((export_obj.get("bundled") or {}).keys()),
        "cards": carded,
        "history": points,
        "needs": dict(node_needs or {}),
        "imported": dict(imported or {}),
        "richness": rich,
        "decisions": {"items": dict((decisions or {}).get("items") or {}),
                      "nodes": dict((decisions or {}).get("nodes") or {})},
    }


def viewer_html(export_obj: Dict[str, Any], cards: Optional[Dict[str, Any]] = None, **extra: Any) -> str:
    """The self-contained viewer page for an export (and its cards). ``extra`` may carry ``history_points``,
    ``node_needs``, ``imported``, ``richness`` and ``decisions`` (see ``viewer_payload``)."""
    template, css, js = _read_viewer_parts()
    data = embed_json(viewer_payload(export_obj, cards, **extra))
    head, tail = template.split(DATA_SENTINEL)
    head = head.replace(CSS_SENTINEL, css).replace(JS_SENTINEL, js)
    tail = tail.replace(CSS_SENTINEL, css).replace(JS_SENTINEL, js)
    return head + data + tail  # the data goes in last, so nothing in it is read as a sentinel


def _node_needs(onto: Any, node_ids: Sequence[str],
                redactor: Optional[Redactor] = None) -> Dict[str, List[Dict[str, Any]]]:
    """The ``NEEDS_PER_NODE`` most severe needs of each node, their text cut short; with ``redactor``, the text
    names no left-out record (a draft link or a contradiction names the record at its other end)."""
    all_n = needs.all_needs(onto)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for nid in node_ids:
        info = all_n.get(nid)
        if not info or not info.get("gaps"):
            continue
        gaps = sorted(info["gaps"], key=lambda g: (-int(g.get("severity") or 0), str(g.get("type"))))
        items = []
        for g in gaps[:NEEDS_PER_NODE]:
            text = g.get("ask") or g.get("note") or g.get("field") or g.get("rel") or ""
            if redactor is not None:
                # before the cut, so a cut name is never half shown; the node's own name stays in its own needs
                text = redactor.text(text, _names_of(onto.nodes.get(nid)))
            items.append({"type": g.get("type"), "severity": g.get("severity"),
                          "text": render.trunc(text, NEED_TEXT)})
        out[nid] = items
    return out


def _scope_keys(scope: str) -> Set[str]:
    """Every string that matches ``scope`` as a prefix under ``ledger.scope_overlaps``: the scope itself and each of
    its prefixes that ends at, or is followed by, a separator."""
    out = {scope}
    for i, ch in enumerate(scope):
        if ch in ledger.SCOPE_SEPARATORS:
            out.add(scope[:i + 1])
            if i:
                out.add(scope[:i])
    return out


def _chosen_text(rec: Dict[str, Any]) -> str:
    if rec.get("chosen_text"):
        return str(rec["chosen_text"])
    for opt in rec.get("options") or []:
        if isinstance(opt, dict) and opt.get("id") == rec.get("chosen"):
            return str(opt.get("label") or rec.get("chosen") or "")
    return str(rec.get("chosen") or "")


def _node_decisions(repo: store.Repo, node_ids: Sequence[str],
                    redactor: Optional[Redactor] = None) -> Dict[str, Dict[str, Any]]:
    """The newest ``DECISIONS_PER_NODE`` active decisions whose scope overlaps each node id (as
    ``ledger.scope_overlaps`` matches them), indexed so a decision that covers many nodes is stored once. With
    ``redactor`` (the build always passes one), a decision whose scope names only left-out records is not shown
    and never named by another one's ``narrows`` or ``narrowed_by``, and the text and id of each one shown name
    no left-out record (``Redactor``)."""
    exact: Dict[str, List[str]] = {}  # a normalised node id -> the node ids
    under: Dict[str, List[str]] = {}  # a scope that is a node id or a prefix of one -> the node ids
    for nid in node_ids:
        norm = ledger.norm_scope(nid)
        exact.setdefault(norm, []).append(nid)
        for key in _scope_keys(norm):
            under.setdefault(key, []).append(nid)
    hits: Dict[str, List[Dict[str, Any]]] = {}
    active = list(ledger.read_decisions(repo))  # active ones, newest first
    known: Dict[str, Dict[str, Any]] = {}  # every decision a shown one may name, by id
    left_out: Set[str] = set()
    if redactor is not None:
        every = ledger.all_decisions(repo) if repo is not None else {}
        known = {str(k): v for k, v in every.items() if isinstance(v, dict)}
        known.update({str(r.get("id")): r for r in active})
        left_out = {k for k, v in known.items() if redactor.withheld(v)}
        active = [r for r in active if str(r.get("id")) not in left_out]

    def shown(dec_id: str) -> str:
        if redactor is None:
            return dec_id
        return redactor.decision_id(known.get(dec_id) or {"id": dec_id})

    def words(value: Any) -> str:
        return redactor.text(value) if redactor is not None else str(value or "")

    narrowed_by: Dict[str, List[str]] = {}  # derived, as onto decisions shows it: both decisions stay active
    for rec in active:
        if isinstance(rec.get("narrows"), str) and rec["narrows"]:
            narrowed_by.setdefault(rec["narrows"], []).append(str(rec.get("id")))
    for rec in active:
        matched: Set[str] = set()
        for scope in rec.get("scope") or []:
            norm = ledger.norm_scope(scope)
            if not norm:
                continue
            matched.update(under.get(norm, ()))  # the scope is the node id or a prefix of it
            for key in _scope_keys(norm):
                matched.update(exact.get(key, ()))  # the node id is a prefix of the scope
        for nid in matched:
            hits.setdefault(nid, []).append(rec)
    items: Dict[str, Dict[str, Any]] = {}
    nodes_out: Dict[str, List[str]] = {}
    for nid in sorted(hits):
        kept = hits[nid][:DECISIONS_PER_NODE]
        nodes_out[nid] = [shown(str(r.get("id"))) for r in kept]
        for r in kept:
            # the words are redacted before the cut, so a cut name is never half shown
            item = {"at": r.get("at"), "question": render.trunc(words(r.get("question")), DECISION_TEXT),
                    "chosen": render.trunc(words(_chosen_text(r)), DECISION_TEXT)}
            if isinstance(r.get("narrows"), str) and r["narrows"] and r["narrows"] not in left_out:
                item["narrows"] = shown(r["narrows"])
            if narrowed_by.get(str(r.get("id"))):
                item["narrowed_by"] = sorted(shown(d) for d in narrowed_by[str(r.get("id"))])
            items[shown(str(r.get("id")))] = item
    return {"items": items, "nodes": nodes_out}


def _imported_ends(onto: Any, edges: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for edge in edges:
        for end in (edge.get("src"), edge.get("dst")):
            if isinstance(end, str) and ids.is_qualified(end):
                node = onto.nodes.get(end) or {}
                out[end] = {"ns": _ns_of(end), "name": node.get("name"), "kind": node.get("kind"),
                            "status": node.get("status") if node else "missing"}
    return out


# rendering the files -------------------------------------------------------------------------------------------
def _render(repo: store.Repo, version: str, html: bool, onto: Any = None) -> Dict[str, Any]:
    """Every output in memory: ``{files: {name: bytes}, counts, cards, viewer, warnings, notes}``. Writes nothing."""
    export_obj, onto, warnings, summary = _export(repo, version, onto)
    notes: List[str] = []
    node_ids = [n["id"] for n in export_obj["nodes"]]
    cards = cards_for(repo, onto, node_ids, notes)
    files: Dict[str, bytes] = {"export.json": export_bytes(export_obj)}
    if cards is not None:
        files["cards.json"] = util.canonical_bytes(cards)
    viewer = None
    if html:
        active = [nid for nid in node_ids if (onto.nodes.get(nid) or {}).get("status") != "archived"]
        redactor = redactor_for(onto)
        points = [dict(p, label=redactor.scrub(p["label"])) if isinstance(p.get("label"), str) else p
                  for p in history.read(repo)]
        page = viewer_html(
            export_obj, cards,
            history_points=points,
            node_needs=_node_needs(onto, active, redactor),
            imported=_imported_ends(onto, export_obj["edges"]),
            richness=summary,
            decisions=_node_decisions(repo, node_ids, redactor),
        ).encode("utf-8")
        size = len(page)
        if size > MAX_BYTES:
            raise Refused("the viewer is %.1f MB, over the %d MB limit; build without --html, or leave records out"
                          % (size / 1e6, MAX_BYTES // (1024 * 1024)), bytes=size)
        if size > WARN_BYTES:
            warnings.append("the viewer is %.1f MB, over the %d MB soft limit" % (size / 1e6,
                                                                                 WARN_BYTES // (1024 * 1024)))
        files["index.html"] = page
        viewer = {"bytes": size}
    return {
        "files": files,
        "version": version,
        "data_hash": export_obj["meta"]["data_hash"],
        "counts": dict(export_obj["meta"]["counts"]),
        "imports": [e.get("ns") for e in export_obj["meta"]["imports"]],
        "cards": None if cards is None else len(cards["cards"]),
        "viewer": viewer,
        "warnings": warnings,
        "notes": notes,
    }


def _out_dir(repo: store.Repo, out_dir: Optional[str], cwd: Optional[str] = None) -> str:
    """The folder to write into: ``build/`` by default. A folder inside the topic repo other than ``build/`` is
    refused, so an output never lands among the topic's data files."""
    default = repo.path(BUILD_DIR)
    if not out_dir:
        return default
    path = os.path.expanduser(out_dir)
    if not os.path.isabs(path):
        path = os.path.join(cwd or os.getcwd(), path)
    path = os.path.abspath(path)
    if store.inside(repo.root, path) and os.path.realpath(path) != os.path.realpath(default):
        raise UsageError("--out %s is inside the topic repo; use build/ or a folder outside the repo" % out_dir)
    return path


def _display(repo: store.Repo, path: str) -> str:
    if store.inside(repo.root, path):
        return os.path.relpath(path, repo.root).replace(os.sep, "/")
    return path


def _refuse_on_problems(repo: store.Repo) -> Dict[str, int]:
    report = validate_mod.validate(repo)
    if not report.ok:
        raise Refused("build refused: validate found %d problem(s); run onto validate and fix them first"
                      % len(report.problems), problems=[p.text() for p in report.problems[:20]])
    return {"warnings": len(report.warnings)}


def _file_report(repo: store.Repo, folder: str, files: Dict[str, bytes]) -> List[Dict[str, Any]]:
    return [{"path": _display(repo, os.path.join(folder, name)), "bytes": len(files[name]),
             "sha256": util.sha256_hex(files[name])} for name in FILE_NAMES if name in files]


def write(repo: store.Repo, out_dir: Optional[str] = None, html: bool = False, version: str = "unreleased", *,
          validated: bool = False, cwd: Optional[str] = None) -> Dict[str, Any]:
    """Write the export and the cards file (and the viewer with ``html``) into ``out_dir`` (default ``build/``).
    Refused while ``validate`` reports problems (``validated`` skips that check for a caller that just ran it).
    Returns the report: files written with their bytes and sha256, counts, cards, viewer size, warnings, notes."""
    folder = _out_dir(repo, out_dir, cwd)
    checks = {"warnings": 0} if validated else _refuse_on_problems(repo)
    with store.write_lock(repo):
        rendered = _render(repo, version, html)
        files = rendered["files"]
        targets = [(os.path.join(folder, name), files[name]) for name in FILE_NAMES if name in files]
        saved: List[Tuple[str, Optional[bytes]]] = []
        try:
            for path, data in targets:
                saved.append((path, _read_or_none(path)))
                store.write_bytes(path, data)
        except BaseException:
            for path, old in reversed(saved):
                _put_back(path, old)
            raise
    report = {k: v for k, v in rendered.items() if k != "files"}
    report["out"] = _display(repo, folder)
    report["files"] = _file_report(repo, folder, files)
    report["validate_warnings"] = checks["warnings"]
    return report


def _read_or_none(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def _put_back(path: str, data: Optional[bytes]) -> None:
    if data is None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return
    store.write_bytes(path, data)


def _clear_caches() -> None:
    graph.clear_cache()
    store.clear_cache()
    lockfile.clear_cache()


def check(repo: store.Repo, out_dir: Optional[str] = None, html: bool = False, *,
          cwd: Optional[str] = None) -> Dict[str, Any]:
    """Build twice in memory (caches cleared in between) and compare the bytes; also say whether the files on
    disk hold the same bytes. Writes nothing. ``same`` is False when the two builds differ."""
    folder = _out_dir(repo, out_dir, cwd)
    _refuse_on_problems(repo)
    _clear_caches()
    first = _render(repo, "unreleased", html)
    _clear_caches()
    second = _render(repo, "unreleased", html)
    files = []
    same = True
    for name in FILE_NAMES:
        a, b = first["files"].get(name), second["files"].get(name)
        if a is None and b is None:
            continue
        equal = a == b
        same = same and equal
        path = os.path.join(folder, name)
        on_disk = _read_or_none(path)
        files.append({"path": _display(repo, path), "same": equal, "bytes": len(a or b or b""),
                      "on_disk": "missing" if on_disk is None else ("same" if on_disk == a else "differs")})
    report = {k: v for k, v in first.items() if k != "files"}
    report["out"] = _display(repo, folder)
    report["check"] = {"same": same, "files": files}
    return report


# the command ---------------------------------------------------------------------------------------------------
def cmd_build(ctx: Context, args: Dict[str, Any]) -> Dict[str, Any]:
    repo = ctx.repo
    if args.get("check"):
        report = check(repo, args.get("out"), bool(args.get("html")), cwd=ctx.cwd)
        return {"report": report, "exit_code": 0 if report["check"]["same"] else 1}
    return {"report": write(repo, args.get("out"), bool(args.get("html")), cwd=ctx.cwd)}


def _counts_text(counts: Dict[str, Any]) -> str:
    return "%d nodes, %d edges, %d sources, %d bridges" % (
        counts.get("nodes", 0), counts.get("edges", 0), counts.get("sources", 0), counts.get("bridges", 0))


def render_build(result: Dict[str, Any], mode: str, ctx: Context) -> List[str]:
    report = result.get("report") or {}
    lines: List[str] = []
    check_block = report.get("check")
    if check_block is not None:
        verdict = "same bytes twice" if check_block.get("same") else "the two builds DIFFER"
        lines.append("build check (%s, data %s): %s" % (report.get("version"), str(report.get("data_hash"))[:12],
                                                          verdict))
        for f in check_block.get("files") or []:
            lines.append("  %s: %s, %d bytes; on disk: %s" % (
                f.get("path"), "same" if f.get("same") else "differs", f.get("bytes", 0), f.get("on_disk")))
    else:
        lines.append("built %s (data %s): %s" % (report.get("version"), str(report.get("data_hash"))[:12],
                                                 _counts_text(report.get("counts") or {})))
        for f in report.get("files") or []:
            lines.append("  wrote %s (%d bytes)" % (f.get("path"), f.get("bytes", 0)))
    if report.get("cards") is not None:
        lines.append("cards: %d" % report["cards"])
    if report.get("imports"):
        lines.append("bundled imports: %s" % ", ".join(str(ns) for ns in report["imports"]))
    for note in report.get("notes") or []:
        lines.append("note: %s" % note)
    for warning in report.get("warnings") or []:
        lines.append("warning: %s" % warning)
    if mode == "text" and check_block is None:
        for f in report.get("files") or []:
            lines.append("  %s sha256 %s" % (f.get("path"), f.get("sha256")))
    return lines
