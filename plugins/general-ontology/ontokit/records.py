"""Record schema checks against ``schema/records.schema.json``.

The schema is loaded once per process. ``check(obj, def_name)`` returns ``'$.path: message'`` strings, empty when
the record is valid. ``DEFS`` names every record type (C.3 to C.19); ``op_<name>`` covers one proposal op each and
``op`` dispatches on the ``op`` field.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from . import schema_lite

SCHEMA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema")
RECORDS_SCHEMA = os.path.join(SCHEMA_DIR, "records.schema.json")

OP_NAMES = (
    "add_node",
    "update_node",
    "add_edge",
    "update_edge",
    "merge",
    "archive",
    "add_gap",
    "add_kind",
    "add_relation",
    "add_field",
    "map_kinds",
    "add_question",
)
DEFS = (
    ("node", "edge", "prov", "gap", "archived", "source", "question", "answer", "proposal", "proposal_draft", "op")
    + tuple("op_" + name for name in OP_NAMES)
    + (
        "review",
        "applied",
        "decision",
        "change",
        "point",
        "lock",
        "lock_entry",
        "export",
        "card",
        "cards_file",
        "manifest",
        "ontology_manifest",
    )
)

_SCHEMA: Optional[Dict[str, Any]] = None


def schema() -> Dict[str, Any]:
    """The parsed records schema (loaded once)."""
    global _SCHEMA
    if _SCHEMA is None:
        with open(RECORDS_SCHEMA, encoding="utf-8") as fh:
            _SCHEMA = json.load(fh)
    return _SCHEMA


def check(obj: Any, def_name: str) -> List[str]:
    """Errors for ``obj`` against ``$defs/<def_name>``, as ``'$.path: message'`` strings."""
    return schema_lite.validate(obj, def_name, schema())


def is_valid(obj: Any, def_name: str) -> bool:
    return not check(obj, def_name)


# free text ------------------------------------------------------------------------------------------------------
# The strings people and agents write, as opposed to ids, kinds and other grammar. ``text_slots`` finds them in a
# record, an op or a proposal, each with a role: ``line`` (one line: names, aliases, attrs values, notes, keys),
# ``block`` (may span lines: summaries, reasons, gap notes), ``quote`` (a provenance quote, which must match its
# source text as stored) and ``pack`` (the declarations and questions of pack ops). The proposal checks refuse
# control characters in them and run the sanitizer over them; validate reports what slips into the files.
LINE, BLOCK, QUOTE, PACK = "line", "block", "quote", "pack"
_SET_ROLES = {"name": LINE, "summary": BLOCK, "aliases": LINE, "note": LINE}
_CONTROL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f  ]")
_LINE_BREAK_RE = re.compile("[\t\n\r]")
# invisible formatting characters that make text display other than it reads: the bidi embeddings, overrides,
# isolates and marks, and the zero-width space, word joiner and byte order mark. Refused in what people and agents
# write (a quote keeps them, since it must match its source as stored); the joiners U+200C and U+200D stay, as
# scripts and emoji need them.
_FORMAT_RE = re.compile("[\u061c\u200b\u200e\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]")
_SURROGATE_RE = re.compile("[\ud800-\udfff]")  # half of a character: no file can hold it (UTF-8)

Slot = Tuple[Any, Any, str, Tuple[Any, ...]]  # (container, key, role, json path)


def _slots(container: Any, key: Any, role: str, path: Tuple[Any, ...], out: List[Slot]) -> None:
    value = container[key] if isinstance(container, list) else container.get(key)
    if isinstance(value, str):
        out.append((container, key, role, path))
    elif isinstance(value, list):
        for i in range(len(value)):
            _slots(value, i, role, path + (i,), out)
    elif isinstance(value, dict):
        for k in sorted(value):
            _slots(value, k, role, path + (k,), out)


def _at(obj: Any, key: str, role: str, path: Tuple[Any, ...], out: List[Slot]) -> None:
    if isinstance(obj, dict) and key in obj:
        _slots(obj, key, role, path + (key,), out)


def _prov_slots(op: Dict[str, Any], path: Tuple[Any, ...], out: List[Slot]) -> None:
    for i, p in enumerate(op.get("prov") or [] if isinstance(op.get("prov"), list) else []):
        _at(p, "quote", QUOTE, path + ("prov", i), out)


def _op_slots(op: Any, path: Tuple[Any, ...], out: List[Slot]) -> None:
    if not isinstance(op, dict):
        return
    kind = op.get("op")
    if kind == "add_node":
        node = op.get("node")
        for key, role in (("name", LINE), ("aliases", LINE), ("attrs", LINE), ("summary", BLOCK)):
            _at(node, key, role, path + ("node",), out)
        for i, gap in enumerate(node.get("gaps") or [] if isinstance(node, dict) and isinstance(node.get("gaps"),
                                                                                                  list) else []):
            _at(gap, "note", BLOCK, path + ("node", "gaps", i), out)
    elif kind == "add_edge":
        for key in ("note", "key"):
            _at(op.get("edge"), key, LINE, path + ("edge",), out)
    elif kind in ("update_node", "update_edge"):
        sets = op.get("set")
        for key in sorted(sets) if isinstance(sets, dict) else []:
            role = LINE if str(key).startswith("attrs.") else _SET_ROLES.get(str(key))
            if role:
                _slots(sets, key, role, path + ("set", key), out)
        _at(op, "reason", BLOCK, path, out)
    elif kind == "merge":
        _at(op, "reason", BLOCK, path, out)
    elif kind == "archive":
        _at(op.get("archived"), "reason", BLOCK, path + ("archived",), out)
    elif kind == "add_gap":
        _at(op.get("gap"), "note", BLOCK, path + ("gap",), out)
    elif kind in ("add_kind", "add_relation", "add_field"):
        for key in ("kind", "relation", "schema"):
            _at(op, key, PACK, path, out)
    elif kind == "add_question":
        _at(op, "question", PACK, path, out)
    _prov_slots(op, path, out)


def text_slots(obj: Any, shape: str) -> List[Slot]:
    """The free-text slots of ``obj``: ``(container, key, role, path)``. ``shape`` is ``op``, ``node``, ``edge``,
    ``draft`` (a proposal draft: its summary and ops) or ``proposal`` (also the review's edits and reason)."""
    out: List[Slot] = []
    if not isinstance(obj, dict):
        return out
    if shape == "op":
        _op_slots(obj, (), out)
    elif shape == "node":
        for key, role in (("name", LINE), ("aliases", LINE), ("attrs", LINE), ("summary", BLOCK)):
            _at(obj, key, role, (), out)
        for i, gap in enumerate(obj.get("gaps") or [] if isinstance(obj.get("gaps"), list) else []):
            _at(gap, "note", BLOCK, ("gaps", i), out)
        _prov_slots(obj, (), out)
    elif shape == "edge":
        for key in ("note", "key"):
            _at(obj, key, LINE, (), out)
        _prov_slots(obj, (), out)
    elif shape in ("draft", "proposal"):
        _at(obj, "summary", BLOCK, (), out)
        for i, op in enumerate(obj.get("ops") or [] if isinstance(obj.get("ops"), list) else []):
            _op_slots(op, ("ops", i), out)
        review = obj.get("review") if shape == "proposal" else None
        if isinstance(review, dict):
            _at(review, "reason", BLOCK, ("review",), out)
            edits = review.get("edits")
            for key in sorted(edits) if isinstance(edits, dict) else []:
                _op_slots(edits[key], ("review", "edits", key), out)
    return out


def control_problem(text: str, role: str) -> Optional[str]:
    """Why ``text`` may not stand in a slot of ``role``, or None: control characters and lone surrogates never,
    invisible formatting characters (``_FORMAT_RE``) never outside a quote, and tabs or line breaks not in a
    one-line slot. The character is named, never echoed."""
    half = _SURROGATE_RE.search(text)
    if half is not None:
        return "holds U+%04X, half of a character (a cut emoji, or text in another encoding); write whole characters" \
            % ord(half.group(0))
    found = _CONTROL_RE.search(text)
    if found is None and role == LINE:
        found = _LINE_BREAK_RE.search(text)
    if found is None and role != QUOTE:
        shown = _FORMAT_RE.search(text)
        if shown is not None:
            return ("holds the invisible formatting character U+%04X (a direction override, a direction mark or a "
                    "zero-width character), which makes the text display other than it reads; write plain text"
                    % ord(shown.group(0)))
    if found is None:
        return None
    char = found.group(0)
    if role == LINE and char in "\t\n\r":
        return "holds a tab or line break (U+%04X); names, aliases, values, notes and keys are one line" % ord(char)
    return "holds the control character U+%04X; write plain text" % ord(char)


def control_problems(obj: Any, shape: str) -> List[str]:
    """``'$.path: message'`` for every free-text slot of ``obj`` holding a control character (see
    ``control_problem``)."""
    out = []
    for container, key, role, path in text_slots(obj, shape):
        message = control_problem(container[key], role)
        if message:
            out.append("%s: %s" % (schema_lite.json_path(path), message))
    return out
