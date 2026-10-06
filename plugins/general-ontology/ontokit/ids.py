"""The id grammar, parsing, qualification and the id makers.

::

    NS     = [a-z][a-z0-9-]{0,31}            reserved: self, imp
    KIND   = [a-z][a-z0-9-]{0,31}
    REL    = [a-z][a-z0-9_]{0,31}
    SLUG   = [a-z0-9][a-z0-9._@-]{0,79}
    LOCAL  = KIND ":" SLUG                   crop:tomato
    QUAL   = NS "/" LOCAL                    garden/crop:tomato
    QKIND  = NS "/" KIND
    EDGE   = "e:" 12 hex                     sha256(src \\x1f rel \\x1f dst \\x1f key)[:12]
    SOURCE = "src-" 12 hex
    PROP   = "prop-" YYYYMMDD "-" 6 hex
    DEC    = "dec-" YYYYMMDD "-" SLUG(<=40) "-" 4 hex
    CHG    = "chg-" YYYYMMDD "-" 6 hex
    ANS    = "ans-" YYYYMMDD "-" 6 hex
    IMPSRC = "imp:" NS "@" 12 hex

A hash collision (the id exists with other content) extends the hash by 2 hex characters until the id is unique.
Record ids (edges, sources, proposals, decisions, changes, answers, import pseudo-sources) are not node ids, so
``parse`` refuses them. ``self/`` is accepted on input and dropped: local ids are always written bare.
"""

from __future__ import annotations

import re
from collections import namedtuple
from typing import Any, Iterable, Optional, Tuple

from . import util
from .errors import DataError, UsageError

NS_P = r"[a-z][a-z0-9-]{0,31}"
KIND_P = r"[a-z][a-z0-9-]{0,31}"
REL_P = r"[a-z][a-z0-9_]{0,31}"
SLUG_P = r"[a-z0-9][a-z0-9._@-]{0,79}"
LOCAL_P = KIND_P + ":" + SLUG_P
QUAL_P = NS_P + "/" + LOCAL_P
QKIND_P = NS_P + "/" + KIND_P

NS_RE = re.compile(r"^%s\Z" % NS_P)
KIND_RE = re.compile(r"^%s\Z" % KIND_P)
REL_RE = re.compile(r"^%s\Z" % REL_P)
SLUG_RE = re.compile(r"^%s\Z" % SLUG_P)
LOCAL_RE = re.compile(r"^(%s):(%s)\Z" % (KIND_P, SLUG_P))
QUAL_RE = re.compile(r"^(%s)/(%s):(%s)\Z" % (NS_P, KIND_P, SLUG_P))
QKIND_RE = re.compile(r"^(%s)/(%s)\Z" % (NS_P, KIND_P))
EDGE_RE = re.compile(r"^e:[0-9a-f]{12}(?:[0-9a-f]{2}){0,4}\Z")
SOURCE_RE = re.compile(r"^src-[0-9a-f]{12}\Z")
PROP_RE = re.compile(r"^prop-[0-9]{8}-[0-9a-f]{6,10}\Z")
DEC_RE = re.compile(r"^dec-[0-9]{8}-[a-z0-9-]{1,40}-[0-9a-f]{4,8}\Z")
CHG_RE = re.compile(r"^chg-[0-9]{8}-[0-9a-f]{6,10}\Z")
ANS_RE = re.compile(r"^ans-[0-9]{8}-[0-9a-f]{6,10}\Z")
IMPSRC_RE = re.compile(r"^imp:(%s)@([0-9a-f]{12})\Z" % NS_P)

RESERVED_NS = ("self", "imp")
# Kind names a pack may not declare: they are record prefixes or virtual kinds.
RESERVED_KINDS = ("e", "src", "imp", "source", "prop", "dec", "chg", "ans")

# prefix -> (regex, base hash length, max hash length)
RECORD_KINDS = {
    "e": (EDGE_RE, 12, 20),
    "src": (SOURCE_RE, 12, 12),
    "prop": (PROP_RE, 6, 10),
    "dec": (DEC_RE, 4, 8),
    "chg": (CHG_RE, 6, 10),
    "ans": (ANS_RE, 6, 10),
    "imp": (IMPSRC_RE, 12, 12),
}
DEC_SLUG_MAX = 40

Ref = namedtuple("Ref", "ns kind slug")


def record_prefix(id: Any) -> Optional[str]:
    """``e``, ``src``, ``prop``, ``dec``, ``chg``, ``ans`` or ``imp`` for a record id, else None."""
    if not isinstance(id, str):
        return None
    for prefix, (rx, _base, _max) in RECORD_KINDS.items():
        if rx.match(id):
            return prefix
    return None


def is_record(id: Any) -> bool:
    return record_prefix(id) is not None


def split_ns(id: str) -> Tuple[Optional[str], str]:
    """``("garden", "crop:tomato")`` for ``garden/crop:tomato``; ``(None, id)`` for a local id. ``self/`` is
    dropped. No validation: use ``parse`` for that."""
    if isinstance(id, str) and "/" in id:
        ns, local = id.split("/", 1)
        if ns == "self":
            return None, local
        return ns, local
    return None, id


def parse(id: Any) -> Ref:
    """``Ref(ns, kind, slug)`` for a node id, ``ns`` None for a local id. Raises ``UsageError``."""
    if not isinstance(id, str) or not id:
        raise UsageError("not an id: %r" % (id,))
    if is_record(id):
        raise UsageError("%s is a record id, not a node id" % id)
    m = LOCAL_RE.match(id)
    if m:
        return Ref(None, m.group(1), m.group(2))
    m = QUAL_RE.match(id)
    if m:
        ns = m.group(1)
        if ns == "self":
            return Ref(None, m.group(2), m.group(3))
        if ns in RESERVED_NS:
            raise UsageError("%s uses the reserved namespace %r" % (id, ns))
        return Ref(ns, m.group(2), m.group(3))
    raise UsageError("not a valid id: %r (expected kind:slug or ns/kind:slug, lower case)" % id)


