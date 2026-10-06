"""The in-memory graph: local records, the imports under their namespaces, and the sources as read-only nodes.

``Ontology.load(repo)`` reads ``graph/nodes.jsonl``, ``graph/edges.jsonl``, ``sources/index.jsonl``, the packs and
every vendored import, and is cached by ``store.data_stamp`` (any write to the data gives a fresh load).

- Local ids are bare (``crop:tomato``); import ids are qualified (``garden/crop:tomato``); ``self/`` is dropped.
- Sources are virtual nodes of kind ``source`` (``src-<12hex>``), read-only. The sources an import's export lists
  load the same way under their qualified ids (``garden/src-...``), so an imported edge that ends on a source
  (``derived_from``, ``refresh_with``) links to a node that is there.
- Edges are stored once, in the acting direction, and indexed both ways; inbound edges read with the relation's
  inverse name. An imported edge is keyed by the edge id of its qualified endpoints. Two imports that hold the same
  edge (two parents bundling one grandparent bridge) share that key: an active copy wins over an archived one,
  ``edge_origins`` lists every import holding it, and a status disagreement is a W04 load warning (``warnings``).
  A local edge under an imported edge's key is P06 (``shadowed``): imports are read-only.
- Ids inside imported records are qualified too (``archived.superseded_by``, id-shaped aliases); an imported
  edge's ``superseded_by`` reads under the keys used here.
- Each edge's relation reads with the declaration of its own namespace (``rel_ns``): two imports may declare one
  relation name differently.
- A *bridge* is an edge whose endpoints sit in different namespaces (``self`` counts as one); it is computed here
  and never stored.
- ``same_as`` edges form equivalence classes (union-find over active edges); traversal treats a class as one node.
- Load problems (unreadable lines, duplicate ids) are kept in ``problems``, never dropped silently: the first record
  with an id wins and later ones are reported. A local record whose aliases, gaps, provenance, attrs, name, summary,
  note or archive block holds another type (a hand edit) reads with that field empty and is P02 (``_shaped``), so
  no reader trips on it; ``mutate.refuse_damaged`` keeps any rewrite of the file from writing the empty field.

``Ontology.from_rows`` builds the same object from rows in memory, so a writer can check a result before it
writes it (``validate.check_graph``).
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

from . import ids as idmod
from . import lockfile, packs, store, util
from .errors import DataError, NotFound, Problem

NODES = "graph/nodes.jsonl"
EDGES = "graph/edges.jsonl"
SOURCES = "sources/index.jsonl"
MAX_CANDIDATES = 20
_CACHE: Dict[Tuple[str, bool], Tuple[Tuple[Any, ...], "Ontology"]] = {}


def _qualified_node(ns: str, row: Dict[str, Any]) -> Dict[str, Any]:
    """An imported node with the ids it holds qualified by ``ns`` (C.2): ``archived.superseded_by`` and id-shaped
    aliases (a merge keeps the dropped id as an alias), so a replacement reads as ``garden/crop:x`` and never as a
    bare id that means a local record here. Ids already qualified (a bundled parent's) stay as they are."""

    def q(value: Any) -> Any:
        return idmod.qualify(ns, value) if isinstance(value, str) and idmod.LOCAL_RE.match(value) else value

    block = row.get("archived")
    if isinstance(block, dict) and isinstance(block.get("superseded_by"), list):
        row["archived"] = dict(block, superseded_by=[q(s) for s in block["superseded_by"]])
    if isinstance(row.get("aliases"), list):
        row["aliases"] = [q(a) for a in row["aliases"]]
    return row


# the fields every reader walks or indexes: a hand edit that gives one another type would crash them all
NODE_SHAPE = (("aliases", list), ("gaps", list), ("prov", list), ("attrs", dict), ("name", str), ("summary", str))
EDGE_SHAPE = (("prov", list), ("note", str))
SHAPE_NOTE = "it reads as empty until the line is fixed by hand, and nothing rewrites the file meanwhile"
_EMPTY = {list: list, dict: dict, str: str}
_TYPE_NAMES = {list: "a list", dict: "an object", str: "text", int: "a number", float: "a number", bool: "true or false"}


def _shaped(row: Dict[str, Any], shape: Sequence[Tuple[str, type]]) -> Tuple[Dict[str, Any], List[str]]:
    """``(row, fields)``: a copy of ``row`` with each field of ``shape`` that holds another type (null counts as
    missing) read as empty, and the names of those fields. The file keeps the line as it is (P02 at load)."""
    bad = [name for name, want in tuple(shape) + (("archived", dict),)
           if row.get(name) is not None and not isinstance(row.get(name), want)]
    if not bad:
        return row, []
    fixed = dict(row)
    for name, want in tuple(shape) + (("archived", dict),):
        if name in bad:
            fixed[name] = None if name == "archived" else _EMPTY[want]()
    return fixed, bad


def trunc(text: Any, width: int = 110) -> str:
    """One line of at most ``width`` characters, cut with ``...``."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= width else flat[: width - 3].rstrip() + "..."


class Ontology(object):
    """Read-only query model of one topic. Build it with ``Ontology.load`` (or ``from_rows``)."""

    def __init__(self) -> None:
        self.repo: Optional[store.Repo] = None
        self.ns = ""
        self.manifest: Dict[str, Any] = {}
        self.registry: packs.Registry = packs.Registry()
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.edges: Dict[str, Dict[str, Any]] = {}
        self.out: Dict[str, List[str]] = {}
        self.inc: Dict[str, List[str]] = {}
        self.prov_index: Dict[str, List[Tuple[str, str]]] = {}
        self.same_as: Dict[str, str] = {}
        self.classes: Dict[str, List[str]] = {}
        self.imports: List[Dict[str, Any]] = []
        self.exports: Dict[str, Dict[str, Any]] = {}
        self.sources: Dict[str, Dict[str, Any]] = {}
        self.local_ids: List[str] = []  # stored local node ids, file order
        self.local_edge_ids: List[str] = []
        self.virtual: Set[str] = set()  # source nodes
        self.bridges: Set[str] = set()
        self.lines: Dict[str, Tuple[str, int]] = {}  # local record id -> (file, line)
        self.edge_origin: Dict[str, Tuple[str, str]] = {}  # imported edge key -> (ns, upstream edge id) of the copy kept
        self.edge_origins: Dict[str, List[Tuple[str, str]]] = {}  # imported edge key -> every import that holds it
        self.shadowed: Set[str] = set()  # local edge ids an import also holds (P06: the local copy hides it)
        self.problems: List[Problem] = []
        self.warnings: List[Problem] = []  # load warnings (imports that disagree about one edge)
        self._origin_status: Dict[Tuple[str, Tuple[str, str]], str] = {}
        self._disagree: Dict[str, Problem] = {}
        self.stats: Dict[str, Any] = {}
        self.include_imports = True
        self._aliases: Dict[str, List[str]] = {}
        self._kind_keys: Dict[str, Optional[str]] = {}
        self._cache: Dict[str, Any] = {}  # per-object caches (needs, entities index)
        self._frozen = False  # set once ``from_rows`` has built every record; ``edges_of`` caches only then

    # loading -----------------------------------------------------------------------------------------------
    @classmethod
    def load(cls, repo: Union[store.Repo, str], include_imports: bool = True) -> "Ontology":
        """The graph of ``repo``, cached by ``store.data_stamp``."""
        root = repo.root if isinstance(repo, store.Repo) else os.path.abspath(repo)
        key = (os.path.realpath(root), bool(include_imports))
        stamp = store.data_stamp(root)
        hit = _CACHE.get(key)
        if hit and hit[0] == stamp:
            return hit[1]
        started = time.perf_counter()
        repo_obj = store.Repo.open(root)
        node_rows, node_problems = store.read_jsonl_lines(repo_obj.path(NODES))
        edge_rows, edge_problems = store.read_jsonl_lines(repo_obj.path(EDGES))
        source_rows, source_problems = store.read_jsonl_lines(repo_obj.path(SOURCES))
        problems = [Problem("P01", NODES, n, m) for n, m in node_problems]
        problems += [Problem("P01", EDGES, n, m) for n, m in edge_problems]
        problems += [Problem("P01", SOURCES, n, m) for n, m in source_problems]
        lock = lockfile.empty()
        exports: Dict[str, Dict[str, Any]] = {}
        try:
            lock = lockfile.read(repo_obj)
        except DataError as exc:
            problems.append(Problem("P01", lockfile.LOCK, 0, str(exc)))
        if include_imports:
            for e in lockfile.entries(lock):
                ns = e.get("ns")
                if not isinstance(ns, str) or not ns:
                    continue
                try:
                    value = lockfile.read_export(repo_obj, ns)
                except DataError as exc:
                    problems.append(Problem("P15", lockfile.EXPORT % ns, 0, str(exc)))
                    continue
                if value is not None:
                    exports[ns] = value
        parse_s = time.perf_counter() - started
        onto = cls.from_rows(
            repo_obj, repo_obj.manifest, node_rows, edge_rows, source_rows, lock, exports,
            include_imports=include_imports, numbered=True,
        )
        onto.problems = problems + onto.problems
        onto.stats["parse_s"] = round(parse_s, 4)
        _CACHE[key] = (stamp, onto)
        return onto

    @classmethod
    def from_rows(
        cls,
        repo: Optional[store.Repo],
        manifest: Dict[str, Any],
        nodes: Sequence[Any],
        edges: Sequence[Any],
        sources: Sequence[Any] = (),
        lock: Optional[Dict[str, Any]] = None,
        exports: Optional[Dict[str, Dict[str, Any]]] = None,
        registry: Optional[packs.Registry] = None,
        include_imports: bool = True,
        numbered: bool = False,
    ) -> "Ontology":
        """A graph from rows in memory. With ``numbered`` each row is ``(line, record)`` as read from a file;
        otherwise the line is the row's position in its sorted file. ``registry`` defaults to the packs of
        ``manifest`` (the local pack is read from ``repo``) plus the imported packs."""
        started = time.perf_counter()
        onto = cls()
        onto.repo = repo
        onto.manifest = dict(manifest or {})
        onto.ns = str(onto.manifest.get("ns") or "")
        onto.include_imports = include_imports
        lock = lock if isinstance(lock, dict) else lockfile.empty()
        onto.imports = [e for e in lockfile.entries(lock) if isinstance(e.get("ns"), str)]
        onto.exports = dict(exports or {}) if include_imports else {}
        if registry is None:
            extra: List[Tuple[str, Dict[str, Any]]] = []
            for ns in sorted(onto.exports):
                for name, block in sorted(((onto.exports[ns].get("meta") or {}).get("packs") or {}).items()):
                    if isinstance(block, dict) and isinstance(block.get("pack"), dict):
                        extra.append((ns, block["pack"]))
            registry = packs.load(repo, onto.manifest, extra)
        onto.registry = registry
        onto.warnings = registry.warnings()  # W04: two imports declare one relation name differently

        def numbered_rows(rows: Sequence[Any], file: str) -> List[Tuple[int, Dict[str, Any]]]:
            if numbered:
                return [(n, r) for n, r in rows]
            ordered = sorted((r for r in rows if isinstance(r, dict)), key=lambda r: str(r.get("id") or ""))
            return list(enumerate(ordered, start=1))

        onto._add_sources(numbered_rows(sources, SOURCES))
        onto._add_local_nodes(numbered_rows(nodes, NODES))
        onto._add_local_edges(numbered_rows(edges, EDGES))
        if include_imports:
            onto._add_imports()
        onto._build_same_as()
        onto.stats = {
            "parse_s": 0.0,
            "build_s": round(time.perf_counter() - started, 4),
            "nodes": len(onto.local_ids),
            "edges": len(onto.local_edge_ids),
            "sources": len(onto.sources),
            "imports": len(onto.imports),
        }
        onto._frozen = True
        return onto

    def _dup(self, file: str, line: int, rid: str, row: Dict[str, Any], first: Dict[str, Any]) -> None:
        if util.canonical_line(row) == util.canonical_line(first):
            self.problems.append(Problem("P05", file, line, "duplicate line: %s appears twice" % rid))
        elif file == SOURCES and isinstance(row.get("sha256"), str) and row.get("sha256") == first.get("sha256"):
            where = self.lines.get(rid, (file, 0))
            self.problems.append(Problem("P06", file, line, "%s is also on %s:%d with the same text (ingested on two "
                                                            "branches, then merged; fix: onto validate --fix keeps "
                                                            "one line)" % (rid, where[0], where[1])))
        else:
            where = self.lines.get(rid, (file, 0))
            self.problems.append(
                Problem("P06", file, line, "id %s is already used on %s:%d with other content" % (rid, where[0], where[1]))
            )

    def _add_sources(self, rows: List[Tuple[int, Dict[str, Any]]]) -> None:
        for line, row in rows:
            sid = row.get("id")
            if not isinstance(sid, str):
                continue
            if sid in self.sources:
                self._dup(SOURCES, line, sid, row, self.sources[sid])
                continue
            self.sources[sid] = row
            self.lines[sid] = (SOURCES, line)
            self.virtual.add(sid)
            self.nodes[sid] = {
                "id": sid,
                "kind": "source",
                "name": str(row.get("title") or sid),
                "summary": "",
                "status": "confirmed",
                "trust": row.get("trust") or "untrusted",
                "conf": 1.0,
                "visibility": "shared",
                "attrs": {"source_kind": row.get("kind"), "url": row.get("url"), "captured_at": row.get("captured_at")},
                "aliases": [],
                "gaps": [],
                "prov": [],
                "archived": None,
            }

    def _add_local_nodes(self, rows: List[Tuple[int, Dict[str, Any]]]) -> None:
        for line, row in rows:
            nid = row.get("id")
            if not isinstance(nid, str) or not nid:
                self.problems.append(Problem("P02", NODES, line, "node without an id"))
                continue
            if nid in self.nodes:
                self._dup(NODES, line, nid, row, self.nodes[nid])
                continue
            row = self._shape(NODES, line, nid, row, NODE_SHAPE)
            self.nodes[nid] = row
            self.lines[nid] = (NODES, line)
            self.local_ids.append(nid)
            self._index_prov(nid, row)

    def _shape(self, file: str, line: int, rid: str, row: Dict[str, Any],
               shape: Sequence[Tuple[str, type]]) -> Dict[str, Any]:
        """``row``, or its copy with the fields of another type read as empty (``_shaped``) and a P02 naming them."""
        fixed, bad = _shaped(row, shape)
        for name in bad:
            want = dict(shape).get(name, dict)
            self.problems.append(Problem("P02", file, line, "%s: %s is %s, not %s; %s" % (
                rid, name, _TYPE_NAMES.get(type(row.get(name)), "a value"), _TYPE_NAMES[want], SHAPE_NOTE)))
        return fixed

    def _index_prov(self, rid: str, row: Dict[str, Any]) -> None:
        prov = row.get("prov")
        for p in prov if isinstance(prov, list) else []:
            if isinstance(p, dict) and isinstance(p.get("src"), str):
                self.prov_index.setdefault(p["src"], []).append((rid, str(p.get("loc") or "")))

    def _link(self, eid: str, edge: Dict[str, Any]) -> None:
        src, dst = edge.get("src"), edge.get("dst")
        if isinstance(src, str):
            self.out.setdefault(src, []).append(eid)
        if isinstance(dst, str):
            self.inc.setdefault(dst, []).append(eid)
        if isinstance(src, str) and isinstance(dst, str) and self.ns_of(src) != self.ns_of(dst):
            self.bridges.add(eid)

    def _add_local_edges(self, rows: List[Tuple[int, Dict[str, Any]]]) -> None:
        for line, row in rows:
            eid = row.get("id")
            if not isinstance(eid, str) or not eid:
                self.problems.append(Problem("P02", EDGES, line, "edge without an id"))
                continue
            if eid in self.edges:
                self._dup(EDGES, line, eid, row, self.edges[eid])
                continue
            row = self._shape(EDGES, line, eid, row, EDGE_SHAPE)
            self.edges[eid] = row
            self.lines[eid] = (EDGES, line)
            self.local_edge_ids.append(eid)
            self._link(eid, row)
            self._index_prov(eid, row)

    def _add_imports(self) -> None:
        for e in sorted(self.imports, key=lambda x: str(x.get("ns"))):
            ns = e["ns"]
            export = self.exports.get(ns)
            if not isinstance(export, dict):
                continue
            file = lockfile.EXPORT % ns
            for row in export.get("nodes") or []:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                    continue
                qid = idmod.qualify(ns, row["id"])
                if qid in self.nodes:
                    self.problems.append(Problem("P06", file, 0, "imported id %s is already loaded" % qid))
                    continue
                self.nodes[qid] = _qualified_node(ns, dict(row, id=qid))
            self._add_import_sources(ns, export)
            upstream: Dict[str, str] = {}  # upstream edge id -> the key it is loaded under here
            for row in export.get("edges") or []:
                if not isinstance(row, dict):
                    continue
                src = idmod.qualify(ns, str(row.get("src") or ""))
                dst = idmod.qualify(ns, str(row.get("dst") or ""))
                if not src or not dst:
                    continue
                key = idmod.edge_id(src, str(row.get("rel") or ""), dst, str(row.get("key") or ""))
                origin = (ns, str(row.get("id") or ""))
                upstream.setdefault(origin[1], key)
                if key in self.edges:
                    if key in self.edge_origin:  # the same edge from two imports (two parents bundle it)
                        self._merge_imported(key, dict(row, id=key, src=src, dst=dst), origin, file)
                    else:  # a local record holds the id: it would hide the upstream record (imports are read-only)
                        where = self.lines.get(key, (EDGES, 0))
                        self.shadowed.add(key)
                        self.problems.append(Problem(
                            "P06", where[0], where[1],
                            "%s (%s -%s-> %s) is also an imported edge of %s: imports are read-only, and this local "
                            "copy hides the upstream record (fix: onto validate --fix drops the local copy)" % (
                                key, src, row.get("rel"), dst, ns)))
                    continue
                self.edges[key] = dict(row, id=key, src=src, dst=dst)
                self.edge_origin[key] = origin
                self.edge_origins[key] = [origin]
                self._link(key, self.edges[key])
            for key, origin in self.edge_origin.items():  # replacements read under the keys used here (C.2)
                row = self.edges[key]
                block = row.get("archived")
                if origin[0] == ns and isinstance(block, dict) and isinstance(block.get("superseded_by"), list):
                    self.edges[key] = dict(row, archived=dict(block, superseded_by=[
                        upstream.get(s, s) if isinstance(s, str) else s for s in block["superseded_by"]]))

    def _add_import_sources(self, ns: str, export: Dict[str, Any]) -> None:
        """The sources an import's export lists, as read-only source nodes under the qualified ids its edges use
        (``<ns>/src-...``): an exported edge may end on a source (``derived_from``, ``refresh_with``, ``about``), and
        without them it would read as a link to a missing node. They are ``virtual`` like local sources; the text
        stays upstream."""
        for row in export.get("sources") or []:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].startswith("src-"):
                continue
            qid = idmod.qualify(ns, row["id"])
            if qid in self.nodes:
                continue
            self.virtual.add(qid)
            self.nodes[qid] = {
                "id": qid,
                "kind": "source",
                "name": str(row.get("title") or row["id"]),
                "summary": "",
                "status": "confirmed",
                "trust": "user" if row.get("kind") == "interview" else "untrusted",
                "conf": 1.0,
                "visibility": "shared",
                "attrs": {"source_kind": row.get("kind"), "url": row.get("url"),
                          "captured_at": row.get("captured_at")},
                "aliases": [],
                "gaps": [],
                "prov": [],
                "archived": None,
            }

    def _merge_imported(self, key: str, row: Dict[str, Any], origin: Tuple[str, str], file: str) -> None:
        """Keep one copy of an edge two imports both hold: an active copy wins over an archived one, whatever the
        namespaces are called; every origin is kept in ``edge_origins``, and a status disagreement is a W04 load
        warning."""
        kept = self.edges[key]
        origins = self.edge_origins.setdefault(key, [self.edge_origin[key]])
        status = self._origin_status
        status.setdefault((key, self.edge_origin[key]), str(kept.get("status")))
        origins.append(origin)
        status[(key, origin)] = str(row.get("status"))
        if kept.get("status") == "archived" and row.get("status") != "archived":
            self.edges[key] = row
            self.edge_origin[key] = origin
        if len({status[(key, o)] for o in origins}) > 1:
            where = ", ".join("%s %s" % (ns, status[(key, (ns, eid))]) for ns, eid in origins)
            self._disagree[key] = Problem("W04", file, 0, "%s -%s-> %s: the imports disagree (%s); the %s copy is "
                                                          "used" % (row.get("src"), row.get("rel"), row.get("dst"),
                                                                    where, self.edges[key].get("status")))
            self.warnings = self.registry.warnings() + [self._disagree[k] for k in sorted(self._disagree)]

    def _build_same_as(self) -> None:
        parent: Dict[str, str] = {}

        def find(x: str) -> str:
            root = x
            while parent.get(root, root) != root:
                root = parent[root]
            while parent.get(x, x) != root:
                parent[x], x = root, parent[x]
            return root

        for eid in sorted(self.edges):
            edge = self.edges[eid]
            if edge.get("rel") != "same_as" or not self.active(eid):
                continue
            a, b = edge.get("src"), edge.get("dst")
            if not isinstance(a, str) or not isinstance(b, str) or a not in self.nodes or b not in self.nodes:
                continue
            if not self.active(a) or not self.active(b):
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
        for members in groups.values():
            ordered = sorted(members)
            cid = ordered[0]
            self.classes[cid] = ordered
            for m in ordered:
                self.same_as[m] = cid

    # lookups -----------------------------------------------------------------------------------------------
    def own_local(self, id: str) -> str:
        """``id`` with a leading ``self/`` or ``<this topic's ns>/`` dropped (the second only when no import uses that
        ns), so ids printed as ``<ns>/<id>`` in a composed topic read as the bare local id."""
        if not isinstance(id, str):
            return id
        if id.startswith("self/"):
            return id[5:]
        own = self.ns
        if own and id.startswith(own + "/") and own not in {e.get("ns") for e in self.imports}:
            rest = id[len(own) + 1:]
            if idmod.LOCAL_RE.match(rest) or rest.startswith(("e:", "src-")):
                return rest
        return id

    def node(self, id: str) -> Optional[Dict[str, Any]]:
        if not isinstance(id, str):
            return None
        return self.nodes.get(self.own_local(id))

    def record(self, id: str) -> Optional[Dict[str, Any]]:
        """A node, a source node or an edge."""
        return self.node(id) or self.edges.get(id)

    def active(self, id: str) -> bool:
        """True for a node or edge that exists and is not archived."""
        rec = self.record(id)
        return rec is not None and rec.get("status") != "archived"

    def is_local(self, id: str) -> bool:
        return id in self.nodes and self.ns_of(id) == "self" and id not in self.virtual

    def is_imported(self, id: str) -> bool:
        return self.ns_of(id) != "self"

    def ns_of(self, id: str) -> str:
        """``self``, or the namespace of a qualified id."""
        ns, _local = idmod.split_ns(id) if isinstance(id, str) else (None, "")
        return ns or "self"

    def kind_of(self, id: str) -> str:
        """The registry key of a node's kind (``crop``, or ``garden/crop`` for an unshared import); ``source`` for a
        source; ``edge`` for an edge id; otherwise the id prefix."""
        if id in self._kind_keys and self._kind_keys[id] is not None:
            return self._kind_keys[id]  # type: ignore
        node = self.node(id)
        ns = self.ns_of(id)
        if node is not None:
            raw = str(node.get("kind") or "")
        elif idmod.record_prefix(id) == "src":
            raw = "source"
        elif idmod.record_prefix(id) == "e":
            return "edge"
        else:
            local = idmod.split_ns(id)[1] if isinstance(id, str) else ""
            raw = local.split(":", 1)[0] if ":" in local else "unknown"
        if raw == "source":
            return raw
        key = self.registry.kind_key(raw, ns if ns != "self" else None)
        if key is None:
            key = raw if ns == "self" else "%s/%s" % (ns, raw)
        if node is not None:
            self._kind_keys[id] = key
        return key

    def edge(self, eid: str) -> Optional[Dict[str, Any]]:
        return self.edges.get(eid)

    def other(self, edge: Dict[str, Any], id: str) -> str:
        return str(edge.get("dst") if edge.get("src") == id else edge.get("src"))

    def rel_ns(self, edge: Union[str, Dict[str, Any]]) -> Optional[str]:
        """The import namespace whose declaration of the edge's relation governs it: the import an imported edge
        comes from, or the one import both ends of a local edge sit in; None for the loaded packs. Two imports may
        declare one relation name differently, and edges keep the bare name."""
        if isinstance(edge, str):
            eid, rec = edge, self.edges.get(edge) or {}
        else:
            eid, rec = str(edge.get("id") or ""), edge
        origin = self.edge_origin.get(eid)
        if origin is not None and eid not in self.lines:
            return origin[0]
        src, dst = rec.get("src"), rec.get("dst")
        if isinstance(src, str) and isinstance(dst, str):
            ns = self.ns_of(src)
            if ns != "self" and ns == self.ns_of(dst):
                return ns
        return None

    def inverse_of(self, edge: Union[str, Dict[str, Any]]) -> str:
        """The name an inbound edge reads with: its relation's inverse as its own namespace declares it."""
        rec = (self.edges.get(edge) or {}) if isinstance(edge, str) else edge
        return self.registry.inverse(str(rec.get("rel") or ""), self.rel_ns(edge))

    def symmetric(self, edge: Union[str, Dict[str, Any]]) -> bool:
        """True when the edge's relation is symmetric as its own namespace declares it."""
        rec = (self.edges.get(edge) or {}) if isinstance(edge, str) else edge
        return self.registry.is_symmetric(str(rec.get("rel") or ""), self.rel_ns(edge))

    def edges_of(self, id: str, direction: str = "both", rels: Optional[Iterable[str]] = None,
                 include_archived: bool = False) -> List[Dict[str, Any]]:
        """Every edge touching ``id`` as ``{label, direction, other, edge}``. Outbound edges read with the relation
        name, inbound ones with its inverse. ``rels`` keeps relation or inverse names. Archived edges, and edges to
        an archived node, are left out unless ``include_archived``."""
        id = self.own_local(id)
        wanted = set(rels) if rels else None
        out: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        ways = [w for w in ("out", "in") if direction in ("both", w)]
        for way in ways:
            for eid, edge, rel, ns, symmetric, label, other, dead in self._edge_rows(id, way):
                if eid in seen:
                    continue
                if symmetric and edge.get("src") == edge.get("dst"):
                    seen.add(eid)
                if not include_archived and dead:
                    continue
                if wanted is not None and rel not in wanted and label not in wanted and not (
                        ns and ("%s/%s" % (ns, rel) in wanted or "%s/%s" % (ns, label) in wanted)):
                    continue
                out.append({"label": label, "direction": way, "other": other, "edge": edge})
        return out

    def _edge_rows(self, id: str, way: str) -> List[Tuple[str, Dict[str, Any], str, Any, bool, str, str, bool]]:
        """One node's edges on one side, with what ``edges_of`` filters on: ``(eid, edge, rel, ns, symmetric,
        label, other, dead)``. ``dead`` is an archived edge or an edge to an archived node. Once the model is built
        (it is read-only then) each side of each node is worked out once, since the needs and richness passes ask
        for one node's edges several times with different filters."""
        key = (id, way)
        memo = self._cache.setdefault("edge_rows", {}) if self._frozen else None
        if memo is not None:
            hit = memo.get(key)
            if hit is not None:
                return hit
        rows = []
        for eid in (self.out if way == "out" else self.inc).get(id, []):
            edge = self.edges[eid]
            rel = str(edge.get("rel") or "")
            ns = self.rel_ns(eid)  # two imports may declare one relation name differently
            symmetric = self.registry.is_symmetric(rel, ns)
            label = rel if way == "out" or symmetric else self.registry.inverse(rel, ns)
            other = self.other(edge, id)
            dead = edge.get("status") == "archived" or not self._alive(other)
            rows.append((eid, edge, rel, ns, symmetric, label, other, dead))
        if memo is not None:
            memo[key] = rows
        return rows

    def _alive(self, id: str) -> bool:
        node = self.node(id)
        return node is None or node.get("status") != "archived"

    def degree(self, id: str) -> int:
        """Active, non-background edges touching ``id``."""
        return sum(1 for e in self.edges_of(id) if not e["edge"].get("background"))

    def is_bridge(self, edge: Union[str, Dict[str, Any]]) -> bool:
        if isinstance(edge, str):
            return edge in self.bridges
        src, dst = edge.get("src"), edge.get("dst")
        return isinstance(src, str) and isinstance(dst, str) and self.ns_of(src) != self.ns_of(dst)

    def members(self, id: str) -> List[str]:
        """The ``same_as`` class of ``id`` (itself alone when it has none)."""
        cid = self.same_as.get(id)
        return list(self.classes[cid]) if cid else [id]

    def label(self, id: str, width: int = 80) -> str:
        """The display name with its markers: ``[untrusted] `` before, `` (draft)`` or `` (archived)`` after."""
        rec = self.record(id)
        if rec is None:
            return ""
        name = trunc(rec.get("name") or rec.get("rel") or id, width)
        return mark_text(name, rec)

    def import_label(self, ns: str) -> str:
        for e in self.imports:
            if e.get("ns") == ns:
                return lockfile.label(e)
        return ns

    def local_nodes(self, active_only: bool = False) -> List[str]:
        return [n for n in self.local_ids if not active_only or self.active(n)]

    def title(self) -> str:
        return str(self.manifest.get("title") or self.ns)

    # resolution --------------------------------------------------------------------------------------------
    def _alias_index(self) -> Dict[str, List[str]]:
        if self._aliases:
            return self._aliases
        index: Dict[str, List[str]] = {}
        for nid, node in self.nodes.items():
            if nid in self.virtual:
                continue
            ns = self.ns_of(nid)
            aliases = node.get("aliases")
            for alias in aliases if isinstance(aliases, list) else []:
                if not isinstance(alias, str) or not alias.strip():
                    continue
                text = alias.strip()
                if ns != "self" and (idmod.LOCAL_RE.match(text)):
                    text = idmod.qualify(ns, text)
                index.setdefault(text.lower(), []).append(nid)
        for key in index:
            index[key] = sorted(set(index[key]))
        self._aliases = index
        return index

    def _rank(self, ids: Iterable[str]) -> List[str]:
        return sorted(set(ids), key=lambda i: (-self.registry.priority(self.kind_of(i)), i))[:MAX_CANDIDATES]

    def resolve(self, text: str, accept: Optional[Callable[[str], bool]] = None,
                ns: Optional[str] = None) -> Dict[str, Any]:
        """Resolve typed text to an id: ``{query, id, candidates, ambiguous, note}``.

        Order: exact qualified id; exact local id; alias (case-insensitive); record id (edge or source); kind-prefixed
        (``tomato`` is ``crop:tomato`` when that is unique across the declared kinds); suffix match after ``:``,
        ``/`` or ``.``; a unique exact id in one import (``note`` says which). Otherwise ``id`` is None with up to 20
        candidates (by kind priority, then id). ``accept`` filters loose matches; ``ns`` prefers (or with ``self``
        limits to) one namespace. A qualified id whose namespace is imported but which is absent there raises
        ``NotFound("not in the ontology (garden v1)")``."""
        query = (text or "").strip()
        ok = accept or (lambda _i: True)

        def done(found: Optional[str], candidates: Sequence[str] = (), ambiguous: bool = False,
                 note: Optional[str] = None) -> Dict[str, Any]:
            return {"query": query, "id": found, "candidates": list(candidates), "ambiguous": ambiguous,
                    "note": note}

        if query.startswith("self/"):
            query, ns = query[5:], "self"
        elif self.own_local(query) != query:
            query, ns = self.own_local(query), "self"
        if not query:
            return done(None)
        low = query.lower()

        def in_scope(i: str) -> bool:
            return ns != "self" or self.ns_of(i) == "self"

        def prefer(found: List[str]) -> List[str]:
            if ns and ns != "self":
                scoped = [i for i in found if self.ns_of(i) == ns]
                return scoped or found
            return found

        # 1 and 2: an exact qualified or local id
        for exact in (query, low):
            if exact in self.nodes and exact not in self.virtual and in_scope(exact):
                return done(exact)
        m = idmod.QUAL_RE.match(low)
        if m and m.group(1) in {e.get("ns") for e in self.imports}:
            raise NotFound("not in the ontology (%s)" % self.import_label(m.group(1)), searched=low)
        # 3: an alias, case-insensitive (a local match wins; an active record wins over an archived one, so the
        # aliases a merge moves onto the kept node resolve to it, not to the merged-away node)
        hits = prefer([i for i in self._alias_index().get(low, []) if ok(i) and in_scope(i)])
        hits = [i for i in hits if self.active(i)] or hits
        local_hits = [i for i in hits if self.ns_of(i) == "self"]
        if len(local_hits) == 1 or len(hits) == 1:
            return done((local_hits or hits)[0], note="alias")
        if hits:
            return done(None, self._rank(hits), ambiguous=True, note="alias")
        # 4: a record id (an import's source reads under its qualified id)
        if low in self.edges or low in self.sources or (low in self.virtual and low in self.nodes):
            return done(low)
        # 5: kind-prefixed
        if ":" not in low and "/" not in low:
            prefixed = prefer([
                "%s:%s" % (kind, low) for kind in self.registry.kinds()
                if "%s:%s" % (kind, low) in self.nodes and ok("%s:%s" % (kind, low)) and in_scope("%s:%s" % (kind, low))
            ])
            if len(prefixed) == 1:
                return done(prefixed[0])
            if prefixed:
                return done(None, self._rank(prefixed), ambiguous=True)
        # 6: a suffix after ":", "/" or "." (a whole kind:slug is left to step 7, which says where it was found)
        id_shaped = bool(idmod.LOCAL_RE.match(low))
        tails = prefer(sorted(
            nid for nid in self.nodes
            if nid not in self.virtual and in_scope(nid) and ok(nid)
            and (nid.endswith(":" + low) or (nid.endswith("/" + low) and not id_shaped) or nid.endswith("." + low))
        ))
        if len(tails) == 1:
            return done(tails[0])
        if tails:
            return done(None, self._rank(tails), ambiguous=True)
        # 7: a unique exact id in one import
        if idmod.LOCAL_RE.match(low) and ns != "self":
            found = prefer(sorted(
                "%s/%s" % (e["ns"], low) for e in self.imports
                if "%s/%s" % (e["ns"], low) in self.nodes and ok("%s/%s" % (e["ns"], low))
            ))
            if len(found) == 1 or (found and ns and self.ns_of(found[0]) == ns):
                return done(found[0], note="found in %s" % self.import_label(self.ns_of(found[0])))
            if found:
                return done(None, self._rank(found), ambiguous=True)
        contains = [nid for nid in self.nodes if low in nid.lower() and ok(nid) and in_scope(nid)]
        return done(None, self._rank(contains))

    def require(self, text: str, accept: Optional[Callable[[str], bool]] = None, ns: Optional[str] = None) -> str:
        """The resolved id, or ``NotFound`` carrying the candidates."""
        res = self.resolve(text, accept, ns)
        if res["id"]:
            return res["id"]
        raise NotFound(
            "%s: %s" % (text, "ambiguous" if res["ambiguous"] else "not in the ontology"),
            res["candidates"], ambiguous=res["ambiguous"], searched=text,
        )


def mark_text(text: str, rec: Optional[Dict[str, Any]]) -> str:
    """``text`` with the trust and status markers of ``rec``."""
    if not rec:
        return text
    out = text
    if rec.get("trust") == "untrusted":
        out = "[untrusted] " + out
    if rec.get("status") == "proposed":
        out += " (draft)"
    elif rec.get("status") == "archived":
        out += " (archived)"
    return out


def clear_cache() -> None:
    """Forget every loaded graph (and the parsed exports)."""
    _CACHE.clear()
    lockfile.clear_cache()
