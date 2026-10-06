"""graph and entities: loading, resolution order, the qualified miss, duplicates kept as problems, bridges, same_as
classes, markers, the load cache, and match scoring. Also holds small builders the other core tests share."""

from __future__ import annotations

import json
import os
import unittest

from tests import _support
from ontokit import entities, errors, graph, ids, packs, store, util

TODAY = "2026-09-28"
CHANGE = "chg-20260928-000000"
SRC = "src-" + "0" * 12
COMMIT = "c0ffee" + "0" * 34

GARDEN_PACK = {
    "pack": "local",
    "version": 1,
    "extends": ["core"],
    "kinds": {
        "crop": {"label": "Crop", "plural": "crops", "dimension": "data", "aliases": ["plant"],
                 "fields": {"season": {"type": "string"}}},
        "plot": {"label": "Plot", "plural": "plots", "dimension": "data"},
    },
    "relations": {"grown_in": {"inverse": "grows", "from": ["crop"], "to": ["plot"], "brief": True}},
    "dimensions": {},
    "deliverables": {},
    "kind_map": [],
}


# builders ------------------------------------------------------------------------------------------------------
def mk_node(nid, name=None, status="confirmed", trust="user", conf=0.8, summary=None, attrs=None, aliases=None,
            gaps=None, prov=None, visibility="shared", archived=None, **extra):
    """A node record that passes its schema (provenance cites ``SRC`` unless given)."""
    kind = nid.split("/")[-1].split(":", 1)[0]
    rec = {
        "id": nid, "kind": kind, "name": name or nid.split(":", 1)[1].replace("-", " ").capitalize(),
        "summary": "A %s used in tests." % kind if summary is None else summary, "status": status, "trust": trust,
        "conf": conf, "visibility": visibility, "attrs": attrs or {}, "aliases": aliases or [], "gaps": gaps or [],
        "prov": [{"src": SRC, "loc": "L1-L1", "by": "user"}] if prov is None else prov, "created": TODAY,
        "updated": TODAY, "change": CHANGE, "archived": archived,
    }
    rec.update(extra)
    return rec


def mk_edge(src, rel, dst, status="confirmed", trust="user", conf=0.7, key="", prov=None, background=False,
            archived=None, symmetric=False):
    if symmetric and src > dst:
        src, dst = dst, src
    return {
        "id": ids.edge_id(src, rel, dst, key), "src": src, "rel": rel, "dst": dst, "key": key, "status": status,
        "trust": trust, "conf": conf, "note": "", "background": background,
        "prov": [{"src": SRC, "loc": "L1-L1", "by": "user"}] if prov is None else prov, "created": TODAY,
        "updated": TODAY, "change": CHANGE, "archived": archived,
    }


def archive_block(reason="replaced by a better record in tests", decision=None, superseded_by=()):
    return {"on": TODAY, "reason": reason, "decision": decision, "superseded_by": list(superseded_by)}


def source_row(sid=SRC, text="line one\n", **extra):
    row = {
        "id": sid, "kind": "note", "title": "Test note", "sha256": util.sha256_text(text), "bytes": len(text),
        "lines": text.count("\n"), "captured_at": "2026-09-28T12:00:00Z", "url": None, "fetched_at": None, "via": None,
        "stale_after_days": None, "trust": "untrusted", "redactions": {}, "supersedes": None, "erased": False,
    }
    row.update(extra)
    return row


def write_topic(tmp, nodes=(), edges=(), ns="t", local_pack=None, text="line one\n"):
    """A topic under ``tmp/<ns>`` holding the rows and one source whose text is ``text`` (its id is ``SRC``
    whatever the text, so tests can cite it; validation tests use the fixture instead). Returns the Repo."""
    root = _support.bare_topic(tmp, ns)
    if local_pack is not None:
        store.write_json(os.path.join(root, "packs", "local.pack.json"), local_pack)
    store.write_jsonl(os.path.join(root, "graph", "nodes.jsonl"), list(nodes))
    store.write_jsonl(os.path.join(root, "graph", "edges.jsonl"), list(edges))
    store.write_jsonl(os.path.join(root, "sources", "index.jsonl"), [source_row(text=text)])
    store.write_bytes(os.path.join(root, "sources", SRC + ".txt"), text.encode("utf-8"))
    return store.Repo.open(root)


