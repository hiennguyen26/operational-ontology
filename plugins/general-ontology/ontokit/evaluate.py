"""Extraction evaluation: precision and recall of a proposal's ``add_node`` and ``add_edge`` ops against a gold file.

A gold file is JSON:

```json
{"title": "Handbook excerpt",
 "nodes": [{"kind": "role", "name": "Bed steward", "ref": "$steward"}, {"kind": "term", "name": "Mulch"}],
 "edges": [{"src": "$steward", "rel": "works_on", "dst": "process:watering"}]}
```

- ``nodes`` items take ``kind``, ``name`` and an optional ``ref`` (a ``$handle`` edges may use, unique in the file)
  and ``note``.
- ``edges`` items take ``src``, ``rel``, ``dst`` and an optional ``note``. An endpoint is a ``$ref`` of a gold node,
  an id (``crop:tomato`` or ``garden/crop:tomato``) or an object ``{kind, name}``.
- ``title``, ``note`` and ``source`` are allowed at the top level. Any other key, a ``ref`` without ``$``, a ``ref``
  used twice, or an endpoint naming a ``$handle`` no gold node defines is refused, so typos surface.

Matching is by key: a node is ``kind:name_key(name)`` and an edge is ``src key -rel-> dst key``. A ``$ref`` keys as
the node it names in the same file. An id endpoint keys as the node it names: a node the proposal adds under that
id, else a node of the loaded graph (``resolve``; an imported node keys as ``ns/kind:name key``), else a node of the
same file whose name slugs to that id; failing all three, as its kind plus the ``name_key`` of its slug
(``role:bed-steward`` is ``role:bed steward``). Endpoints of a symmetric relation are unordered. Matching counts
repeats (a key proposed twice and in the gold once matches once), so duplicates lower precision.

Scores: precision = matched / proposed, recall = matched / gold, f1 = 2 * matched / (proposed + gold) (their
harmonic mean, computed exactly), each rounded to 4 places. With nothing proposed, precision is 1 when the gold is
empty too, else 0; with an empty gold, recall is 1; with both empty, f1 is 1.
"""

from __future__ import annotations

import os
from collections import Counter
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import ids, pipeline, render, store, util
from .errors import UsageError

GOLD_KEYS = ("nodes", "edges", "title", "note", "source")
NODE_KEYS = ("kind", "name", "ref", "note")
EDGE_KEYS = ("src", "rel", "dst", "note")
MAX_LIST = 10


# keys ----------------------------------------------------------------------------------------------------------
def node_key(kind: Any, name: Any) -> str:
    """``kind:name key``, for example ``role:bed steward``."""
    return "%s:%s" % (str(kind or "?").strip(), util.name_key(str(name or "")))


def endpoint_key(ref: Any, handles: Dict[str, str], lookup: Optional[Callable[[str], Optional[str]]] = None) -> str:
    """The key of an edge endpoint: a ``$ref`` (through ``handles``), an id (through ``lookup`` when it knows the
    node, else by its slug), a source id or ``{kind, name}``."""
    if isinstance(ref, dict):
        return node_key(ref.get("kind"), ref.get("name"))
    text = str(ref or "").strip()
    if text.startswith("$"):
        return handles.get(text, text)
    if text.startswith("self/"):
        text = text[5:]
    if ids.SOURCE_RE.match(text):
        return text
    found = lookup(text) if lookup is not None else None
    if found:
        return found
    kind, sep, rest = text.partition(":")
    if not sep:
        return node_key("?", text)
    return node_key(kind, rest)


def graph_resolver(onto: Any) -> Callable[[str], Optional[str]]:
    """``id -> key`` for the nodes of a loaded graph: ``kind:name key``, or ``ns/kind:name key`` for an imported
    node; None for an id the graph does not hold."""

    def resolve(text: str) -> Optional[str]:
        try:
            node = onto.node(text)
        except Exception:  # an odd id is simply not in the graph
            return None
        if not isinstance(node, dict) or not node.get("name") or not node.get("kind"):
            return None
        key = node_key(node.get("kind"), node.get("name"))
        ns = onto.ns_of(text)
        return key if ns in (None, "", "self") else "%s/%s" % (ns, key)

    return resolve


