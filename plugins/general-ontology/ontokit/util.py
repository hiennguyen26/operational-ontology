"""Small shared helpers: canonical JSON, hashing, the clock, name keys, slugs and edit distance.

Everything here is pure and deterministic. The clock is the one exception, and it honours ``ONTO_FIXED_NOW``
(an ISO timestamp such as ``2026-09-28T12:00:00Z``) so tests and demos produce the same bytes every run.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import re
import unicodedata
from datetime import datetime, timezone
from types import ModuleType
from typing import Any, Dict, List, Optional

FIXED_NOW_ENV = "ONTO_FIXED_NOW"
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
SLUG_MAX = 60

_WS_RE = re.compile(r"\s+")
_SLUG_DROP_RE = re.compile(r"[^a-z0-9._@-]")
_DASHES_RE = re.compile(r"-{2,}")
_LEAD_RE = re.compile(r"^[^a-z0-9]+")


# canonical JSON ------------------------------------------------------------------------------------------------
def canonical_line(obj: Any) -> str:
    """One JSONL line (no newline): sorted keys, UTF-8 kept, no spaces. NaN and Infinity raise ``ValueError``:
    they are not JSON (RFC 8259), so they never reach a file."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def canonical_bytes(obj: Any) -> bytes:
    """The canonical file form: sorted keys, indent 1, UTF-8, one trailing newline (NaN and Infinity raise)."""
    return (json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=1, allow_nan=False) + "\n").encode("utf-8")


def _no_constant(name: str) -> Any:
    raise ValueError("%s is not a JSON number" % name)


def _finite_float(literal: str) -> float:
    """A JSON number with a fraction or an exponent, refused when it does not fit a float: ``1e400`` would read
    as Infinity, which no canonical write can hold (``allow_nan=False``)."""
    value = float(literal)
    if math.isinf(value) or math.isnan(value):
        raise ValueError("%s is out of range for a JSON number" % literal[:40])
    return value


# nesting depth --------------------------------------------------------------------------------------------------
JSON_MAX_DEPTH = 500
"""The deepest JSON nesting the kit reads or writes. Older Pythons raise ``RecursionError`` near 1000 levels and
3.12 to 3.14 often do not, so the kit checks the depth itself before parsing or dumping: the same input gets the
same answer on every version."""


class NestingError(ValueError, RecursionError):
    """JSON nested deeper than ``JSON_MAX_DEPTH``. It is both a ``ValueError`` (invalid input) and a
    ``RecursionError`` (what older Pythons raised), so every existing handler keeps its message."""


_JSON_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"', re.S)
_NOT_BRACKET_RE = re.compile(r"[^\[\]{}]+")
_BRACKET_STEP = {"[": 1, "{": 1, "]": -1, "}": -1}


def json_depth_over(text: str, limit: int = JSON_MAX_DEPTH) -> bool:
    """True when the JSON text ``text`` opens more than ``limit`` arrays or objects inside each other. Brackets
    inside strings do not count. A text with no more than ``limit`` opening brackets is answered without a scan."""
    if text.count("[") + text.count("{") <= limit:
        return False
    if '"' in text:
        text = _JSON_STRING_RE.sub("", text)
    depth = 0
    for char in _NOT_BRACKET_RE.sub("", text):
        depth += _BRACKET_STEP[char]
        if depth > limit:
            return True
    return False


def value_depth_over(value: Any, limit: int = JSON_MAX_DEPTH) -> bool:
    """True when ``value`` nests dicts, lists or tuples more than ``limit`` deep. Walks without recursion and stops
    at the first container past the limit."""
    stack: List[Any] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            children: Any = item.values()
        elif isinstance(item, (list, tuple)):
            children = item
        else:
            continue
        if depth > limit:
            return True
        for child in children:
            if isinstance(child, (dict, list, tuple)):
                stack.append((child, depth + 1))
    return False


def check_json_depth(text: str) -> None:
    """Raise ``NestingError`` when ``text`` nests deeper than ``JSON_MAX_DEPTH``."""
    if json_depth_over(text):
        raise NestingError("nested too deeply")


def loads_strict(text: str) -> Any:
    """``json.loads`` that refuses ``NaN``, ``Infinity`` and ``-Infinity`` (Python accepts them; JSON does not), a
    number too large for a float (``1e400``, which Python reads as Infinity), and nesting deeper than
    ``JSON_MAX_DEPTH`` (``NestingError``, checked before parsing)."""
    check_json_depth(text)
    return json.loads(text, parse_constant=_no_constant, parse_float=_finite_float)


