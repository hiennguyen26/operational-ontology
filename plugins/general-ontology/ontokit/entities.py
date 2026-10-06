"""Duplicate and match scoring: which existing nodes a new name may be.

Scores (a kind pair listed in the local pack's ``kind_map`` counts as the same kind):

- same kind and an equal name key, or a name equal to an alias: 1.0;
- equal name key, different kind: 0.7;
- same kind and (token Jaccard of at least 0.6, or a Damerau distance of at most 2 between names of 6 or more
  characters): 0.8 minus 0.1 per edit, never below 0.5. Names that differ only in their numbers (``Process 586`` and
  ``Process 590``, ``Week 1`` and ``Week 12``) are numbered siblings, never near duplicates.

Only scores of 0.5 or more are returned, sorted by (-score, id). ``duplicates`` pairs local nodes; ``cross`` pairs two
namespaces for bridge suggestion and adds 0.6 for a shared term name and 0.5 for nodes of compatible kinds citing a
source with the same sha or url. Name keys come from ``util.name_key``, so case, accents and punctuation never
matter.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from . import util

_DIGITS_RE = re.compile(r"[0-9]+")


def numbered_siblings(a: str, b: str) -> bool:
    """True when two different name keys are the same once every run of digits is masked (``week 1``, ``week 12``)."""
    return a != b and bool(_DIGITS_RE.search(a)) and _DIGITS_RE.sub("#", a) == _DIGITS_RE.sub("#", b)

THRESHOLD = 0.5
JACCARD = 0.6
MIN_EDIT_LEN = 6
MAX_BLOCK = 50  # a larger block compares neighbours in name order only
WINDOW = 10


class Index(object):
    """Name keys, alias keys and tokens of the active nodes of a graph, by kind."""

    def __init__(self, onto: Any) -> None:
        self.onto = onto
        self.by_kind: Dict[str, List[str]] = {}
        self.names: Dict[str, str] = {}
        self.keys: Dict[str, Set[str]] = {}  # id -> name key plus alias keys
        self.tokens: Dict[str, Set[str]] = {}
        self.by_key: Dict[str, List[str]] = {}
        self.kind: Dict[str, str] = {}
        self.ns: Dict[str, str] = {}
        for nid in sorted(onto.nodes):
            if nid in onto.virtual or not onto.active(nid):
                continue
            node = onto.nodes[nid]
            kind = onto.kind_of(nid)
            name_key = util.name_key(str(node.get("name") or ""))
            aliases = {util.name_key(a) for a in node.get("aliases") or [] if isinstance(a, str) and ":" not in a}
            aliases.discard("")
            self.names[nid] = name_key
            self.keys[nid] = {name_key} | aliases if name_key else set(aliases)
            self.tokens[nid] = set(name_key.split())
            self.kind[nid] = kind
            self.ns[nid] = onto.ns_of(nid)
            self.by_kind.setdefault(kind, []).append(nid)
            for key in self.keys[nid]:
                self.by_key.setdefault(key, []).append(nid)
        self.maps: Dict[str, Set[str]] = {}
        for entry in onto.registry.kind_map():
            a = onto.registry.kind_key(str(entry.get("a") or "")) or entry.get("a")
            b = onto.registry.kind_key(str(entry.get("b") or "")) or entry.get("b")
            if a and b:
                self.maps.setdefault(a, set()).add(b)
                self.maps.setdefault(b, set()).add(a)

    def same_kinds(self, kind: str) -> Set[str]:
        """``kind`` and every kind the kind map pairs with it (transitively)."""
        key = self.onto.registry.kind_key(kind) or kind
        out, todo = {key}, [key]
        while todo:
            for other in self.maps.get(todo.pop(), ()):
                if other not in out:
                    out.add(other)
                    todo.append(other)
        return out


def index(onto: Any) -> Index:
    """The ``Index`` of a graph, cached on it."""
    cached = onto._cache.get("entities")
    if cached is None:
        cached = Index(onto)
        onto._cache["entities"] = cached
    return cached


def jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    return float(len(a & b)) / float(len(a | b))


def _score(idx: Index, cand: str, same_kind: bool, key: str, alias_keys: Set[str],
           toks: Set[str]) -> Optional[Tuple[float, str]]:
    cand_key = idx.names.get(cand, "")
    if same_kind and (key and key == cand_key):
        return 1.0, "same name, same kind"
    if same_kind and ((key and key in idx.keys.get(cand, ())) or (alias_keys & idx.keys.get(cand, set()))):
        return 1.0, "alias match, same kind"
    if key and key == cand_key:
        return 0.7, "same name, different kind"
    if not same_kind or not key or not cand_key or numbered_siblings(key, cand_key):
        return None
    distance = util.edit_distance(key, cand_key, cap=3)
    close = distance <= 2 and len(key) >= MIN_EDIT_LEN and len(cand_key) >= MIN_EDIT_LEN
    overlap = jaccard(toks, idx.tokens.get(cand, set()))
    if not close and overlap < JACCARD:
        return None
    score = round(max(THRESHOLD, 0.8 - 0.1 * min(distance, 3)), 2)
    why = "name edit distance %d, same kind" % distance if close else "shared words %.2f, same kind" % overlap
    return score, why


def match(idx: Index, kind: str, name: str, aliases: Iterable[str] = (), ns: Optional[str] = None,
          exclude: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """Existing nodes ``name`` may be: ``[{id, score, why}]`` with score >= 0.5, sorted by (-score, id). ``ns``
    limits the candidates to one namespace (``self`` for local)."""
    key = util.name_key(name or "")
    alias_keys = {util.name_key(a) for a in aliases or () if isinstance(a, str) and ":" not in a}
    alias_keys.discard("")
    toks = set(key.split())
    kinds = idx.same_kinds(kind)
    skip = set(exclude)
    pool: Set[str] = set()
    for k in kinds:
        pool.update(idx.by_kind.get(k, ()))
    for k in {key} | alias_keys:
        pool.update(idx.by_key.get(k, ()))
    out: List[Dict[str, Any]] = []
    for cand in pool:
        if cand in skip or (ns is not None and idx.ns.get(cand) != ns):
            continue
        scored = _score(idx, cand, idx.kind.get(cand) in kinds, key, alias_keys, toks)
        if scored and scored[0] >= THRESHOLD:
            out.append({"id": cand, "score": scored[0], "why": scored[1]})
    out.sort(key=lambda m: (-m["score"], m["id"]))
    return out


def _blocks(idx: Index, ids: List[str]) -> Dict[str, List[str]]:
    """Blocking keys so duplicate search compares only plausible pairs: shared words, and the first or last two
    characters of the name key (a Damerau distance of 2 rarely changes both)."""
    blocks: Dict[str, List[str]] = {}
    for nid in ids:
        key = idx.names.get(nid, "")
        keys = {"t:" + t for t in idx.tokens.get(nid, ())} | {"k:" + k for k in idx.keys.get(nid, ())}
        if key:
            keys |= {"a:" + key[:2], "z:" + key[-2:]}
        for b in keys:
            blocks.setdefault(b, []).append(nid)
    return blocks


def _candidate_pairs(idx: Index, ids: List[str]) -> List[Tuple[str, str]]:
    """Pairs worth scoring, sorted. A block of up to ``MAX_BLOCK`` ids compares every pair; a larger block (a
    common word or prefix) compares each id with the next ``WINDOW`` ids in name order, so the work stays linear."""
    pairs: Set[Tuple[str, str]] = set()
    for members in _blocks(idx, ids).values():
        if len(members) < 2:
            continue
        if len(members) <= MAX_BLOCK:
            for i, a in enumerate(members):
                for b in members[i + 1:]:
                    pairs.add((a, b) if a < b else (b, a))
            continue
        ordered = sorted(members, key=lambda n: (idx.names.get(n, ""), n))
        for i, a in enumerate(ordered):
            for b in ordered[i + 1: i + 1 + WINDOW]:
                pairs.add((a, b) if a < b else (b, a))
    return sorted(pairs)


def duplicates(onto: Any, kind: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """Likely duplicate pairs among active local nodes: ``[{a, b, score, why}]`` with a < b, sorted by
    (-score, a, b), at most ``limit`` (0 for all)."""
    idx = index(onto)
    ids = [n for n in sorted(idx.names) if idx.ns.get(n) == "self"]
    if kind:
        wanted = idx.same_kinds(kind)
        ids = [n for n in ids if idx.kind.get(n) in wanted]
    same: Dict[str, Set[str]] = {}
    out = []
    for lo, hi in _candidate_pairs(idx, ids):
        kind_lo = idx.kind.get(lo, "")
        if kind_lo not in same:
            same[kind_lo] = idx.same_kinds(kind_lo)
        scored = _score(idx, hi, idx.kind.get(hi) in same[kind_lo], idx.names.get(lo, ""),
                        idx.keys.get(lo, set()) - {idx.names.get(lo, "")}, idx.tokens.get(lo, set()))
        if scored and scored[0] >= THRESHOLD:
            out.append({"a": lo, "b": hi, "score": scored[0], "why": scored[1]})
    out.sort(key=lambda p: (-p["score"], p["a"], p["b"]))
    return out[:limit] if limit else out


def _source_keys(onto: Any, ns: str) -> Dict[str, Set[str]]:
    """``{source id: {"sha:<sha>", "url:<url>"}}`` for the sources of one namespace."""
    if ns == "self":
        rows = list(onto.sources.values())
    else:
        rows = (onto.exports.get(ns) or {}).get("sources") or []
    out: Dict[str, Set[str]] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        keys = set()
        if row.get("sha256"):
            keys.add("sha:" + str(row["sha256"]))
        if row.get("url"):
            keys.add("url:" + str(row["url"]))
        out[str(row["id"])] = keys
    return out


def _cited(onto: Any, nid: str, keys: Dict[str, Set[str]]) -> Set[str]:
    node = onto.node(nid) or {}
    found: Set[str] = set()
    for p in node.get("prov") or []:
        if isinstance(p, dict):
            found |= keys.get(str(p.get("src") or ""), set())
    return found


def cross(onto: Any, ns_a: str, ns_b: str) -> List[Dict[str, Any]]:
    """Candidate pairs between two namespaces for bridge suggestion: ``[{a, b, score, why}]`` (a in ``ns_a``),
    the best score per pair, sorted by (-score, a, b)."""
    idx = index(onto)
    side_a = [n for n in sorted(idx.names) if idx.ns.get(n) == ns_a]
    side_b = [n for n in sorted(idx.names) if idx.ns.get(n) == ns_b]
    best: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def offer(a: str, b: str, score: float, why: str) -> None:
        cur = best.get((a, b))
        if cur is None or score > cur["score"]:
            best[(a, b)] = {"a": a, "b": b, "score": score, "why": why}

    for a in side_a:
        node = onto.nodes[a]
        for m in match(idx, idx.kind[a], str(node.get("name") or ""), node.get("aliases") or [], ns=ns_b):
            offer(a, m["id"], m["score"], m["why"])
    b_keys = {}
    for b in side_b:
        for k in idx.keys.get(b, ()):
            b_keys.setdefault(k, []).append(b)
    for a in side_a:
        a_is_term = idx.kind.get(a, "").split("/")[-1] == "term"
        for k in idx.keys.get(a, ()):
            for b in b_keys.get(k, ()):
                if a_is_term or idx.kind.get(b, "").split("/")[-1] == "term":
                    offer(a, b, 0.6, "shared term name")
    keys_a, keys_b = _source_keys(onto, ns_a), _source_keys(onto, ns_b)
    cited_b = {b: _cited(onto, b, keys_b) for b in side_b}
    for a in side_a:
        cited_a = _cited(onto, a, keys_a)
        if not cited_a:
            continue
        kinds = idx.same_kinds(idx.kind[a])
        for b in side_b:
            if idx.kind.get(b) in kinds and cited_a & cited_b[b]:
                offer(a, b, 0.5, "cite the same source")
    return sorted(best.values(), key=lambda p: (-p["score"], p["a"], p["b"]))