def builtin_pack(name):
    with open(os.path.join(packs.BUILTIN_DIR, "%s.pack.json" % name), encoding="utf-8") as fh:
        return json.load(fh)


def make_export(ns, nodes, edges=(), sources=(), local_pack=None, title=None, bundled=None):
    """An export object as ``build`` writes it (C.17), shared nodes only."""
    pack_list = {"core": builtin_pack("core"), "discovery": builtin_pack("discovery"),
                 "local": local_pack or packs.empty_local_pack()}
    return {
        "meta": {
            "format": 1, "kit": "0.1.0", "name": "test-%s" % ns, "ns": ns, "title": title or "Test %s" % ns,
            "version": "v1", "data_hash": "0" * 64, "last_change": None,
            "packs": {k: {"sha256": packs.sha_of(v), "pack": v} for k, v in sorted(pack_list.items())},
            "imports": [], "counts": {"nodes": len(nodes), "edges": len(edges), "sources": len(sources)},
            "richness": None,
            "statuses": [{"id": "confirmed", "label": "Confirmed", "tone": "ok"}],
        },
        "nodes": list(nodes),
        "edges": list(edges),
        "sources": list(sources),
        "bundled": bundled or {},
    }


def add_import(repo, ns, export, ref="v1", commit=COMMIT, via=None, override=None, write_lock=True):
    """Vendor ``export`` under ``imports/<ns>/export.json`` (canonical bytes) and pin it in the lock."""
    data = util.canonical_bytes(export)
    store.write_bytes(repo.path("imports/%s/export.json" % ns), data)
    entry = {
        "ns": ns, "name": export["meta"]["name"], "from": None if via else "../%s" % ns, "ref": ref,
        "commit": commit, "export_sha256": util.sha256_hex(data), "manifest_sha256": None, "kit": "0.1.0",
        "format": 1, "packs": {}, "nodes": len(export["nodes"]), "edges": len(export["edges"]), "via": via,
        "override": override, "locked_at": TODAY,
    }
    if write_lock:
        from ontokit import lockfile

        lock = lockfile.read(repo)
        lock["imports"] = [e for e in lock["imports"] if e.get("ns") != ns] + [entry]
        lockfile.write(repo, lock)
    return entry


def garden_export():
    crop = mk_node("crop:tomato", "Tomato", aliases=["love apple"], attrs={"season": "summer"})
    mint = mk_node("crop:mint", "Mint", attrs={"season": "spring"})
    old = mk_node("crop:old-bean", "Old bean", status="archived",
                  archived=archive_block(superseded_by=["crop:mint"]))
    bed = mk_node("plot:north-bed", "North bed")
    steward = mk_node("role:bed-steward", "Bed steward")
    return make_export("garden", [crop, mint, old, bed, steward],
                       [mk_edge("crop:tomato", "grown_in", "plot:north-bed")], local_pack=GARDEN_PACK)


def clear():
    graph.clear_cache()
    store.clear_cache()