def _unique_pairs(pairs: List[Any]) -> Dict[str, Any]:
    out = dict(pairs)
    if len(out) != len(pairs):
        seen: set = set()
        for key, _value in pairs:
            if key in seen:
                raise ValueError("the key %s appears twice in one object (a hand-merged line?); keep one" % (
                    json.dumps(key, ensure_ascii=True)[:60],))
            seen.add(key)
    return out


_SURROGATE_ESCAPE_RE = re.compile(r"\\u[dD][89a-fA-F][0-9a-fA-F]{2}")
_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def surrogate_problem(value: Any, path: str = "$") -> Optional[str]:
    """``'$.path: ...'`` for the first string (key or value) in ``value`` holding a lone surrogate (half of a
    character: what a cut emoji leaves, or a byte of another encoding read as UTF-8 with ``surrogateescape``), or
    None. Such a string cannot be written as UTF-8, so it may never reach a file."""
    if isinstance(value, str):
        found = _SURROGATE_RE.search(value)
        if found is None:
            return None
        code = ord(found.group(0))
        if 0xDC80 <= code <= 0xDCFF:
            return "%s: holds the byte 0x%02X, which is not UTF-8 (text in another encoding?)" % (path, code - 0xDC00)
        return "%s: holds U+%04X, half of a character (a cut emoji?)" % (path, code)
    if isinstance(value, dict):
        for key in value:
            found_key = surrogate_problem(key, path)
            if found_key:
                return found_key
            found_value = surrogate_problem(value[key], "%s.%s" % (path, key))
            if found_value:
                return found_value
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            found_item = surrogate_problem(item, "%s[%d]" % (path, i))
            if found_item:
                return found_item
    return None


def loads_record(text: str) -> Any:
    """``loads_strict`` for the topic's own data files: it also refuses a key repeated in one object (``json.loads``
    keeps the last silently, so a rewrite would drop the other value) and a ``\\uD8xx`` escape that leaves a lone
    surrogate (no rewrite could encode it); like ``loads_strict``, a number too large for a float and nesting deeper
    than ``JSON_MAX_DEPTH``. Raises ``ValueError``."""
    check_json_depth(text)
    value = json.loads(text, parse_constant=_no_constant, parse_float=_finite_float, object_pairs_hook=_unique_pairs)
    if "\\u" in text and _SURROGATE_ESCAPE_RE.search(text):
        found = surrogate_problem(value)
        if found:
            raise ValueError(found)
    return value


# hashing -------------------------------------------------------------------------------------------------------
def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def short_hash(obj: Any, n: int) -> str:
    """The first ``n`` hex characters of the sha256 of ``obj``'s canonical bytes."""
    return sha256_hex(canonical_bytes(obj))[:n]


# clock ---------------------------------------------------------------------------------------------------------
def parse_ts(s: str) -> datetime:
    """A timestamp or a date as an aware UTC datetime. A trailing ``Z`` means UTC; a naive value is taken as UTC.
    Raises ``ValueError`` on anything else."""
    if not isinstance(s, str) or not s.strip():
        raise ValueError("not a timestamp: %r" % (s,))
    text = s.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    when = datetime.fromisoformat(text)
    if when.tzinfo is None:
        return when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def now() -> datetime:
    """The current UTC time to the second, or ``$ONTO_FIXED_NOW`` when it is set (placeholders count as unset)."""
    fixed = os.environ.get(FIXED_NOW_ENV, "")
    if fixed.strip() and not fixed.strip().startswith("$"):
        return parse_ts(fixed)
    return datetime.now(timezone.utc).replace(microsecond=0)


def now_iso() -> str:
    return now().strftime(TS_FORMAT)


def today() -> str:
    return now().strftime("%Y-%m-%d")