def edge_key(src: str, rel: Any, dst: str, symmetric: Iterable[str] = ()) -> str:
    """``src key -rel-> dst key``; a symmetric relation sorts its endpoints."""
    rel = str(rel or "?")
    if rel in set(symmetric) and dst < src:
        src, dst = dst, src
    return "%s -%s-> %s" % (src, rel, dst)


Resolver = Optional[Callable[[str], Optional[str]]]


def _items(nodes: Iterable[Dict[str, Any]], edges: Iterable[Dict[str, Any]], ref_field: str,
           symmetric: Iterable[str], resolve: Resolver = None) -> Tuple[List[str], List[str]]:
    handles: Dict[str, str] = {}
    explicit: Dict[str, str] = {}  # ids given to the file's own nodes (node.id, or the kit's assigned id)
    slugged: Dict[str, str] = {}  # kind:slug of each of the file's own nodes, the id the kit would give it
    node_keys: List[str] = []
    for node in nodes:
        key = node_key(node.get("kind"), node.get("name"))
        node_keys.append(key)
        ref = node.get(ref_field)
        if isinstance(ref, str) and ref.startswith("$"):
            handles.setdefault(ref, key)
        nid = node.get("_id") or node.get("id")
        if isinstance(nid, str) and nid:
            explicit.setdefault(nid[5:] if nid.startswith("self/") else nid, key)
        if node.get("kind") and node.get("name"):
            slugged.setdefault("%s:%s" % (str(node["kind"]).strip(), util.slugify(str(node["name"]))), key)

    def lookup(text: str) -> Optional[str]:
        if text in explicit:
            return explicit[text]
        found = resolve(text) if resolve is not None else None
        return found or slugged.get(text)

    sym = set(symmetric)
    edge_keys = [edge_key(endpoint_key(e.get("src"), handles, lookup), e.get("rel"),
                          endpoint_key(e.get("dst"), handles, lookup), sym)
                 for e in edges]
    return node_keys, edge_keys


def gold_items(gold: Dict[str, Any], symmetric: Iterable[str] = (), resolve: Resolver = None
               ) -> Tuple[List[str], List[str]]:
    """The node and edge keys of a gold extraction."""
    return _items(gold.get("nodes") or [], gold.get("edges") or [], "ref", symmetric, resolve)


def proposal_items(proposal: Dict[str, Any], symmetric: Iterable[str] = (), resolve: Resolver = None
                   ) -> Tuple[List[str], List[str]]:
    """The node and edge keys of a proposal (or a draft): its ``add_node`` and ``add_edge`` ops."""
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    for op in proposal.get("ops") or []:
        if not isinstance(op, dict):
            continue
        if op.get("op") == "add_node" and isinstance(op.get("node"), dict):
            assigned = (op.get("annot") or {}).get("assigned_id") if isinstance(op.get("annot"), dict) else None
            nodes.append(dict(op["node"], _ref=op.get("ref"), _id=assigned or op["node"].get("id")))
        elif op.get("op") == "add_edge" and isinstance(op.get("edge"), dict):
            edges.append(op["edge"])
    return _items(nodes, edges, "_ref", symmetric, resolve)


# scoring -------------------------------------------------------------------------------------------------------
def _ratio(num: int, den: int, empty: float) -> float:
    return round(float(num) / float(den), 4) if den else empty