# tests ---------------------------------------------------------------------------------------------------------
class LoadTest(_support.TempCase):
    def test_mini_loads_counts_and_sources(self):
        root = _support.make_topic(self.tmp)
        onto = graph.Ontology.load(root)
        self.assertEqual(onto.stats["nodes"], 12)
        self.assertEqual(onto.stats["edges"], 15)
        self.assertEqual(onto.stats["sources"], 2)
        self.assertEqual(onto.problems, [])
        self.assertEqual(onto.ns, "mini")
        for sid in onto.sources:
            self.assertEqual(onto.kind_of(sid), "source")
            self.assertIn(sid, onto.virtual)
            self.assertTrue(onto.prov_index[sid])
        self.assertEqual(onto.ns_of("role:bed-steward"), "self")

    def test_cache_by_stamp(self):
        root = _support.make_topic(self.tmp)
        one = graph.Ontology.load(root)
        self.assertIs(graph.Ontology.load(root), one)
        rows, _ = store.read_jsonl(os.path.join(root, "graph", "nodes.jsonl"))
        rows.append(mk_node("term:mulch", prov=[]))
        store.write_jsonl(os.path.join(root, "graph", "nodes.jsonl"), rows)
        two = graph.Ontology.load(root)
        self.assertIsNot(two, one)
        self.assertIn("term:mulch", two.nodes)

    def test_from_rows_matches_load(self):
        root = _support.make_topic(self.tmp)
        repo = store.Repo.open(root)
        loaded = graph.Ontology.load(repo)
        nodes, _ = store.read_jsonl(repo.path("graph/nodes.jsonl"))
        edges, _ = store.read_jsonl(repo.path("graph/edges.jsonl"))
        srcs, _ = store.read_jsonl(repo.path("sources/index.jsonl"))
        built = graph.Ontology.from_rows(repo, repo.manifest, nodes, edges, srcs)
        self.assertEqual(sorted(built.nodes), sorted(loaded.nodes))
        self.assertEqual(sorted(built.edges), sorted(loaded.edges))
        self.assertEqual(built.lines, loaded.lines)

    def test_duplicates_reported_not_dropped(self):
        a = mk_node("role:cook", "Cook")
        b = mk_node("role:cook", "Head cook")
        repo = write_topic(self.tmp, [a])
        path = repo.path("graph/nodes.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(util.canonical_line(b) + "\n")
            fh.write(util.canonical_line(a) + "\n")
        onto = graph.Ontology.load(repo)
        self.assertEqual(onto.nodes["role:cook"]["name"], "Cook")
        codes = sorted(p.code for p in onto.problems)
        self.assertEqual(codes, ["P05", "P06"])
        self.assertTrue(all(p.file == "graph/nodes.jsonl" and p.line in (2, 3) for p in onto.problems))

    def test_bad_lines_are_problems(self):
        repo = write_topic(self.tmp, [mk_node("role:cook")])
        with open(repo.path("graph/edges.jsonl"), "a", encoding="utf-8") as fh:
            fh.write("[1, 2]\nnot json\n")
        onto = graph.Ontology.load(repo)
        self.assertEqual([(p.code, p.line) for p in onto.problems], [("P01", 1), ("P01", 2)])


class EdgesAndLabelsTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        self.repo = write_topic(self.tmp, [
            mk_node("role:cook", "Cook"),
            mk_node("process:prep", "Prep", status="proposed", trust="untrusted"),
            mk_node("process:old-prep", "Old prep", status="archived",
                    archived=archive_block(superseded_by=["process:prep"])),
            mk_node("term:mise", "Mise"),
            mk_node("term:setup", "Setup"),
        ], [
            mk_edge("role:cook", "owns", "process:prep"),
            mk_edge("role:cook", "owns", "process:old-prep", status="archived",
                    archived=archive_block(superseded_by=[])),
            mk_edge("term:setup", "related_to", "term:mise", symmetric=True),
            mk_edge("role:cook", "works_on", "process:prep", background=True),
        ])
        self.onto = graph.Ontology.load(self.repo)

    def test_inverse_names_and_archived_hidden(self):
        labels = sorted((e["label"], e["direction"], e["other"]) for e in self.onto.edges_of("process:prep"))
        self.assertEqual(labels, [("owned_by", "in", "role:cook"), ("worked_on_by", "in", "role:cook")])
        self.assertEqual(len(self.onto.edges_of("role:cook")), 2)
        self.assertEqual(len(self.onto.edges_of("role:cook", include_archived=True)), 3)
        self.assertEqual([e["label"] for e in self.onto.edges_of("role:cook", rels=["owns"])], ["owns"])
        self.assertEqual([e["label"] for e in self.onto.edges_of("process:prep", rels=["owned_by"])], ["owned_by"])

    def test_symmetric_reads_the_same_both_ways(self):
        self.assertEqual([e["label"] for e in self.onto.edges_of("term:mise")], ["related_to"])
        self.assertEqual([e["label"] for e in self.onto.edges_of("term:setup")], ["related_to"])

    def test_degree_skips_background_and_archived(self):
        self.assertEqual(self.onto.degree("role:cook"), 1)
        self.assertEqual(self.onto.degree("process:old-prep"), 0)

    def test_labels_marked(self):
        self.assertEqual(self.onto.label("process:prep"), "[untrusted] Prep (draft)")
        self.assertEqual(self.onto.label("process:old-prep"), "Old prep (archived)")
        self.assertEqual(self.onto.label("role:cook"), "Cook")
        self.assertEqual(self.onto.label("role:nobody"), "")
        self.assertFalse(self.onto.active("process:old-prep"))
        self.assertTrue(self.onto.active("process:prep"))


class EdgeRowsCacheTest(_support.TempCase):
    """``edges_of`` works each side of a node out once on a built model; every filter must give what the uncached
    path gives, and a caller that edits its result must not change the next call's."""

    def test_cached_edges_match_the_uncached_ones(self):
        repo = store.Repo.open(_support.make_topic(self.tmp))
        cached = graph.Ontology.load(repo)
        self.assertTrue(cached._frozen)
        graph.clear_cache()
        plain = graph.Ontology.load(repo)
        plain._frozen = False  # the same model with the cache turned off
        some_rels = sorted({str(e.get("rel")) for e in plain.edges.values()})[:2]
        checked = 0
        for nid in sorted(plain.nodes):
            for direction in ("both", "out", "in"):
                for rels in (None, some_rels):
                    for archived in (False, True):
                        args = (nid, direction, rels, archived)
                        first = cached.edges_of(*args)
                        self.assertEqual(first, plain.edges_of(*args), args)
                        for entry in first:
                            entry["label"] = "changed by the caller"
                        self.assertEqual(cached.edges_of(*args), plain.edges_of(*args), args)
                        checked += 1
            self.assertEqual(cached.degree(nid), plain.degree(nid), nid)
        self.assertGreater(checked, 0)
        self.assertFalse(plain._cache.get("edge_rows"))


class ResolveTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        self.repo = write_topic(self.tmp, [
            mk_node("role:bed-steward", "Bed steward", aliases=["bed captain", "role:bed-captain"]),
            mk_node("dataset:harvest-log", "Harvest log"),
            mk_node("term:log", "Log"),
            mk_node("process:log", "Log process"),
            mk_node("goal:weekly.harvest", "Weekly harvest"),
            mk_node("topic:t", "Test topic"),
        ], [mk_edge("role:bed-steward", "owns", "dataset:harvest-log")])
        add_import(self.repo, "garden", garden_export())
        clear()
        self.onto = graph.Ontology.load(self.repo)

    def test_exact_ids_first(self):
        self.assertEqual(self.onto.resolve("role:bed-steward")["id"], "role:bed-steward")
        self.assertEqual(self.onto.resolve("ROLE:BED-STEWARD")["id"], "role:bed-steward")
        self.assertEqual(self.onto.resolve("self/role:bed-steward")["id"], "role:bed-steward")
        self.assertEqual(self.onto.resolve("garden/crop:tomato")["id"], "garden/crop:tomato")

    def test_alias_then_record_ids(self):
        res = self.onto.resolve("Bed Captain")
        self.assertEqual((res["id"], res["note"]), ("role:bed-steward", "alias"))
        self.assertEqual(self.onto.resolve("role:bed-captain")["id"], "role:bed-steward")
        self.assertEqual(self.onto.resolve("love apple")["id"], "garden/crop:tomato")
        eid = ids.edge_id("role:bed-steward", "owns", "dataset:harvest-log")
        self.assertEqual(self.onto.resolve(eid)["id"], eid)
        self.assertEqual(self.onto.resolve(SRC)["id"], SRC)

    def test_alias_prefers_active_records(self):
        # regression: after a merge the kept node and the archived drop share aliases; the alias must resolve to
        # the kept node, not come back ambiguous
        repo = write_topic(os.path.join(self.tmp, "m"), [
            mk_node("role:bed-steward", "Bed steward", aliases=["captain", "role:bed-captain"]),
            mk_node("role:bed-captain", "Bed captain", aliases=["captain"], status="archived",
                    archived=archive_block(superseded_by=["role:bed-steward"])),
        ])
        clear()
        onto = graph.Ontology.load(repo)
        res = onto.resolve("captain")
        self.assertEqual((res["id"], res["note"]), ("role:bed-steward", "alias"))
        self.assertEqual(onto.resolve("role:bed-captain")["id"], "role:bed-captain")  # an exact id still wins

    def test_kind_prefixed_unique_and_ambiguous(self):
        self.assertEqual(self.onto.resolve("harvest-log")["id"], "dataset:harvest-log")
        res = self.onto.resolve("log")
        self.assertIsNone(res["id"])
        self.assertTrue(res["ambiguous"])
        self.assertEqual(res["candidates"], ["process:log", "term:log"])  # process priority 7 before term 5

    def test_suffix_and_import_matches(self):
        self.assertEqual(self.onto.resolve("harvest")["id"], "goal:weekly.harvest")
        res = self.onto.resolve("crop:tomato")
        self.assertEqual(res["id"], "garden/crop:tomato")
        self.assertEqual(res["note"], "found in garden v1")
        self.assertEqual(self.onto.resolve("tomato")["id"], "garden/crop:tomato")

    def test_qualified_miss_is_not_in_the_ontology(self):
        with self.assertRaises(errors.NotFound) as ctx:
            self.onto.resolve("garden/crop:melon")
        self.assertEqual(ctx.exception.message, "not in the ontology (garden v1)")

    def test_candidates_accept_and_self_scope(self):
        res = self.onto.resolve("ste")
        self.assertIsNone(res["id"])
        self.assertIn("role:bed-steward", res["candidates"])
        self.assertIsNone(self.onto.resolve("crop:tomato", ns="self")["id"])
        only_terms = self.onto.resolve("log", accept=lambda i: i.startswith("term:"))
        self.assertEqual(only_terms["id"], "term:log")
        with self.assertRaises(errors.NotFound):
            self.onto.require("nothing-like-this")


class ImportsTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        self.repo = write_topic(self.tmp, [
            mk_node("role:bed-steward", "Bed steward"),
            mk_node("process:menu", "Menu"),
        ], [
            mk_edge("garden/crop:mint", "same_as", "garden/crop:tomato", symmetric=True),
            mk_edge("garden/crop:tomato", "same_as", "role:bed-steward", symmetric=True),
            mk_edge("garden/crop:old-bean", "same_as", "process:menu", symmetric=True, status="archived",
                    archived=archive_block(superseded_by=[])),
            mk_edge("process:menu", "consumes", "garden/plot:north-bed"),
        ])
        add_import(self.repo, "garden", garden_export())
        clear()
        self.onto = graph.Ontology.load(self.repo)

    def test_import_nodes_are_qualified_and_kinds_keyed(self):
        self.assertIn("garden/crop:tomato", self.onto.nodes)
        self.assertEqual(self.onto.ns_of("garden/crop:tomato"), "garden")
        self.assertEqual(self.onto.kind_of("garden/crop:tomato"), "garden/crop")
        self.assertEqual(self.onto.kind_of("garden/role:bed-steward"), "role")  # discovery is shared
        self.assertEqual(self.onto.registry.display("garden/crop"), "crop")
        self.assertEqual(self.onto.stats["imports"], 1)
        self.assertEqual(self.onto.stats["nodes"], 2)

    def test_imported_edges_keyed_by_qualified_endpoints(self):
        key = ids.edge_id("garden/crop:tomato", "grown_in", "garden/plot:north-bed", "")
        self.assertIn(key, self.onto.edges)
        self.assertEqual(self.onto.edge_origin[key][0], "garden")
        self.assertFalse(self.onto.is_bridge(key))
        self.assertEqual([e["label"] for e in self.onto.edges_of("garden/plot:north-bed", rels=["grows"])],
                         ["grows"])

    def test_bridges_flagged(self):
        bridge = ids.edge_id("process:menu", "consumes", "garden/plot:north-bed")
        self.assertTrue(self.onto.is_bridge(bridge))
        self.assertIn(bridge, self.onto.bridges)
        self.assertTrue(self.onto.is_bridge({"src": "role:x", "dst": "garden/crop:y"}))
        self.assertFalse(self.onto.is_bridge({"src": "role:x", "dst": "role:y"}))

    def test_same_as_classes(self):
        members = ["garden/crop:mint", "garden/crop:tomato", "role:bed-steward"]
        self.assertEqual(self.onto.members("role:bed-steward"), members)
        cid = self.onto.same_as["role:bed-steward"]
        self.assertEqual(self.onto.classes[cid], members)
        self.assertNotIn("process:menu", self.onto.same_as)  # its same_as edge is archived
        self.assertEqual(self.onto.members("process:menu"), ["process:menu"])

    def test_without_imports(self):
        clear()
        onto = graph.Ontology.load(self.repo, include_imports=False)
        self.assertNotIn("garden/crop:tomato", onto.nodes)
        self.assertEqual(onto.exports, {})


class EntitiesTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        local = dict(GARDEN_PACK, kinds={"ingredient": {"label": "Ingredient", "plural": "ingredients"}},
                     relations={}, kind_map=[{"a": "garden/crop", "b": "ingredient"}])
        self.repo = write_topic(self.tmp, [
            mk_node("role:bed-steward", "Bed steward", aliases=["bed captain"]),
            mk_node("role:bed-stewart", "Bed stewart"),
            mk_node("term:bed-steward", "Bed steward"),
            mk_node("role:harvest-crew-lead", "Harvest crew lead"),
            mk_node("role:lead-harvest-crew", "Lead harvest crew"),
            mk_node("ingredient:tomato", "Tomato"),
            mk_node("ingredient:basil", "Basil", aliases=["love apple"]),
            mk_node("role:gone", "Bed steward", status="archived", archived=archive_block(superseded_by=[])),
        ], local_pack=local)
        add_import(self.repo, "garden", garden_export())
        clear()
        self.onto = graph.Ontology.load(self.repo)
        self.idx = entities.index(self.onto)

    def scores(self, kind, name, aliases=(), ns=None):
        return {m["id"]: m["score"] for m in entities.match(self.idx, kind, name, aliases, ns=ns)}

    def test_match_tiers(self):
        found = self.scores("role", "Bed Steward!", ns="self")
        self.assertEqual(found["role:bed-steward"], 1.0)
        self.assertEqual(found["term:bed-steward"], 0.7)
        self.assertEqual(found["role:bed-stewart"], 0.7)  # one edit on names of 6+ characters
        self.assertNotIn("role:gone", found)  # archived nodes are not candidates
        self.assertEqual(self.scores("role", "Bed captain", ns="self")["role:bed-steward"], 1.0)
        self.assertEqual(self.scores("role", "Crew", aliases=["bed captain"], ns="self")["role:bed-steward"], 1.0)
        crew = self.scores("role", "Harvest crew lead", ns="self")
        self.assertEqual(crew["role:harvest-crew-lead"], 1.0)
        self.assertGreaterEqual(crew["role:lead-harvest-crew"], 0.5)
        self.assertEqual(self.scores("role", "Zzz"), {})

    def test_sorted_by_score_then_id(self):
        found = entities.match(self.idx, "role", "Bed steward", ns="self")
        keys = [(-m["score"], m["id"]) for m in found]
        self.assertEqual(keys, sorted(keys))

    def test_kind_map_counts_as_same_kind(self):
        found = self.scores("ingredient", "Tomato")
        self.assertEqual(found["garden/crop:tomato"], 1.0)
        self.assertEqual(found["ingredient:tomato"], 1.0)

    def test_duplicates_local_pairs(self):
        pairs = entities.duplicates(self.onto)
        found = {(p["a"], p["b"]): p["score"] for p in pairs}
        self.assertEqual(found[("role:bed-steward", "role:bed-stewart")], 0.7)
        self.assertEqual(found[("role:bed-steward", "term:bed-steward")], 0.7)
        self.assertFalse(any("garden/" in a or "garden/" in b for a, b in found))
        self.assertEqual(entities.duplicates(self.onto, kind="ingredient"), [])
        self.assertEqual(len(entities.duplicates(self.onto, limit=1)), 1)

    def test_cross_between_namespaces(self):
        pairs = entities.cross(self.onto, "self", "garden")
        found = {(p["a"], p["b"]): p for p in pairs}
        self.assertEqual(found[("ingredient:tomato", "garden/crop:tomato")]["score"], 1.0)
        self.assertEqual(found[("ingredient:basil", "garden/crop:tomato")]["score"], 1.0)  # via the alias
        self.assertEqual(found[("role:bed-steward", "garden/role:bed-steward")]["score"], 1.0)
        self.assertEqual(found[("term:bed-steward", "garden/role:bed-steward")]["score"], 0.7)


if __name__ == "__main__":
    unittest.main()
