"""A small JSON Schema validator (standard library only, Python 3.9+) for the kit's records and packs.

It covers the keywords ``schema/records.schema.json``, ``schema/pack.schema.json`` and pack field schemas use,
with draft 2020-12 semantics: ``type`` (a name or a list, ``null`` included), ``required``, ``properties``,
``additionalProperties`` (false or a schema), ``propertyNames``, ``enum``, ``const``, ``pattern`` (``re.search``,
as jsonschema does, with ECMA-262's ``$``: the end of the text, never before a final newline), ``minLength``,
``maxLength``, ``minItems``, ``maxItems``, ``uniqueItems``, ``minimum``,
``maximum``, ``items``, ``$ref`` into ``#/$defs/...``, ``allOf``, ``anyOf``, ``oneOf``, ``not`` and
``if``/``then``/``else``. Annotations (``description``, ``title``, ``$schema``, ``$id``, ``$comment``,
``default``, ``examples``, ``format``) are ignored. Any other keyword raises ``SchemaUnsupported``, so a schema
the kit cannot check fails loudly instead of passing records unchecked.

Errors are strings ``"<json path>: <message>"`` with jsonschema's path style (``$``, ``$.a.b``, ``$.a[0]``) and
one error per failing keyword, as jsonschema reports them (one per missing required property).

    errors = validate(record, "node", schema)     # [] when valid
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, Iterator, List, Sequence, Tuple, Union

REPR_MAX = 80  # characters of a value shown in a message
ENUM_MAX = 400  # characters of the accepted values listed in an enum message

ANNOTATIONS = frozenset(
    ("$schema", "$id", "$comment", "title", "description", "default", "examples", "format", "$defs")
)
SUPPORTED = frozenset(
    (
        "type",
        "required",
        "properties",
        "additionalProperties",
        "enum",
        "const",
        "pattern",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "items",
        "$ref",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "uniqueItems",
        "propertyNames",
        "if",
        "then",
        "else",
    )
)

Path = Tuple[Union[str, int], ...]

_PATTERN_CACHE: Dict[str, "re.Pattern[str]"] = {}


class SchemaUnsupported(ValueError):
    """The schema uses a keyword or a $ref this validator does not implement."""


def validate(instance: Any, def_name: str, schema: Dict[str, Any]) -> List[str]:
    """Errors for ``instance`` against ``schema["$defs"][def_name]``, as ``"<json path>: <message>"`` strings."""
    defs = schema.get("$defs") or {}
    if def_name not in defs:
        raise SchemaUnsupported("no $defs/%s in the schema" % def_name)
    return ["%s: %s" % (json_path(path), message) for path, message in iter_errors(instance, defs[def_name], schema)]


def is_valid(instance: Any, def_name: str, schema: Dict[str, Any]) -> bool:
    return not validate(instance, def_name, schema)


def json_path(path: Sequence[Union[str, int]]) -> str:
    out = "$"
    for elem in path:
        out += "[%d]" % elem if isinstance(elem, int) else "." + elem
    return out


def iter_errors(instance: Any, subschema: Any, root: Dict[str, Any], path: Path = ()) -> Iterator[Tuple[Path, str]]:
    """(path, message) for every failing keyword, recursing into properties, items, $ref and the combinators."""
    if subschema is True or subschema == {}:
        return
    if subschema is False:
        yield path, "False schema does not allow %s" % _repr(instance)
        return
    if not isinstance(subschema, dict):
        raise SchemaUnsupported("a schema must be an object or a boolean, got %r" % (subschema,))
    unknown = [k for k in subschema if k not in SUPPORTED and k not in ANNOTATIONS]
    if unknown:
        raise SchemaUnsupported("unsupported keyword(s): %s" % ", ".join(sorted(unknown)))

    if "$ref" in subschema:
        yield from iter_errors(instance, _resolve(subschema["$ref"], root), root, path)

    if "type" in subschema:
        types = subschema["type"]
        names = [types] if isinstance(types, str) else list(types)
        if not any(is_type(instance, name) for name in names):
            yield path, "%s is not of type %s" % (_repr(instance), ", ".join(repr(n) for n in names))

    if "enum" in subschema and not any(_equal(instance, v) for v in subschema["enum"]):
        yield path, "%s is not one of %s" % (_repr(instance), _repr(subschema["enum"], ENUM_MAX))

    if "const" in subschema and not _equal(instance, subschema["const"]):
        yield path, "%s was expected" % _repr(subschema["const"])

    if isinstance(instance, str):
        if "minLength" in subschema and len(instance) < subschema["minLength"]:
            yield path, "%s is too short" % _repr(instance)
        if "maxLength" in subschema and len(instance) > subschema["maxLength"]:
            yield path, "%s is too long" % _repr(instance)
        if "pattern" in subschema and not _pattern(subschema["pattern"]).search(instance):
            yield path, "%s does not match %r" % (_repr(instance), subschema["pattern"])

    if is_type(instance, "number"):
        if "minimum" in subschema and instance < subschema["minimum"]:
            yield path, "%s is less than the minimum of %s" % (_repr(instance), subschema["minimum"])
        if "maximum" in subschema and instance > subschema["maximum"]:
            yield path, "%s is greater than the maximum of %s" % (_repr(instance), subschema["maximum"])

    if isinstance(instance, list):
        if "minItems" in subschema and len(instance) < subschema["minItems"]:
            word = "should be non-empty" if subschema["minItems"] == 1 else "is too short"
            yield path, "%s %s" % (_repr(instance), word)
        if "maxItems" in subschema and len(instance) > subschema["maxItems"]:
            yield path, "%s is too long" % _repr(instance)
        if subschema.get("uniqueItems") is True:
            for index, item in enumerate(instance):
                if any(_equal(item, other) for other in instance[:index]):
                    yield path, "%s has non-unique elements" % _repr(instance)
                    break
        if "items" in subschema:
            for index, item in enumerate(instance):
                yield from iter_errors(item, subschema["items"], root, path + (index,))

    if isinstance(instance, dict):
        props = subschema.get("properties") or {}
        for name in subschema.get("required") or []:
            if name not in instance:
                yield path, "%r is a required property" % name
        for name, value in instance.items():
            if name in props:
                yield from iter_errors(value, props[name], root, path + (name,))
        if "propertyNames" in subschema:
            for name in instance:
                for _sub, message in iter_errors(name, subschema["propertyNames"], root, path):
                    yield path, "property name %s" % message
        if "additionalProperties" in subschema:
            extra_schema = subschema["additionalProperties"]
            extras = [name for name in instance if name not in props]
            if extra_schema is False and extras:
                yield path, "Additional properties are not allowed (%s %s unexpected)" % (
                    ", ".join(repr(e) for e in extras),
                    "was" if len(extras) == 1 else "were",
                )
            elif extra_schema is not False:
                for name in extras:
                    yield from iter_errors(instance[name], extra_schema, root, path + (name,))

    for part in subschema.get("allOf") or []:
        yield from iter_errors(instance, part, root, path)

    if "anyOf" in subschema:
        if not any(_passes(instance, part, root, path) for part in subschema["anyOf"]):
            yield path, "%s is not valid under any of the given schemas" % _repr(instance)

    if "oneOf" in subschema:
        passing = sum(1 for part in subschema["oneOf"] if _passes(instance, part, root, path))
        if passing == 0:
            yield path, "%s is not valid under any of the given schemas" % _repr(instance)
        elif passing > 1:
            yield path, "%s is valid under each of %d of the given schemas" % (_repr(instance), passing)

    if "not" in subschema and _passes(instance, subschema["not"], root, path):
        yield path, "%s should not be valid under %s" % (_repr(instance), _repr(subschema["not"]))

    if "if" in subschema:
        matched = _passes(instance, subschema["if"], root, path)
        branch = subschema.get("then") if matched else subschema.get("else")
        if branch is not None:
            yield from iter_errors(instance, branch, root, path)


def _passes(instance: Any, subschema: Any, root: Dict[str, Any], path: Path) -> bool:
    """True when ``instance`` has no error under ``subschema`` (unsupported keywords still raise)."""
    return not any(True for _ in iter_errors(instance, subschema, root, path))


def is_type(instance: Any, name: str) -> bool:
    """JSON types. Booleans are not numbers; an integral float (1.0) counts as an integer, as in jsonschema."""
    if name == "null":
        return instance is None
    if name == "boolean":
        return isinstance(instance, bool)
    if name == "string":
        return isinstance(instance, str)
    if name == "object":
        return isinstance(instance, dict)
    if name == "array":
        return isinstance(instance, list)
    if isinstance(instance, bool):
        return False
    if isinstance(instance, float) and not math.isfinite(instance):
        return False  # NaN and Infinity are not JSON numbers (every bound comparison with NaN is False)
    if name == "number":
        return isinstance(instance, (int, float))
    if name == "integer":
        return isinstance(instance, int) or (isinstance(instance, float) and instance.is_integer())
    raise SchemaUnsupported("unknown type %r" % name)


def _resolve(ref: str, root: Dict[str, Any]) -> Any:
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise SchemaUnsupported("only local $ref values (#/...) are supported, got %r" % (ref,))
    node: Any = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            raise SchemaUnsupported("unresolvable $ref %r" % ref)
        node = node[part]
    return node


def ecma_dollar(source: str) -> str:
    """``source`` with every ``$`` anchor (unescaped, outside a character class) written ``\\Z``. In ECMA-262, which
    JSON Schema patterns follow, ``$`` matches only at the end of the text; Python's also matches before a final
    newline, so ``^id$`` would accept ``"id\\n"``."""
    out: List[str] = []
    in_class = False
    i = 0
    while i < len(source):
        ch = source[i]
        if ch == "\\" and i + 1 < len(source):
            out.append(source[i:i + 2])
            i += 2
            continue
        if in_class:
            if ch == "]":
                in_class = False
        elif ch == "[":
            in_class = True
            if source[i + 1:i + 2] == "]" or source[i + 1:i + 3] == "^]":  # a leading ] is a literal
                step = 2 if source[i + 1] == "]" else 3
                out.append(source[i:i + step])
                i += step
                continue
        elif ch == "$":
            out.append(r"\Z")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _pattern(source: str) -> "re.Pattern[str]":
    compiled = _PATTERN_CACHE.get(source)
    if compiled is None:
        compiled = _PATTERN_CACHE[source] = re.compile(ecma_dollar(source))
    return compiled


def _equal(one: Any, two: Any) -> bool:
    """JSON equality: True is not 1, and containers compare element by element."""
    if isinstance(one, bool) or isinstance(two, bool):
        return isinstance(one, bool) and isinstance(two, bool) and one is two
    if isinstance(one, dict) and isinstance(two, dict):
        return one.keys() == two.keys() and all(_equal(one[k], two[k]) for k in one)
    if isinstance(one, list) and isinstance(two, list):
        return len(one) == len(two) and all(_equal(a, b) for a, b in zip(one, two))
    if isinstance(one, (dict, list)) or isinstance(two, (dict, list)):
        return False
    return one == two


def _repr(value: Any, width: int = REPR_MAX) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else repr(value)
    return text if len(text) <= width else text[:width - 3] + "..."