def _prf(gold: List[str], proposed: List[str]) -> Tuple[Dict[str, Any], List[str], List[str], List[str]]:
    left = Counter(gold)
    matched: List[str] = []
    extra: List[str] = []
    for key in proposed:
        if left[key] > 0:
            left[key] -= 1
            matched.append(key)
        else:
            extra.append(key)
    missed: List[str] = []
    for key in gold:
        if left[key] > 0:
            left[key] -= 1
            missed.append(key)
    precision = _ratio(len(matched), len(proposed), 1.0 if not gold else 0.0)
    recall = _ratio(len(matched), len(gold), 1.0)
    total = len(proposed) + len(gold)
    f1 = round(2.0 * len(matched) / total, 4) if total else 1.0
    stats = {"precision": precision, "recall": recall, "f1": f1, "gold": len(gold), "proposed": len(proposed),
             "matched": len(matched)}
    return stats, sorted(matched), sorted(missed), sorted(extra)


def score(gold: Dict[str, Any], proposal: Dict[str, Any], symmetric: Iterable[str] = (),
          resolve: Resolver = None) -> Dict[str, Any]:
    """``{nodes: {precision, recall, f1, gold, proposed, matched}, edges: {...}, matched, missed, extra}`` where
    ``matched``, ``missed`` and ``extra`` are ``{nodes: [keys], edges: [keys]}``. ``symmetric`` names relations
    whose endpoints are unordered; ``resolve`` (``graph_resolver``) keys id endpoints by the name of the node they
    name in the loaded graph."""
    sym = sorted(set(symmetric))
    gold_nodes, gold_edges = gold_items(gold, sym, resolve)
    prop_nodes, prop_edges = proposal_items(proposal, sym, resolve)
    node_stats, node_hit, node_miss, node_extra = _prf(gold_nodes, prop_nodes)
    edge_stats, edge_hit, edge_miss, edge_extra = _prf(gold_edges, prop_edges)
    return {
        "nodes": node_stats,
        "edges": edge_stats,
        "matched": {"nodes": node_hit, "edges": edge_hit},
        "missed": {"nodes": node_miss, "edges": edge_miss},
        "extra": {"nodes": node_extra, "edges": edge_extra},
    }


def check_gold(gold: Any) -> Dict[str, Any]:
    """The gold object, or ``UsageError`` naming the first item that does not fit the gold format."""
    if not isinstance(gold, dict):
        raise UsageError("gold: expected a JSON object with nodes and edges")
    unknown = sorted(k for k in gold if k not in GOLD_KEYS)
    if unknown:
        raise UsageError("gold: unknown key%s %s; allowed: %s" % ("" if len(unknown) == 1 else "s",
                                                                 ", ".join(unknown), ", ".join(GOLD_KEYS)))
    for field, keys, required in (("nodes", NODE_KEYS, ("kind", "name")), ("edges", EDGE_KEYS, ("src", "rel", "dst"))):
        items = gold.get(field, [])
        if not isinstance(items, list):
            raise UsageError("gold: %s must be a list" % field)
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise UsageError("gold: %s[%d] must be an object" % (field, i))
            extra = sorted(k for k in item if k not in keys)
            if extra:
                raise UsageError("gold: %s[%d] has unknown key%s %s; allowed: %s" % (
                    field, i, "" if len(extra) == 1 else "s", ", ".join(extra), ", ".join(keys)))
            missing = [k for k in required if item.get(k) in (None, "")]
            if missing:
                raise UsageError("gold: %s[%d] needs %s" % (field, i, ", ".join(missing)))
    refs: Dict[str, int] = {}
    for i, node in enumerate(gold.get("nodes") or []):
        ref = node.get("ref")
        if ref is None:
            continue
        if not isinstance(ref, str) or not ref.startswith("$") or len(ref) < 2:
            raise UsageError("gold: nodes[%d] ref %r must start with $ (for example $%s)"
                             % (i, ref, str(ref).lstrip("$") or "name"))
        if ref in refs:
            raise UsageError("gold: nodes[%d] ref %s is already the ref of nodes[%d]; each ref names one node"
                             % (i, ref, refs[ref]))
        refs[ref] = i
    for i, edge in enumerate(gold.get("edges") or []):
        if not isinstance(edge.get("rel"), str):
            raise UsageError("gold: edges[%d] rel must be a relation name" % i)
        for end in ("src", "dst"):
            value = edge.get(end)
            if isinstance(value, dict):
                extra = sorted(k for k in value if k not in ("kind", "name"))
                if extra or not all(isinstance(value.get(k), str) and value[k].strip() for k in ("kind", "name")):
                    raise UsageError("gold: edges[%d] %s must be {kind, name} with both set%s"
                                     % (i, end, "; unknown key %s" % ", ".join(extra) if extra else ""))
            elif not isinstance(value, str) or not value.strip():
                raise UsageError("gold: edges[%d] %s must be a $ref, an id or {kind, name}" % (i, end))
            elif value.strip().startswith("$") and value.strip() not in refs:
                raise UsageError("gold: edges[%d] %s %s is not the ref of any gold node (refs: %s)"
                                 % (i, end, value.strip(), ", ".join(sorted(refs)) or "none"))
    return gold