def fmt_ts(when: datetime) -> str:
    """``YYYY-MM-DDTHH:MM:SSZ`` for an aware or naive (taken as UTC) datetime."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime(TS_FORMAT)


# text keys -----------------------------------------------------------------------------------------------------
# default-ignorable code points that are not in category Cf: they print as nothing, so a name holding one must key
# as the name without it (Cf itself: soft hyphen, zero-width space and joiners, word joiner, BOM, bidi marks)
_IGNORABLE = frozenset([0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x3164, 0xFFA0]
                       + list(range(0x180B, 0x1810)) + list(range(0xFE00, 0xFE10)) + list(range(0xE0100, 0xE01F0)))


def invisible(ch: str) -> bool:
    """True for a character that prints as nothing (Unicode category Cf, or another default-ignorable code point)."""
    return unicodedata.category(ch) == "Cf" or ord(ch) in _IGNORABLE


def strip_invisible(text: str) -> str:
    """``text`` without its invisible characters (``invisible``), for a name or an alias. A zero-width joiner or
    non-joiner stays where it joins two characters that are not ASCII letters or digits (an emoji sequence, a
    Persian word); between ASCII letters or digits it is what a web page leaves inside a word, so it goes."""
    if not any(invisible(ch) for ch in text):
        return text
    out = []
    for i, ch in enumerate(text):
        if not invisible(ch):
            out.append(ch)
            continue
        if ch in "\u200c\u200d":
            prev, nxt = text[i - 1:i], text[i + 1:i + 2]
            if not (prev.isascii() and prev.isalnum() and nxt.isascii() and nxt.isalnum()):
                out.append(ch)
    return "".join(out)


def name_key(s: str) -> str:
    """The matching key of a name: NFKD, combining marks and invisible characters (``invisible``: a soft hyphen or
    a zero-width space copied from a web page) dropped, casefolded, every other non-alphanumeric character turned
    into a space, whitespace collapsed. ``"Crème  Brûlée!"`` becomes ``"creme brulee"``, and ``"Ba\u00adsil"``
    becomes ``"basil"``."""
    if not s:
        return ""
    decomposed = unicodedata.normalize("NFKD", s)
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch) and not invisible(ch)).casefold()
    spaced = "".join(ch if ch.isalnum() else " " for ch in plain)
    return " ".join(spaced.split())


def slugify(name: str) -> str:
    """The slug part of a node id: ``name_key`` with spaces as ``-``, only ``[a-z0-9._@-]`` kept, repeated ``-``
    collapsed, leading non-alphanumerics stripped, cut to 60 (and trailing ``-`` dropped after the cut). A name
    with no Latin letters or digits (for example a non-Latin script) becomes ``"x" + sha256(name)[:8]``."""
    slug = name_key(name).replace(" ", "-")
    slug = _SLUG_DROP_RE.sub("", slug)
    slug = _DASHES_RE.sub("-", slug)
    slug = _LEAD_RE.sub("", slug)
    slug = slug[:SLUG_MAX].rstrip("-")
    if not slug:
        return "x" + sha256_text(name or "")[:8]
    return slug


def tokens(s: str) -> List[str]:
    return name_key(s).split()


def normalize_ws(s: str) -> str:
    """Runs of whitespace become one space, then the ends are stripped."""
    return _WS_RE.sub(" ", s or "").strip()


def edit_distance(a: str, b: str, cap: int = 3) -> int:
    """Damerau distance (optimal string alignment: a transposition of neighbours counts 1). Returns ``cap + 1`` as
    soon as the distance is known to exceed ``cap``, so long unrelated strings cost little."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) > cap:
        return cap + 1
    if not la or not lb:
        return min(max(la, lb), cap + 1)
    prev2: List[int] = []
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        ca = a[i - 1]
        for j in range(1, lb + 1):
            cb = b[j - 1]
            cost = 0 if ca == cb else 1
            value = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                value = min(value, prev2[j - 2] + 1)
            cur[j] = value
        if min(cur) > cap:
            return cap + 1
        prev2, prev = prev, cur
    return prev[lb] if prev[lb] <= cap else cap + 1


# optional package modules ----------------------------------------------------------------------------------------
def module_missing(exc: ImportError, name: str) -> bool:
    """True when ``exc`` says the module ``name`` itself (or a parent package) does not exist, as opposed to an
    import failing inside a module that exists."""
    missing = getattr(exc, "name", None)
    return isinstance(exc, ModuleNotFoundError) and bool(missing) and (
        name == missing or name.startswith(str(missing) + "."))


def optional_module(name: str) -> Optional[ModuleType]:
    """``ontokit.<name>`` when that package module is built, else None. A built module whose own import fails
    raises, so a broken package reads as the bug it is, never as "not built"."""
    full = name if name.startswith("ontokit.") else "ontokit." + name
    try:
        return importlib.import_module(full)
    except ImportError as exc:
        if module_missing(exc, full):
            return None
        raise