def is_local(id: Any) -> bool:
    return isinstance(id, str) and bool(LOCAL_RE.match(id)) and not is_record(id)


def is_qualified(id: Any) -> bool:
    if not isinstance(id, str):
        return False
    m = QUAL_RE.match(id)
    return bool(m) and m.group(1) not in RESERVED_NS


def qualify(ns: Optional[str], local_id: str) -> str:
    """``ns/local_id``. A local id stays bare for ``ns`` None, ``""`` or ``self``; an id that is already
    qualified (for example a bundled parent's id inside an import) is returned unchanged."""
    if not ns or ns == "self":
        return split_ns(local_id)[1] if local_id.startswith("self/") else local_id
    if "/" in local_id:
        return local_id
    if not NS_RE.match(ns) or ns in RESERVED_NS:
        raise UsageError("not a valid namespace: %r" % ns)
    return "%s/%s" % (ns, local_id)


def edge_id(src: str, rel: str, dst: str, key: str = "") -> str:
    """The edge id: ``e:`` + sha256 of the endpoints as stored, the relation and the key, 12 hex."""
    return "e:" + util.sha256_text("\x1f".join((src, rel, dst, key or "")))[:12]


def same_edge(rec: Any, src: str, rel: str, dst: str, key: str = "") -> bool:
    """True when the edge record ``rec`` links ``src`` to ``dst`` by ``rel`` under ``key``."""
    return (isinstance(rec, dict) and rec.get("src") == src and rec.get("rel") == rel and rec.get("dst") == dst
            and str(rec.get("key") or "") == (key or ""))


def edge_id_in(edges: Any, src: str, rel: str, dst: str, key: str = "") -> str:
    """The id this edge has among ``edges`` (a mapping of id to record, or anything with ``get``): the 12-hex
    ``edge_id``, extended by 2 hex characters at a time while that id is held by an edge with other endpoints, relation
    or key (the C.2 collision rule). The id of an equal edge already stored is returned as it is."""
    full = util.sha256_text("\x1f".join((src, rel, dst, key or "")))
    _rx, base, most = RECORD_KINDS["e"]
    n = base
    while n <= most:
        eid = "e:" + full[:n]
        rec = edges.get(eid)
        if rec is None or same_edge(rec, src, rel, dst, key):
            return eid
        n += 2
    raise DataError("cannot make a unique edge id for %s -%s-> %s (e:%s... is taken)" % (src, rel, dst, full[:base]))


def _hash_of(body: Any) -> str:
    if isinstance(body, bytes):
        return util.sha256_hex(body)
    if isinstance(body, str):
        return util.sha256_text(body)
    return util.sha256_hex(util.canonical_bytes(body))


def _day(date: Optional[str]) -> str:
    text = (date or util.today()).replace("-", "")[:8]
    if not re.match(r"^[0-9]{8}\Z", text):
        raise UsageError("not a date: %r" % (date,))
    return text


def record_id(
    prefix: str, body: Any, date: Optional[str] = None, slug: Optional[str] = None, taken: Iterable[str] = frozenset()
) -> str:
    """A record id for ``body``: bytes and text are hashed as they are, anything else by its canonical bytes.
    ``date`` (``YYYY-MM-DD`` or ``YYYYMMDD``, default today) is used by prop, dec, chg and ans; ``slug`` is
    required for dec (slugified, at most 40 characters). ``taken`` holds ids already used by *other* content:
    the hash is extended by 2 hex characters until the id is free."""
    if prefix not in RECORD_KINDS or prefix == "imp":
        raise UsageError("unknown record prefix %r" % prefix)
    rx, base, most = RECORD_KINDS[prefix]
    digest = _hash_of(body)
    taken_set = taken if isinstance(taken, (set, frozenset, dict)) else set(taken)
    if prefix == "e":
        head = "e:"
    elif prefix == "src":
        head = "src-"
    elif prefix == "dec":
        if not slug:
            raise UsageError("a decision id needs a slug")
        dec_slug = util.slugify(slug)[:DEC_SLUG_MAX].rstrip("-") or "x"
        head = "dec-%s-%s-" % (_day(date), dec_slug)
    else:
        head = "%s-%s-" % (prefix, _day(date))
    n = base
    while n <= most:
        candidate = head + digest[:n]
        if candidate not in taken_set:
            return candidate
        n += 2
    raise DataError("cannot make a unique %s id for this content (%s... is taken)" % (prefix, head + digest[:base]))


def new_node_id(kind: str, name: str, taken: Iterable[str]) -> str:
    """``kind:slug`` for a new node; a slug already taken within the kind gets ``-2``, ``-3`` and so on."""
    if not isinstance(kind, str) or not KIND_RE.match(kind) or kind in RESERVED_KINDS:
        raise UsageError("not a valid kind: %r" % (kind,))
    taken_set = taken if isinstance(taken, (set, frozenset, dict)) else set(taken)
    base = "%s:%s" % (kind, util.slugify(name))
    if base not in taken_set:
        return base
    n = 2
    while "%s-%d" % (base, n) in taken_set:
        n += 1
    return "%s-%d" % (base, n)


def impsrc(ns: str, commit: str) -> str:
    """The pseudo-source id of an import's pinned export: ``imp:<ns>@<commit[:12]>``."""
    return "imp:%s@%s" % (ns, commit[:12])