# the command ---------------------------------------------------------------------------------------------------
def _path(ctx: Any, value: str) -> str:
    path = os.path.expanduser(value)
    if not os.path.isabs(path) and getattr(ctx, "cwd", None):
        path = os.path.join(ctx.cwd, path)
    return store.guard_input_path(path, None, allow_any=True)


def _read(ctx: Any, value: str, what: str) -> Any:
    path = _path(ctx, value)
    if os.path.isdir(path):
        raise UsageError("%s: %s is a folder; give a JSON file" % (what, value))
    return store.read_json(path)


def _resolver(ctx: Any) -> Resolver:
    try:
        return graph_resolver(ctx.onto())
    except Exception:  # the graph only refines id endpoints; scoring works without it
        return None


def _symmetric(ctx: Any) -> List[str]:
    try:
        registry = ctx.onto().registry
    except Exception:  # the relation list only refines matching; scoring works without it
        return []
    return sorted(r for r in registry.relations() if registry.is_symmetric(r))


def cmd_eval(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """Score a proposal (an id, or a JSON file holding a proposal or a draft) against a gold extraction file."""
    gold = check_gold(_read(ctx, str(args.get("gold") or ""), "gold"))
    ref = str(args.get("proposal") or "").strip()
    if ids.PROP_RE.match(ref):
        proposal = pipeline.load(ctx.repo, ref)
    else:
        proposal = _read(ctx, ref, "proposal")
    if not isinstance(proposal, dict) or not isinstance(proposal.get("ops"), list):
        raise UsageError("proposal: expected a proposal id or a JSON file with an ops list")
    result = score(gold, proposal, _symmetric(ctx), _resolver(ctx))
    result["gold"] = str(args.get("gold"))
    result["proposal"] = proposal.get("id") or os.path.basename(ref)
    return result


def _keys_line(label: str, keys: List[str], cap: Optional[int]) -> List[str]:
    if not keys:
        return []
    shown = keys if cap is None else keys[:cap]
    extra = render.more(len(keys), len(shown))
    return ["%s: %s%s" % (label, "; ".join(shown), "; " + extra if extra else "")]


def render_eval(result: Dict[str, Any], mode: str, ctx: Any) -> List[str]:
    cap = None if mode == "text" else MAX_LIST
    lines = ["eval %s against %s" % (result.get("proposal"), os.path.basename(str(result.get("gold") or "")))]
    for part in ("nodes", "edges"):
        s = result.get(part) or {}
        lines.append("%s: precision %s, recall %s, f1 %s (%s of %s proposed matched; %s in the gold)" % (
            part, render.fmt(s.get("precision")), render.fmt(s.get("recall")), render.fmt(s.get("f1")),
            s.get("matched"), s.get("proposed"), s.get("gold")))
    for label, key in (("missed", "missed"), ("extra", "extra")):
        found = result.get(key) or {}
        lines += _keys_line("%s nodes" % label, list(found.get("nodes") or []), cap)
        lines += _keys_line("%s edges" % label, list(found.get("edges") or []), cap)
    return lines
