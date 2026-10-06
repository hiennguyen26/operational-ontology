"""compose: add verifies the manifest, a tampered vendored file fails P15, namespace collisions, shared vs qualified
packs, the bundle round trip (a fourth topic gets the parents from the bundle), pin conflicts refused and then allowed
with an override decision, the update diff (dangling and archived bridges), remove refused while bridges exist, and
same_as suggestion via aliases and via the kind map. Upstream topics are real git repos released by a small exporter
here, so these tests do not depend on the build package."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests import _support
from ontokit import (commands, compose, errors, graph, history, ledger, lockfile, mutate, packs, pipeline, richness,
                     sources, store, util, validate)

GARDEN_PACK = {
    "kinds": {
        "crop": {"label": "Crop", "plural": "crops", "dimension": "data", "fields": {"season": {"type": "string"}}},
        "plot": {"label": "Plot", "plural": "plots", "dimension": "data"},
    },
    "relations": {"grown_in": {"inverse": "grows", "from": ["crop"], "to": ["plot"], "brief": True}},
}
KITCHEN_PACK = {
    "kinds": {
        "ingredient": {"label": "Ingredient", "plural": "ingredients", "dimension": "data"},
        "dish": {"label": "Dish", "plural": "dishes", "dimension": "deliverables"},
    },
    "relations": {"made_with": {"inverse": "goes_into", "from": ["dish"], "to": ["ingredient"], "brief": True}},
}
GARDEN_NODES = [
    ("crop:tomato", "Tomato", {"attrs": {"season": "summer"}}),
    ("crop:mint", "Mint", {}),
    ("plot:north-bed", "North bed", {}),
    ("role:bed-steward", "Bed steward", {}),
    ("term:compost", "Compost", {"aliases": ["humus"]}),
]
GARDEN_EDGES = [("crop:tomato", "grown_in", "plot:north-bed")]
KITCHEN_NODES = [
    ("ingredient:tomato", "Tomato", {}),
    ("dish:tomato-salad", "Tomato salad", {}),
    ("term:humus", "Humus", {}),
]
KITCHEN_EDGES = [("dish:tomato-salad", "made_with", "ingredient:tomato")]
STATUSES = [{"id": "confirmed", "label": "Confirmed", "tone": "ok"},
            {"id": "proposed", "label": "Draft", "tone": "warn"},
            {"id": "archived", "label": "Archived", "tone": "neutral"}]


# builders ------------------------------------------------------------------------------------------------------
def clear():
    graph.clear_cache()
    store.clear_cache()


def src_of(repo):
    return sorted(sources.index(repo))[0]


def topic(tmp, ns, pack=None):
    """A topic made by ``mutate.init_topic`` with the kinds and relations of ``pack`` in its local pack."""
    repo = store.Repo.open(_support.init_topic(tmp, ns))
    ops = []
    for name, decl in sorted(((pack or {}).get("kinds") or {}).items()):
        ops.append({"n": len(ops) + 1, "op": "add_kind", "name": name, "kind": decl})
    for name, decl in sorted(((pack or {}).get("relations") or {}).items()):
        ops.append({"n": len(ops) + 1, "op": "add_relation", "name": name, "relation": decl})
    if ops:
        mutate.apply_ops(repo, ops, by="user", change_type="apply", summary="the topic's kinds")
    return repo


def add_records(repo, nodes=(), edges=(), prov=None, status="confirmed", trust="user"):
    """Nodes ``(id, name, extra)`` and edges ``(src, rel, dst)`` citing the topic's first source (confirmed and
    trusted unless ``status`` and ``trust`` say otherwise)."""
    cite = prov or [{"src": src_of(repo), "loc": "L1-L1", "by": "user"}]
    ops = []
    for nid, name, extra in nodes:
        node = {"id": nid, "kind": nid.split(":", 1)[0], "name": name, "summary": "%s, used in tests." % name}
        node.update(extra)
        ops.append({"n": len(ops) + 1, "op": "add_node", "node": node, "status": status, "trust": trust,
                    "conf": 0.8, "prov": cite})
    for src, rel, dst in edges:
        if rel in ("same_as", "related_to") and src > dst:
            src, dst = dst, src
        ops.append({"n": len(ops) + 1, "op": "add_edge", "edge": {"src": src, "rel": rel, "dst": dst, "key": ""},
                    "status": status, "trust": trust, "conf": 0.7, "prov": cite})
    return mutate.apply_ops(repo, ops, by="user", change_type="apply", summary="records for tests")


def tamper(repo, ns):
    """Edit one summary in the vendored export of ``ns``."""
    path = repo.path("imports/%s/export.json" % ns)
    export = store.read_json(path)
    export["nodes"][0]["summary"] = "Changed by hand."
    store.write_json(path, export)
    clear()


def build_export(repo, version, fmt=1):
    """The C.17 export of a topic, as ``build`` writes it: shared local records, the packs, the lock entries and
    the bundle of every import."""
    clear()
    onto = graph.Ontology.load(repo)
    local = {n: onto.nodes[n] for n in onto.local_ids}
    hidden = {n for n, rec in local.items() if rec.get("visibility") == "local"}
    nodes = [rec for n, rec in sorted(local.items()) if n not in hidden]
    edges = [onto.edges[e] for e in sorted(onto.local_edge_ids)
             if onto.edges[e].get("src") not in hidden and onto.edges[e].get("dst") not in hidden]
    pack_list = {}
    for name in ("core", "discovery"):
        with open(os.path.join(packs.BUILTIN_DIR, "%s.pack.json" % name), encoding="utf-8") as fh:
            pack_list[name] = json.load(fh)
    pack_list["local"] = store.read_json(repo.path(packs.LOCAL_PACK))
    rows = [{k: r.get(k) for k in ("id", "kind", "title", "sha256", "bytes", "captured_at", "url")}
            for _i, r in sorted(onto.sources.items())]
    lock = lockfile.read(repo)
    return {
        "meta": {
            "format": fmt, "kit": "0.1.0", "name": repo.name, "ns": repo.ns, "title": repo.manifest["title"],
            "version": version, "data_hash": store.data_hash(repo), "last_change": ledger.last_change(repo),
            "packs": {k: {"sha256": packs.sha_of(v), "pack": v} for k, v in sorted(pack_list.items())},
            "imports": lockfile.entries(lock),
            "counts": {"nodes": len(nodes), "edges": len(edges), "sources": len(rows)},
            "richness": None, "statuses": STATUSES,
        },
        "nodes": nodes, "edges": edges, "sources": rows, "bundled": compose.bundle(repo),
    }


def release(repo, version, fmt=1, manifest_sha=None, edit=None, git_root=None):
    """Write build/export.json and MANIFEST.json, commit and tag ``version``. ``edit(export)`` changes the export
    before it is written; ``manifest_sha`` puts another sha in the manifest; ``git_root`` is the work tree that
    holds the topic folder (a topic made with ``onto init --path``). Returns (commit, export sha256)."""
    export = build_export(repo, version, fmt)
    if edit:
        edit(export)
    data = util.canonical_bytes(export)
    store.write_bytes(repo.path("build/export.json"), data)
    sha = util.sha256_hex(data)
    manifest = {
        "format": fmt, "kit": "0.1.0", "name": repo.name, "ns": repo.ns, "version": version,
        "created": _support.FIXED_NOW, "data_hash": export["meta"]["data_hash"],
        "last_change": export["meta"]["last_change"], "files": {"build/export.json": manifest_sha or sha},
        "packs": {k: v["sha256"] for k, v in export["meta"]["packs"].items()}, "imports": export["meta"]["imports"],
        "counts": export["meta"]["counts"], "richness": None, "checks": {"validate": "ok"},
    }
    store.write_json(repo.path("MANIFEST.json"), manifest)
    root = git_root or repo.root
    if not os.path.isdir(os.path.join(root, ".git")):
        _support.git_init(root)
    commit = _support.commit_all(root, "release %s" % version)
    _support.tag(root, version)
    clear()
    return commit, sha


def garden(tmp):
    repo = topic(tmp, "garden", GARDEN_PACK)
    add_records(repo, GARDEN_NODES, GARDEN_EDGES)
    return repo


def kitchen(tmp):
    repo = topic(tmp, "kitchen", KITCHEN_PACK)
    add_records(repo, KITCHEN_NODES, KITCHEN_EDGES)
    return repo


def problems(repo, *codes):
    clear()
    found = validate.validate(repo).problems
    return [p.text() for p in found if not codes or p.code in codes]


def lock_of(repo):
    return {e["ns"]: e for e in lockfile.entries(lockfile.read(repo))}


def ctx_for(repo, mcp=False):
    return commands.Context(repo=repo, mcp=mcp, profile="full" if mcp else "cli")


# worlds: each is built once per module (git repos are slow to make) and copied into every test's temp folder
_WORLDS = {}
_BASES = []


def tearDownModule():
    for base in _BASES:
        shutil.rmtree(base, True)


def world(case, name):
    """Copy the world ``name`` (built on first use by ``BUILDERS[name]``) into ``case.tmp``; returns its info."""
    if name not in _WORLDS:
        base = os.path.realpath(tempfile.mkdtemp(prefix="onto-world-"))
        _BASES.append(base)
        _WORLDS[name] = (base, BUILDERS[name](base))
        clear()
    base, info = _WORLDS[name]
    for entry in sorted(os.listdir(base)):
        shutil.copytree(os.path.join(base, entry), os.path.join(case.tmp, entry), symlinks=True)
    clear()
    return info


def open_repos(case, *names):
    for name in names:
        setattr(case, name, store.Repo.open(os.path.join(case.tmp, name)))


def _garden_world(base):
    commit, sha = release(garden(base), "v1")
    topic(base, "g2t")
    return {"commit": commit, "sha": sha}


def _pair_world(base):
    info = {"garden_v1": release(garden(base), "v1"), "kitchen_v1": release(kitchen(base), "v1")}
    topic(base, "g2t")
    return info


def _imported_world(base):
    info = _pair_world(base)
    g2t = store.Repo.open(os.path.join(base, "g2t"))
    compose.add(g2t, None, "../garden", "v1")
    compose.add(g2t, None, "../kitchen", "v1")
    return info


def _bundle_world(base):
    info = _imported_world(base)
    g2t = store.Repo.open(os.path.join(base, "g2t"))
    add_records(g2t, [("goal:weekly-harvest-menu", "Weekly harvest menu", {})],
                [("garden/crop:tomato", "same_as", "kitchen/ingredient:tomato")])
    info["g2t_v1"] = release(g2t, "v1")
    topic(base, "market")
    return info


def _update_world(base):
    info = _imported_world(base)
    g2t = store.Repo.open(os.path.join(base, "g2t"))
    add_records(g2t, [], [("garden/crop:tomato", "same_as", "kitchen/ingredient:tomato"),
                          ("garden/crop:mint", "related_to", "kitchen/dish:tomato-salad"),
                          ("garden/plot:north-bed", "related_to", "kitchen/dish:tomato-salad")])
    # garden v2: mint becomes local (left out of the export) and the north bed is archived
    repo = store.Repo.open(os.path.join(base, "garden"))
    dec = ledger.decide(repo, "Keep the north bed?", ["keep=Keep", "drop=Drop"], "drop", scope=["plot:north-bed"])
    mutate.apply_ops(repo, [
        {"n": 1, "op": "update_node", "id": "crop:mint", "set": {"visibility": "local"}, "unset": [], "prov": []},
        {"n": 2, "op": "archive", "id": "plot:north-bed",
         "archived": {"reason": "the north bed was paved over this spring", "decision": dec["id"],
                      "superseded_by": []}},
    ], by="user", change_type="apply", summary="garden v2 changes")
    info["garden_v2"] = release(repo, "v2")
    return info


def _nested_world(base):
    """The bundle world plus food, a composed topic that imports g2t and is released."""
    info = _bundle_world(base)
    food = topic(base, "food")
    compose.add(food, None, "../g2t", "v1")
    info["food_v1"] = release(food, "v1")
    return info


def _settled_world(base):
    """pa is released at v1, v2 and v3; cone imports pa v1 and ctwo imports pa v2, both released. top imports cone
    and ctwo and pins pa v3 directly, which settles the three pa releases with an override."""
    pa = topic(base, "pa", GARDEN_PACK)
    info = {}
    for n, name in enumerate(("Tomato", "Mint", "Basil"), start=1):
        add_records(pa, [("crop:%s" % name.lower(), name, {})])
        info["pa_v%d" % n] = release(pa, "v%d" % n)
    for folder, ref in (("cone", "v1"), ("ctwo", "v2")):
        repo = topic(base, folder)
        compose.add(repo, None, "../pa", ref)
        info[folder] = release(repo, "v1")
    top = topic(base, "top")
    compose.add(top, None, "../cone", "v1")
    dec = ledger.decide(top, "Which pa release should top pin?", ["v1=v1", "v2=v2", "v3=v3"], "v3", scope=["pa/"])
    keep = "pa=%s" % info["pa_v3"][1]
    compose.add(top, None, "../pa", "v3", override=dec["id"], keep=keep)
    compose.add(top, None, "../ctwo", "v1", override=dec["id"], keep=keep)
    info["dec"] = dec["id"]
    return info


BUILDERS = {"garden": _garden_world, "pair": _pair_world, "imported": _imported_world, "bundle": _bundle_world,
            "update": _update_world, "nested": _nested_world, "settled": _settled_world}


# tests ---------------------------------------------------------------------------------------------------------
class AddTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        info = world(self, "garden")
        self.commit, self.sha = info["commit"], info["sha"]
        open_repos(self, "garden", "g2t")

    def test_add_pins_vendors_and_logs(self):
        result = compose.add(self.g2t, None, "../garden", "v1")
        self.assertTrue(result["written"])
        entry = lock_of(self.g2t)["garden"]
        self.assertEqual((entry["from"], entry["ref"], entry["commit"], entry["via"]),
                         ("../garden", "v1", self.commit, None))
        self.assertEqual(entry["export_sha256"], self.sha)
        self.assertEqual(store.file_sha256(self.g2t.path("imports/garden/export.json")), self.sha)
        with open(self.garden.path("MANIFEST.json"), "rb") as fh:
            self.assertEqual(entry["manifest_sha256"], util.sha256_hex(fh.read()))
        self.assertIn("garden/crop:tomato", result["node_diff"]["added"])
        self.assertEqual([d["change"] for d in result["lock_diff"]], ["added"])
        last = ledger.read_changes(self.g2t)[-1]
        self.assertEqual((last["type"], last["id"]), ("import", result["change"]))
        self.assertEqual(last["ids"], ["imp:garden@%s" % self.commit[:12]])
        self.assertEqual(history.read(self.g2t)[-1]["kind"], "import")
        self.assertEqual(problems(self.g2t), [])
        self.assertEqual([(p["ns"], p["ok"], p["verdict"]) for p in result["pins"]], [("garden", True, "current")])

    def test_default_ref_is_the_newest_release(self):
        add_records(self.garden, [("crop:basil", "Basil", {})])
        release(self.garden, "v2")
        result = compose.add(self.g2t, None, os.path.join(self.tmp, "garden"), None)
        self.assertEqual(lock_of(self.g2t)["garden"]["ref"], "v2")
        self.assertTrue(any("newest release" in n for n in result["notes"]))
        self.assertEqual(lock_of(self.g2t)["garden"]["from"], "../garden")  # stored relative to the topic root

    def test_preview_writes_nothing(self):
        before = _support.snapshot(self.g2t.root)
        result = compose.add(self.g2t, None, "../garden", "v1", preview=True)
        self.assertFalse(result["written"])
        self.assertEqual(result["lock_diff"][0]["ns"], "garden")
        self.assertEqual(_support.snapshot(self.g2t.root), before)

    def test_manifest_sha_is_verified(self):
        add_records(self.garden, [("crop:basil", "Basil", {})])
        release(self.garden, "v2", manifest_sha="0" * 64)
        before = _support.snapshot(self.g2t.root)
        with self.assertRaises(errors.Refused) as cm:
            compose.add(self.g2t, None, "../garden", "v2")
        self.assertIn("MANIFEST.json", cm.exception.message)
        self.assertEqual(_support.snapshot(self.g2t.root), before)

    def test_newer_export_format_is_refused(self):
        add_records(self.garden, [("crop:basil", "Basil", {})])
        release(self.garden, "v2", fmt=2)
        with self.assertRaises(errors.Refused) as cm:
            compose.add(self.g2t, None, "../garden", "v2")
        self.assertIn("upgrade the kit", cm.exception.message)

    def test_unreleased_and_bad_refs(self):
        with self.assertRaises(errors.GitError):
            compose.add(self.g2t, None, "../garden", "--upload-pack=x")
        with self.assertRaises(errors.GitError):
            compose.add(self.g2t, None, "../garden", "v9")
        with self.assertRaises(errors.NotFound):
            compose.add(self.g2t, None, "../nowhere", "v1")
        with self.assertRaises(errors.Refused):
            compose.add(self.g2t, None, ".", "v1")

    def test_namespace_collisions(self):
        kit = kitchen(self.tmp)
        release(kit, "v1")
        compose.add(self.g2t, None, "../garden", "v1")
        with self.assertRaises(errors.Refused) as cm:
            compose.add(self.g2t, "garden", "../kitchen", "v1")
        self.assertIn("namespace collision", cm.exception.message)
        with self.assertRaises(errors.Refused):
            compose.add(self.g2t, "g2t", "../kitchen", "v1")  # this topic's own ns
        with self.assertRaises(errors.Refused) as cm:
            compose.add(self.g2t, "garden2", "../garden", "v1")  # the same topic twice
        self.assertIn("imported twice", cm.exception.message)

    def test_same_commit_is_deduplicated(self):
        compose.add(self.g2t, None, "../garden", "v1")
        changes = len(ledger.read_changes(self.g2t))
        result = compose.add(self.g2t, None, "../garden", self.commit)
        self.assertFalse(result["written"])
        self.assertEqual(result["lock_diff"], [])
        self.assertTrue(any("nothing to change" in n for n in result["notes"]))
        self.assertEqual(len(ledger.read_changes(self.g2t)), changes)

    def test_a_failed_write_puts_every_file_back(self):
        before = _support.snapshot(self.g2t.root, skip=[".onto"])
        with mock.patch.object(compose.ledger, "append_change", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                compose.add(self.g2t, None, "../garden", "v1")
        self.assertEqual(_support.snapshot(self.g2t.root, skip=[".onto"]), before)
        self.assertFalse(os.path.exists(self.g2t.path("imports/garden")))
        compose.add(self.g2t, None, "../garden", "v1")
        before = _support.snapshot(self.g2t.root, skip=[".onto"])
        with mock.patch.object(compose.history, "append_point", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                compose.remove(self.g2t, "garden")
        self.assertEqual(_support.snapshot(self.g2t.root, skip=[".onto"]), before)

    def test_shared_and_qualified_packs(self):
        compose.add(self.g2t, None, "../garden", "v1")
        clear()
        onto = graph.Ontology.load(self.g2t)
        self.assertEqual(onto.kind_of("garden/crop:tomato"), "garden/crop")  # the local pack differs: qualified
        self.assertEqual(onto.kind_of("garden/role:bed-steward"), "role")  # discovery is the same pack: shared
        self.assertIsNotNone(onto.registry.kind("garden/crop"))

    def test_tampered_vendored_file_fails_p15_and_update_repairs_it(self):
        compose.add(self.g2t, None, "../garden", "v1")
        path = self.g2t.path("imports/garden/export.json")
        export = store.read_json(path)
        export["nodes"][0]["summary"] = "Changed by hand."
        store.write_json(path, export)
        clear()
        self.assertTrue(problems(self.g2t, "P15"))
        pins = compose.pin_status(self.g2t)
        self.assertEqual((pins[0]["ok"], pins[0]["verdict"]), (False, "mismatch"))
        stamp = store.version_stamp(self.g2t)
        from ontokit import render
        self.assertIn("mismatch", render.version_line(stamp))
        result = compose.update(self.g2t, "garden", "v1")
        self.assertTrue(result["written"])
        self.assertTrue(any("vendors again" in n for n in result["notes"]))
        self.assertEqual(problems(self.g2t), [])

    def test_adding_a_pinned_namespace_again_moves_the_pin(self):
        compose.add(self.g2t, None, "../garden", "v1")
        add_records(self.garden, [("crop:basil", "Basil", {})])
        _commit2, sha2 = release(self.garden, "v2")
        result = compose.add(self.g2t, None, "../garden", "v2")  # no pin conflict with its own older pin
        self.assertTrue(result["written"])
        entry = lock_of(self.g2t)["garden"]
        self.assertEqual((entry["ref"], entry["export_sha256"], entry["override"]), ("v2", sha2, None))
        self.assertEqual([(d["ns"], d["change"]) for d in result["lock_diff"]], [("garden", "changed")])
        self.assertEqual(result["node_diff"]["added"], ["garden/crop:basil"])
        self.assertTrue(any("moves the pin" in n for n in result["notes"]), result["notes"])
        self.assertFalse(any("not needed" in n for n in result["notes"]))
        self.assertEqual(problems(self.g2t), [])

    def test_a_linked_import_folder_is_refused(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "keep-me.txt"), "w") as fh:
            fh.write("not the topic's\n")
        os.makedirs(self.g2t.path("imports"), exist_ok=True)
        os.symlink(outside, self.g2t.path("imports/garden"))
        before = _support.snapshot(self.g2t.root, skip=[".onto"])
        for preview in (True, False):
            with self.assertRaises(errors.Refused) as cm:
                compose.add(self.g2t, None, "../garden", "v1", preview=preview)
            self.assertIn("imports/garden: P19 a symbolic link", cm.exception.message)
        self.assertEqual(sorted(os.listdir(outside)), ["keep-me.txt"])
        self.assertEqual(_support.snapshot(self.g2t.root, skip=[".onto"]), before)
        # remove does not unlink a file through a linked folder either
        os.unlink(self.g2t.path("imports/garden"))
        compose.add(self.g2t, None, "../garden", "v1")
        shutil.copy(self.g2t.path("imports/garden/export.json"), outside)
        shutil.rmtree(self.g2t.path("imports/garden"))
        os.symlink(outside, self.g2t.path("imports/garden"))
        clear()
        with self.assertRaises(errors.Refused) as cm:
            compose.remove(self.g2t, "garden")
        self.assertIn("P19", cm.exception.message)
        self.assertEqual(sorted(os.listdir(outside)), ["export.json", "keep-me.txt"])
        self.assertIn("garden", lock_of(self.g2t))


class BundleTest(_support.TempCase):
    """garden and kitchen are released; g2t imports both, bridges them and is released; market imports g2t."""

    def setUp(self):
        super().setUp()
        info = world(self, "bundle")
        self.garden_v1, self.kitchen_v1, self.g2t_v1 = info["garden_v1"], info["kitchen_v1"], info["g2t_v1"]
        open_repos(self, "garden", "kitchen", "g2t", "market")

    def test_bundle_is_flat_and_verified(self):
        bundled = compose.bundle(self.g2t)
        self.assertEqual(sorted(bundled), ["garden", "kitchen"])
        for ns, block in bundled.items():
            self.assertEqual(block["export"]["bundled"], {})
            self.assertEqual(block["sha256"], lockfile.bundle_sha(block["export"]))
            self.assertEqual(block["sha256"], lock_of(self.g2t)[ns]["export_sha256"])  # no own imports: same bytes
        store.write_bytes(self.g2t.path("imports/garden/export.json"), b"{}\n")
        with self.assertRaises(errors.DataError):
            compose.bundle(self.g2t)

    def test_fourth_topic_gets_the_parents_from_the_bundle(self):
        result = compose.add(self.market, None, "../g2t", "v1")
        lock = lock_of(self.market)
        self.assertEqual(sorted(lock), ["g2t", "garden", "kitchen"])
        self.assertEqual((lock["garden"]["via"], lock["garden"]["from"], lock["garden"]["commit"]),
                         ("g2t", None, self.garden_v1[0]))
        self.assertEqual(lock["kitchen"]["via"], "g2t")
        self.assertEqual(lock["g2t"]["via"], None)
        vendored = store.read_json(self.market.path("imports/garden/export.json"))
        self.assertEqual(vendored["bundled"], {})
        self.assertEqual({d["ns"]: d["change"] for d in result["lock_diff"]},
                         {"g2t": "added", "garden": "added", "kitchen": "added"})
        self.assertEqual(problems(self.market), [])
        clear()
        onto = graph.Ontology.load(self.market)
        self.assertIn("garden/crop:tomato", onto.nodes)
        self.assertIn("g2t/goal:weekly-harvest-menu", onto.nodes)
        self.assertEqual(onto.members("garden/crop:tomato"), ["garden/crop:tomato", "kitchen/ingredient:tomato"])
        pins = {p["ns"]: p for p in compose.pin_status(self.market)}
        self.assertEqual(pins["garden"]["verdict"], "bundled")
        # the market bundle round-trips again: every entry flattened, parents under their own ns
        self.assertEqual(sorted(compose.bundle(self.market)), ["g2t", "garden", "kitchen"])

    def test_pin_conflict_refused_then_allowed_with_an_override(self):
        compose.add(self.market, None, "../g2t", "v1")
        add_records(self.garden, [("crop:basil", "Basil", {})])
        _commit2, sha2 = release(self.garden, "v2")
        before = _support.snapshot(self.market.root)
        with self.assertRaises(errors.Refused) as cm:
            compose.add(self.market, None, "../garden", "v2")
        self.assertIn("pin conflict", cm.exception.message)
        conflict = cm.exception.extra["conflicts"][0]
        self.assertEqual(conflict["ns"], "garden")
        self.assertEqual(sorted(p["sha256"] for p in conflict["pins"]), sorted([sha2, self.garden_v1[1]]))
        self.assertEqual(_support.snapshot(self.market.root), before)
        with self.assertRaises(errors.Refused):  # keep alone is not enough
            compose.add(self.market, None, "../garden", "v2", keep="garden=%s" % sha2)
        dec = ledger.decide(self.market, "Which garden release should the market use?", ["v1=v1", "v2=v2"], "v2",
                            rationale="v2 adds basil", scope=["garden/"])
        decided = _support.snapshot(self.market.root, skip=[".onto"])
        with self.assertRaises(errors.UsageError):  # a bare sha mixed with ns=sha is a usage error, not a crash
            compose.add(self.market, None, "../garden", "v2", override=dec["id"],
                        keep="%s,garden=%s" % (sha2[:12], sha2[:12]))
        self.assertEqual(_support.snapshot(self.market.root, skip=[".onto"]), decided)
        result = compose.add(self.market, None, "../garden", "v2", override=dec["id"], keep="garden=%s" % sha2[:12])
        self.assertTrue(result["written"])
        entry = lock_of(self.market)["garden"]
        self.assertEqual((entry["via"], entry["ref"], entry["from"]), (None, "v2", "../garden"))
        self.assertEqual(entry["override"], {"decision": dec["id"], "kept": sha2, "dropped": [self.garden_v1[1]]})
        self.assertEqual(problems(self.market, "P15"), [])
        self.assertEqual(problems(self.market, "P10"), [])  # market has no bridges of its own
        # a later recompute keeps the recorded choice
        again = compose.add(self.market, None, "../g2t", "v1")
        self.assertFalse(again["written"])
        # a superseded decision no longer covers the conflict
        ledger.decide(self.market, "Which garden release should the market use?", ["v1=v1", "v2=v2"], "v1",
                      supersedes=dec["id"])
        clear()
        self.assertTrue(problems(self.market, "P15"))

    def test_a_broken_bundled_file_is_repaired_at_the_bundler_pin(self):
        add_records(self.g2t, [("goal:second", "Second goal", {})])
        release(self.g2t, "v2")
        compose.add(self.market, None, "../g2t", "v2")
        tamper(self.market, "garden")
        pins = {p["ns"]: p for p in compose.pin_status(self.market)}
        self.assertEqual(pins["garden"]["verdict"], "mismatch")
        self.assertIn("update g2t at its current ref v2", pins["garden"]["text"])
        self.assertEqual(pins["garden"]["repair"], {"ns": "g2t", "ref": "v2"})
        self.assertIsNone(pins["g2t"]["repair"])
        for args in (["import", "status"], ["import", "status", "--ns", "garden"]):
            code, out, err = _support.run_cli(args, self.market.root)
            self.assertEqual(code, 0, err)
            self.assertIn("onto import update --ns g2t --ref v2", out)
            self.assertNotIn("--ref v1", out)
        code, out, err = _support.run_cli(["import", "update", "--ns", "g2t", "--ref", "v2"], self.market.root)
        self.assertEqual(code, 0, err)
        self.assertIn("vendors again imports/garden/export.json", out)
        self.assertEqual(lock_of(self.market)["g2t"]["ref"], "v2")  # the bundler did not move
        self.assertEqual(problems(self.market), [])

    def test_inactive_override_is_refused(self):
        compose.add(self.market, None, "../g2t", "v1")
        add_records(self.garden, [("crop:basil", "Basil", {})])
        _c, sha2 = release(self.garden, "v2")
        with self.assertRaises(errors.NotFound):
            compose.add(self.market, None, "../garden", "v2", override="dec-20260928-nothing-0000",
                        keep="garden=%s" % sha2)

    def test_remove_refused_while_bridges_exist(self):
        before = _support.snapshot(self.g2t.root)
        with self.assertRaises(errors.Refused) as cm:
            compose.remove(self.g2t, "kitchen")
        self.assertIn("still refer to kitchen", cm.exception.message)
        self.assertTrue(any("P10" in p for p in cm.exception.extra["problems"]))
        self.assertEqual(_support.snapshot(self.g2t.root), before)

    def test_remove_drops_the_parents_it_bundled(self):
        compose.add(self.market, None, "../g2t", "v1")
        with self.assertRaises(errors.Refused):
            compose.remove(self.market, "garden")  # bundled: remove g2t instead
        preview = compose.remove(self.market, "g2t", preview=True)
        self.assertFalse(preview["written"])
        self.assertIn("garden/crop:tomato", preview["node_diff"]["removed"])
        result = compose.remove(self.market, "g2t")
        self.assertTrue(result["written"])
        self.assertEqual(lock_of(self.market), {})
        self.assertFalse(os.path.exists(self.market.path("imports/garden")))
        self.assertEqual(problems(self.market), [])


class NestedTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        world(self, "nested")
        open_repos(self, "g2t", "food", "market")

    def test_a_topic_bundled_by_two_imports_is_pinned_once(self):
        result = compose.add(self.market, None, "../food", "v1")
        lock = lock_of(self.market)
        self.assertEqual((lock["g2t"]["via"], lock["garden"]["via"]), ("food", "food"))
        self.assertEqual(problems(self.market), [])
        self.assertTrue(result["written"])

    def test_direct_and_bundled_at_the_same_commit(self):
        compose.add(self.market, None, "../g2t", "v1")
        direct_sha = lock_of(self.market)["g2t"]["export_sha256"]
        # the flattened bundle of g2t inside food is the same release as the direct pin: no override needed
        result = compose.add(self.market, None, "../food", "v1")
        self.assertTrue(result["written"])
        lock = lock_of(self.market)
        self.assertEqual((lock["g2t"]["via"], lock["g2t"]["export_sha256"]), (None, direct_sha))
        self.assertEqual(lock["garden"]["via"], "g2t")  # the pin already in the lock stays
        self.assertEqual(problems(self.market, "P15"), [])


class UpdateTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        world(self, "update")
        open_repos(self, "garden", "kitchen", "g2t")

    def test_status_says_behind(self):
        pins = {p["ns"]: p for p in compose.pin_status(self.g2t)}
        self.assertEqual((pins["garden"]["verdict"], pins["garden"]["latest"]), ("behind", "v2"))
        self.assertEqual(pins["kitchen"]["verdict"], "current")

    def test_update_diff_lists_dangling_and_archived_bridges(self):
        before = _support.snapshot(self.g2t.root)
        result = compose.update(self.g2t, "garden", None, preview=True)
        self.assertEqual(_support.snapshot(self.g2t.root), before)
        self.assertFalse(result["written"])
        self.assertEqual(result["node_diff"]["removed"], ["garden/crop:mint"])
        self.assertIn("garden/plot:north-bed", result["node_diff"]["changed"])
        self.assertEqual([b["end"] for b in result["bridges_affected"]["dangling"]], ["garden/crop:mint"])
        self.assertEqual([b["end"] for b in result["bridges_affected"]["archived"]], ["garden/plot:north-bed"])
        self.assertFalse(any(b["inherited"] for k in ("dangling", "archived") for b in result["bridges_affected"][k]))
        self.assertEqual(result["lock_diff"][0]["change"], "changed")
        self.assertTrue(any("P10" in p for p in result["problems"]))
        dangling = result["bridges_affected"]["dangling"][0]["edge"]  # the note names the bridge behind the problem
        self.assertTrue(any("until these bridges" in n and dangling in n for n in result["notes"]), result["notes"])
        done = compose.update(self.g2t, "garden", "v2")
        self.assertTrue(done["written"])
        self.assertEqual(lock_of(self.g2t)["garden"]["ref"], "v2")
        self.assertTrue(problems(self.g2t, "P10"))  # the dangling bridge, as the preview said

    def test_an_archived_end_names_its_replacement(self):
        add_records(self.garden, [("plot:south-bed", "South bed", {})])

        def replaced(export):
            for row in export["nodes"]:
                if row.get("id") == "plot:north-bed":
                    row["archived"] = dict(row["archived"], superseded_by=["plot:south-bed"])

        release(self.garden, "v3", edit=replaced)
        result = compose.update(self.g2t, "garden", "v3", preview=True)
        bed = [b for b in result["bridges_affected"]["archived"] if b["end"] == "garden/plot:north-bed"]
        self.assertEqual([b.get("now") for b in bed], [["garden/plot:south-bed"]])
        text = "\n".join(compose.render_import(result, "compact", ctx_for(self.g2t)))
        self.assertIn("-related_to-> kitchen/dish:tomato-salad now garden/plot:south-bed", text)

    def test_affected_bridges_carry_the_markers(self):
        add_records(self.g2t, [], [("garden/crop:mint", "related_to", "kitchen/ingredient:tomato")],
                    status="proposed", trust="untrusted")
        result = compose.update(self.g2t, "garden", "v2", preview=True)
        by_dst = {b["dst"]: b for b in result["bridges_affected"]["dangling"]}
        draft = by_dst["kitchen/ingredient:tomato"]
        self.assertEqual((draft.get("untrusted"), draft.get("draft"), draft.get("trust")), (True, True, "untrusted"))
        trusted = by_dst["kitchen/dish:tomato-salad"]
        self.assertNotIn("untrusted", trusted)
        self.assertNotIn("draft", trusted)
        text = "\n".join(compose.render_import(result, "compact", ctx_for(self.g2t)))
        self.assertIn("[untrusted] %s garden/crop:mint -related_to-> kitchen/ingredient:tomato (draft)"
                      % draft["edge"], text)
        plain = "%s garden/crop:mint -related_to-> kitchen/dish:tomato-salad" % trusted["edge"]
        self.assertIn(plain, text)
        self.assertNotIn("[untrusted] " + plain, text)
        self.assertNotIn(plain + " (draft)", text)

    def test_update_refusals(self):
        with self.assertRaises(errors.NotFound):
            compose.update(self.g2t, "orchard", None)
        result = compose.update(self.g2t, "kitchen", None)
        self.assertFalse(result["written"])  # already at the newest release


class InheritedBridgeTest(_support.TempCase):
    """market imports g2t, whose own bridges point into garden; switching market's garden pin to v2 (mint left out,
    the north bed archived) cuts two of g2t's bridges, which market only has through the import."""

    def setUp(self):
        super().setUp()
        info = world(self, "update")
        open_repos(self, "garden", "g2t")
        self.garden_v2 = info["garden_v2"]
        release(self.g2t, "v1")
        self.market = store.Repo.open(_support.init_topic(self.tmp, "market"))
        compose.add(self.market, None, "../g2t", "v1")
        self.mint = "garden/crop:mint -related_to-> kitchen/dish:tomato-salad"
        self.bed = "garden/plot:north-bed -related_to-> kitchen/dish:tomato-salad"

    def test_the_pin_conflict_says_what_each_side_cuts(self):
        before = _support.snapshot(self.market.root)
        with self.assertRaises(errors.Refused) as cm:
            compose.add(self.market, None, "../garden", "v2")
        message = cm.exception.message
        self.assertIn("pin conflict", message)
        self.assertIn("keeping v2 %s would cut 2 bridge(s)" % self.garden_v2[0][:7], message)
        self.assertIn("dangling ", message)
        self.assertIn("%s (bridge of g2t)" % self.mint, message)
        self.assertIn("archived ", message)
        pins = {p["via"] or "direct": p for p in cm.exception.extra["conflicts"][0]["pins"]}
        self.assertEqual(pins["g2t"]["cuts"], [])  # the pin g2t was released with keeps its bridges
        cuts = {c["end"]: c for c in pins["direct"]["cuts"]}
        self.assertEqual({e: (c["why"], c["inherited"], c["owner"]) for e, c in cuts.items()},
                         {"garden/crop:mint": ("dangling", True, "g2t"),
                          "garden/plot:north-bed": ("archived", True, "g2t")})
        self.assertEqual(_support.snapshot(self.market.root), before)

    def test_an_override_lists_the_bridges_it_cuts(self):
        dec = ledger.decide(self.market, "Which garden release should the market use?", ["v1=v1", "v2=v2"], "v2",
                            rationale="v2 is current", scope=["garden/"])
        keep = "garden=%s" % self.garden_v2[1]
        decided = _support.snapshot(self.market.root, skip=[".onto"])
        preview = compose.add(self.market, None, "../garden", "v2", override=dec["id"], keep=keep, preview=True)
        self.assertEqual(_support.snapshot(self.market.root, skip=[".onto"]), decided)
        self.assertFalse(preview["written"])
        self.assertIn("garden/crop:mint", preview["node_diff"]["removed"])
        ba = preview["bridges_affected"]
        self.assertEqual([(b["end"], b["inherited"], b["owner"]) for b in ba["dangling"]],
                         [("garden/crop:mint", True, "g2t")])
        self.assertEqual([(b["end"], b["inherited"], b["owner"]) for b in ba["archived"]],
                         [("garden/plot:north-bed", True, "g2t")])
        self.assertEqual(preview["problems"], [])  # market has no bridges of its own
        text = "\n".join(compose.render_import(preview, "text", ctx_for(self.market)))
        self.assertIn("bridges dangling 1: %s %s (bridge of g2t)" % (ba["dangling"][0]["edge"], self.mint), text)
        self.assertIn("bridges archived 1: %s %s (bridge of g2t)" % (ba["archived"][0]["edge"], self.bed), text)
        self.assertIn("note: 2 bridge(s) that g2t brings would dangle or point at archived nodes", text)
        done = compose.add(self.market, None, "../garden", "v2", override=dec["id"], keep=keep)
        self.assertTrue(done["written"])
        self.assertEqual(done["bridges_affected"], ba)
        self.assertEqual(lock_of(self.market)["garden"]["ref"], "v2")
        # after the write, validate warns (read-only, so never a problem) and onto gaps lists both, read-only
        report = validate.validate(self.market)
        self.assertEqual(report.problems, [])
        cut = [w.text() for w in report.warnings if "bridge of g2t" in w.text()]
        self.assertEqual(len(cut), 2)
        self.assertTrue(any("imports/g2t/export.json" in w and "garden/crop:mint, which the pins leave absent" in w
                            for w in cut), cut)
        self.assertTrue(any("garden/plot:north-bed, which the pins leave archived" in w for w in cut), cut)
        gaps = richness.ranked_gaps(graph.Ontology.load(self.market), type="dangling_bridge")
        self.assertEqual(sorted((g["note"].split(" ")[0], g["read_only"], g["owner"]) for g in gaps),
                         [("garden/crop:mint", True, "g2t"), ("garden/plot:north-bed", True, "g2t")])
        self.assertTrue(all(g["node"] == "kitchen/dish:tomato-salad" for g in gaps))
        self.assertTrue(all("keep the pin g2t was released with" in g["action"] for g in gaps))

    def test_a_change_that_keeps_both_ends_lists_nothing(self):
        # garden v1 directly: the release g2t bundles, so no bridge of g2t loses an end
        result = compose.add(self.market, None, "../garden", "v1", preview=True)
        self.assertEqual(result["bridges_affected"], {"dangling": [], "archived": []})
        self.assertFalse(any("brings" in n for n in result["notes"]))


class NamespaceAdviceTest(_support.TempCase):
    """herb-a and herb-b both use the ns herb; cone and ctwo bundle one each as herb, cthree bundles herb-a as
    herbx. Every refusal names options that exist, and following them works."""

    def mk(self, folder, ns=None):
        return mutate.init_topic(os.path.join(self.tmp, folder), folder, ns or folder, "Test %s" % folder)

    def setUp(self):
        super().setUp()
        for folder in ("herb-a", "herb-b"):
            release(self.mk(folder, "herb"), "v1")
        for folder, ns, parent in (("cone", None, "herb-a"), ("ctwo", None, "herb-b"), ("cthree", "herbx", "herb-a")):
            repo = self.mk(folder)
            compose.add(repo, ns, "../%s" % parent, "v1")
            release(repo, "v1")
        self.mix = self.mk("mix")
        compose.add(self.mix, None, "../cone", "v1")

    def refused(self, repo, ns, from_):
        before = _support.snapshot(repo.root)
        with self.assertRaises(errors.Refused) as cm:
            compose.add(repo, ns, from_, "v1")
        self.assertEqual(_support.snapshot(repo.root), before)
        return cm.exception

    def test_two_bundled_parents_under_one_ns(self):
        for ns in (None, "ctwo2"):
            exc = self.refused(self.mix, ns, "../ctwo")
            name = ns or "ctwo"
            self.assertIn("namespace collision: herb names different topics", exc.message)
            self.assertIn("herb is fixed by the releases of cone and %s" % name, exc.message)
            self.assertIn("importing %s under another ns does not change it" % name, exc.message)
            self.assertIn("import only one of cone and %s" % name, exc.message)
            self.assertNotIn("Import one of them under another ns", exc.message)
            self.assertEqual(exc.extra["fixed_by"], sorted(["cone", name]))
        # a direct pin of herb-a that cone also bundles as herb cannot move away from herb: not offered
        compose.add(self.mix, "herb", "../herb-a", "v1")
        exc = self.refused(self.mix, None, "../ctwo")
        self.assertIn("herb-a (direct); herb-a (via cone); herb-b (via ctwo)", exc.message)
        self.assertNotIn("move your direct pin", exc.message)
        # the advice can be followed: ctwo imports its parent under another ns and releases again
        ctwo = store.Repo.open(os.path.join(self.tmp, "ctwo"))
        compose.remove(ctwo, "herb")
        compose.add(ctwo, "hb", "../herb-b", "v1")
        release(ctwo, "v2")
        self.assertTrue(compose.add(self.mix, None, "../ctwo", "v2")["written"])
        self.assertEqual(problems(self.mix), [])

    def test_a_direct_import_against_a_bundled_parent(self):
        exc = self.refused(self.mix, "herb", "../herb-b")
        self.assertIn("herb-b (direct); herb-a (via cone)", exc.message)
        self.assertIn("herb is fixed by the release of cone", exc.message)
        self.assertIn('Options: import herb-b under another ns (onto import add --ns "<other>" --from ../herb-b --ref '
                      'v1)', exc.message)
        with self.assertRaises(errors.Refused) as cm:  # over MCP the way out names the tool and its arguments
            compose.add(self.mix, "herb", "../herb-b", "v1", mcp=True)
        self.assertIn("Options: import herb-b under another ns (onto_import action=add ns=<other> from=../herb-b "
                      "ref=v1)", cm.exception.message)
        self.assertNotIn("--", cm.exception.message)
        self.assertTrue(compose.add(self.mix, "hb", "../herb-b", "v1")["written"])
        # a direct pin already in the lock, against a parent that a new import bundles
        other = self.mk("other")
        compose.add(other, "herb", "../herb-b", "v1")
        exc = self.refused(other, None, "../cone")
        self.assertIn('move your direct pin of herb-b to another ns (onto import remove --ns herb, then onto import '
                      'add --ns "<other>" --from ../herb-b)', exc.message)
        compose.remove(other, "herb")
        compose.add(other, "hb", "../herb-b", "v1")
        self.assertTrue(compose.add(other, None, "../cone", "v1")["written"])

    def test_one_topic_under_two_ns(self):
        exc = self.refused(self.mix, None, "../cthree")  # bundled twice, as herb and as herbx
        self.assertIn("topic herb-a would be imported twice, as herb and as herbx: the release of cone bundles it as "
                      "herb; the release of cthree bundles it as herbx", exc.message)
        self.assertIn("Options: import only one of cone and cthree", exc.message)
        self.assertEqual(exc.extra["fixed_by"], ["cone", "cthree"])
        exc = self.refused(self.mix, "herb2", "../herb-a")  # direct, while cone bundles it as herb
        self.assertIn("the release of cone bundles it as herb", exc.message)
        self.assertIn("Import it as herb instead", exc.message)
        compose.add(self.mix, "herb", "../herb-a", "v1")  # following it: the same release is one pin
        self.assertEqual([(e["ns"], e["via"]) for e in lock_of(self.mix).values() if e["name"] == "herb-a"],
                         [("herb", None)])
        # the direct pin herb is still bundled as herb by cone, so moving it is not offered
        exc = self.refused(self.mix, None, "../cthree")
        self.assertIn("Options: import only one of cone and cthree", exc.message)
        self.assertNotIn("move your direct pin", exc.message)
        # a direct pin that nothing else bundles can move to the ns a new import fixes, and that works
        other = self.mk("other")
        compose.add(other, "ha", "../herb-a", "v1")
        exc = self.refused(other, None, "../cone")
        self.assertIn("the release of cone bundles it as herb", exc.message)
        self.assertIn("Options: move your direct pin from ha to herb (onto import remove --ns ha, then onto import "
                      "add --ns herb --from ../herb-a); or have cone import it as ha", exc.message)
        compose.remove(other, "ha")
        compose.add(other, "herb", "../herb-a", "v1")
        self.assertTrue(compose.add(other, None, "../cone", "v1")["written"])
        self.assertEqual(problems(other), [])

    def test_a_bundled_parent_under_this_topics_own_ns(self):
        own = self.mk("own", "herb")  # its own ns is herb, the ns cone's release gives herb-a
        for ns in (None, "c2"):
            exc = self.refused(own, ns, "../cone")
            name = ns or "cone"
            self.assertIn("namespace herb is this topic's own ns, and the release of %s bundles topic herb-a as herb"
                          % name, exc.message)
            self.assertIn("importing %s under another ns does not change it" % name, exc.message)
            self.assertIn("Options: have %s (from ../cone) import its herb parent under another ns and release again"
                          % name, exc.message)
            self.assertNotIn("import it under another ns", exc.message)
            self.assertEqual((exc.extra["fixed_by"], exc.extra["ns"]), ([name], "herb"))
        exc = self.refused(own, None, "../herb-b")  # a direct import can move
        self.assertIn("namespace herb is this topic's own ns; import herb-b under another ns (onto import add --ns "
                      '"<other>" --from ../herb-b --ref v1)', exc.message)
        exc = self.refused(store.Repo.open(os.path.join(self.tmp, "herb-a")), None, "../cone")
        self.assertIn("the release of cone bundles this topic itself (herb-a) as herb; a topic cannot import itself",
                      exc.message)
        # the advice can be followed: cone imports herb-a under another ns and releases again
        cone = store.Repo.open(os.path.join(self.tmp, "cone"))
        compose.remove(cone, "herb")
        compose.add(cone, "ha", "../herb-a", "v1")
        release(cone, "v2")
        self.assertTrue(compose.add(own, None, "../cone", "v2")["written"])
        self.assertEqual(problems(own), [])


class SuggestTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        world(self, "imported")
        open_repos(self, "garden", "kitchen", "g2t")

    def pairs(self, a=None):
        return [(p["a"], p["b"], p["score"]) for p in compose.candidates(self.g2t, a, "kitchen")["pairs"]]

    def test_alias_match_and_kind_map(self):
        found = self.pairs()
        self.assertIn(("garden/term:compost", "kitchen/term:humus", 1.0), found)  # alias, same (shared) kind
        self.assertIn(("garden/crop:tomato", "kitchen/ingredient:tomato", 0.7), found)  # same name, other kind
        mutate.apply_ops(self.g2t, [{"n": 1, "op": "map_kinds", "a": "garden/crop", "b": "kitchen/ingredient"}],
                         by="user", change_type="apply", summary="crops are ingredients")
        clear()
        self.assertEqual(self.pairs()[0], ("garden/crop:tomato", "kitchen/ingredient:tomato", 1.0))

    def test_suggest_saves_a_proposal_that_applies(self):
        mutate.apply_ops(self.g2t, [{"n": 1, "op": "map_kinds", "a": "garden/crop", "b": "kitchen/ingredient"}],
                         by="user", change_type="apply", summary="crops are ingredients")
        clear()
        proposal = compose.suggest(self.g2t, "garden", "kitchen")
        self.assertEqual(proposal["status"], "pending")
        first = proposal["ops"][0]
        self.assertEqual((first["edge"]["src"], first["edge"]["rel"], first["edge"]["dst"], first["conf"]),
                         ("garden/crop:tomato", "same_as", "kitchen/ingredient:tomato", 1.0))
        commit = lock_of(self.g2t)["garden"]["commit"]
        self.assertIn({"src": "imp:garden@%s" % commit[:12], "loc": "garden/crop:tomato", "by": "agent"},
                      first["prov"])
        # offered pairs are not offered again while the proposal is open
        again = compose.candidates(self.g2t, "garden", "kitchen")
        self.assertEqual(again["pairs"], [])
        self.assertEqual(again["offered"], {proposal["id"]: len(proposal["ops"])})
        pipeline.review(self.g2t, proposal["id"], {str(op["n"]): "accept" for op in proposal["ops"]})
        pipeline.commit(self.g2t, proposal["id"])
        clear()
        onto = graph.Ontology.load(self.g2t)
        self.assertEqual(onto.members("kitchen/ingredient:tomato"), ["garden/crop:tomato", "kitchen/ingredient:tomato"])
        self.assertEqual(problems(self.g2t), [])
        self.assertIsNone(compose.suggest(self.g2t, "garden", "kitchen"))  # linked now

    def test_rejected_pairs_are_not_offered_again(self):
        proposal = compose.suggest(self.g2t, None, "kitchen")
        pipeline.review(self.g2t, proposal["id"], {str(op["n"]): "reject" for op in proposal["ops"]})
        self.assertEqual(compose.candidates(self.g2t, None, "kitchen")["pairs"], [])

    def test_self_is_not_paired_with_itself(self):
        add_records(self.g2t, [("term:compost", "Compost", {}), ("term:composts", "Composts", {}),
                               ("goal:tomato", "Tomato", {})])
        clear()
        found = compose.candidates(self.g2t, None, "self")
        self.assertEqual(found["sides"], ["garden", "kitchen"])
        self.assertEqual([p for p in found["pairs"] if p["a"] == p["b"]], [])
        self.assertIn(("garden/term:compost", "term:compost"), [(p["a"], p["b"]) for p in found["pairs"]])
        proposal = compose.suggest(self.g2t, None, "self")  # the cross-namespace pairs are not lost
        self.assertEqual(proposal["status"], "pending")
        self.assertIn(("garden/term:compost", "term:compost"),
                      [(op["edge"]["src"], op["edge"]["dst"]) for op in proposal["ops"]])

    def test_candidates_carry_the_markers(self):
        add_records(self.g2t, [("term:humus", "Humus", {})], status="proposed", trust="untrusted")
        clear()
        pair = next(p for p in compose.candidates(self.g2t, None, "kitchen")["pairs"] if p["b"] == "term:humus")
        self.assertEqual((pair["a"], pair["a_flags"], pair["b_flags"]),
                         ("kitchen/term:humus", {}, {"untrusted": True, "draft": True}))
        code, out, err = _support.run_cli(["import", "suggest", "--with", "kitchen"], self.g2t.root)
        self.assertEqual(code, 0, err)
        self.assertIn("kitchen/term:humus =same_as= [untrusted] term:humus (draft) 1.00", out)

    def test_a_kind_map_of_a_removed_import_does_not_block_removal(self):
        mutate.apply_ops(self.g2t, [{"n": 1, "op": "map_kinds", "a": "garden/crop", "b": "kitchen/ingredient"}],
                         by="user", change_type="apply", summary="crops are ingredients")
        clear()
        # a kind_map entry naming a removed import is inert, so it no longer blocks the removal
        result = compose.remove(self.g2t, "garden")
        self.assertTrue(result["written"])
        self.assertEqual(problems(self.g2t, "P20"), [])

    def test_suggest_arguments(self):
        with self.assertRaises(errors.NotFound):
            compose.candidates(self.g2t, "orchard", "kitchen")
        with self.assertRaises(errors.UsageError):
            compose.candidates(self.g2t, "kitchen", "kitchen")
        with self.assertRaises(errors.UsageError):
            compose.candidates(self.g2t, "garden", None)


class PathTopicTest(_support.TempCase):
    """Two topics made with ``onto init --path`` in one git repo (mono/topics/orchard and mono/topics/cider): their
    files sit under the folder prefix, and they share the vN tags (orchard v1, cider v2, orchard v3)."""

    def setUp(self):
        super().setUp()
        self.mono = os.path.join(self.tmp, "mono")
        _support.git_init(self.mono)
        self.orchard = mutate.init_topic(os.path.join(self.mono, "topics", "orchard"), "orchard-club", "orchard",
                                         "Orchard club")
        self.cider = mutate.init_topic(os.path.join(self.mono, "topics", "cider"), "cider-club", "cider", "Cider club")
        _support.commit_all(self.mono, "two topics")
        self.v1 = release(self.orchard, "v1", git_root=self.mono)
        self.v2 = release(self.cider, "v2", git_root=self.mono)
        add_records(self.orchard, [("topic:cider-press", "Cider press", {})])
        self.v3 = release(self.orchard, "v3", git_root=self.mono)
        self.cellar = topic(self.tmp, "cellar")

    def test_a_subfolder_topic_is_imported_by_tag_and_by_default_ref(self):
        orchard_from = "../mono/topics/orchard"
        self.assertEqual(compose.topic_prefix(self.orchard.root), "topics/orchard/")
        self.assertEqual([t for t, _c in compose.release_tags(self.orchard.root)], ["v3", "v1"])
        self.assertEqual([t for t, _c in compose.release_tags(self.cider.root)], ["v2"])
        result = compose.add(self.cellar, None, orchard_from, "v1")
        self.assertTrue(result["written"])
        pin = lock_of(self.cellar)["orchard"]
        self.assertEqual((pin["ref"], pin["commit"], pin["export_sha256"], pin["from"]),
                         ("v1", self.v1[0], self.v1[1], orchard_from))
        pins = {p["ns"]: p for p in compose.pin_status(self.cellar)}
        self.assertEqual((pins["orchard"]["verdict"], pins["orchard"]["latest"]), ("behind", "v3"))
        done = compose.update(self.cellar, "orchard", None)  # the newest release of orchard, not cider's v2
        self.assertEqual(lock_of(self.cellar)["orchard"]["ref"], "v3")
        self.assertIn("ref v3: the newest release of %s" % orchard_from, done["notes"])
        self.assertIn("orchard/topic:cider-press", done["node_diff"]["added"])
        result = compose.add(self.cellar, None, "../mono/topics/cider", None)
        self.assertEqual(lock_of(self.cellar)["cider"]["ref"], "v2")
        self.assertTrue(any("ref v2: the newest release" in n for n in result["notes"]))
        self.assertEqual({p["ns"]: p["verdict"] for p in compose.pin_status(self.cellar)},
                         {"orchard": "current", "cider": "current"})
        self.assertEqual(problems(self.cellar), [])

    def test_another_topics_tag_and_the_repo_folder_are_refused_with_the_way_out(self):
        before = _support.snapshot(self.cellar.root)
        with self.assertRaises(errors.Refused) as cm:
            compose.add(self.cellar, None, "../mono/topics/orchard", "v2")  # cider's release
        self.assertIn("at v2 is not a release of this topic", cm.exception.message)
        self.assertIn("this topic's MANIFEST.json there is still v1", cm.exception.message)
        self.assertIn("Import a tag of its own (v3)", cm.exception.message)
        with self.assertRaises(errors.Refused) as cm:
            compose.add(self.cellar, None, "../mono", "v3")
        self.assertIn("../mono has no build/export.json at v3: it holds topic folders", cm.exception.message)
        self.assertIn("the releases of topics/cider and topics/orchard; import one with onto import add --from "
                      "../mono/topics/cider or onto import add --from ../mono/topics/orchard", cm.exception.message)
        self.assertEqual(_support.snapshot(self.cellar.root), before)
        code, out, err = _support.run_cli(["import", "add", "--from", "../mono/topics/orchard", "--ref", "v1"],
                                          self.cellar.root)
        self.assertEqual(code, 0, err)
        self.assertIn("+ orchard v1", out)

    def test_the_commit_of_another_topics_release_carries_this_topics_older_one(self):
        before = _support.snapshot(self.cellar.root)
        with self.assertRaises(errors.Refused) as cm:  # cider's v2 commit: orchard there is still its v1
            compose.add(self.cellar, None, "../mono/topics/orchard", self.v2[0][:12])
        self.assertIn("is not a release: its MANIFEST.json there is still release v1, made at %s" % self.v1[0][:7],
                      cm.exception.message)
        self.assertIn("Import v1 (onto import add --from ../mono/topics/orchard --ref v1)", cm.exception.message)
        self.assertEqual(_support.snapshot(self.cellar.root), before)
        compose.add(self.cellar, None, "../mono/topics/orchard", self.v3[0])  # orchard's own release commit
        pin = lock_of(self.cellar)["orchard"]
        self.assertEqual((pin["ref"], pin["commit"], pin["export_sha256"]), ("v3", self.v3[0], self.v3[1]))


class StaleProposalTest(_support.TempCase):
    """Open bridge proposals follow the pins: garden v2 adds squash and makes compost local (so it leaves the
    export); an update re-cites the tomato bridge and marks the compost one stale."""

    def setUp(self):
        super().setUp()
        info = world(self, "imported")
        open_repos(self, "garden", "kitchen", "g2t")
        self.v1 = info["garden_v1"][0]
        mutate.apply_ops(self.g2t, [{"n": 1, "op": "map_kinds", "a": "garden/crop", "b": "kitchen/ingredient"}],
                         by="user", change_type="apply", summary="crops are ingredients")
        add_records(self.garden, [("crop:squash", "Squash", {})])
        mutate.apply_ops(self.garden, [{"n": 1, "op": "update_node", "id": "term:compost",
                                        "set": {"visibility": "local"}, "unset": [], "prov": []}],
                         by="user", change_type="apply", summary="compost stays local")
        self.v2 = release(self.garden, "v2")[0]
        clear()

    def ops_by_pair(self, prop_id):
        prop = pipeline.load(self.g2t, prop_id)
        return {(op["edge"]["src"], op["edge"]["dst"]): op for op in prop["ops"]}, prop

    def test_update_recites_an_open_proposal_so_it_applies(self):
        proposal = compose.suggest(self.g2t, "garden", "kitchen")
        tomato = ("garden/crop:tomato", "kitchen/ingredient:tomato")
        compost = ("garden/term:compost", "kitchen/term:humus")
        self.assertEqual(sorted(self.ops_by_pair(proposal["id"])[0]), sorted([compost, tomato]))
        before = _support.snapshot(self.g2t.root)
        preview = compose.update(self.g2t, "garden", "v2", preview=True)
        self.assertEqual(_support.snapshot(self.g2t.root), before)
        self.assertTrue(any(n.startswith("would re-cite 1 open proposal(s)") for n in preview["notes"]))
        result = compose.update(self.g2t, "garden", "v2")
        self.assertTrue(any(n.startswith("re-cited 1 open proposal(s) to the current pins: %s op" % proposal["id"])
                            for n in result["notes"]), result["notes"])
        self.assertTrue(any("no longer applies at the current pins" in n and "garden/term:compost" in n
                            for n in result["notes"]), result["notes"])
        ops, prop = self.ops_by_pair(proposal["id"])
        self.assertIn({"src": "imp:garden@%s" % self.v2[:12], "loc": "garden/crop:tomato", "by": "agent"},
                      ops[tomato]["prov"])
        self.assertFalse(ops[tomato]["annot"]["stale"])
        self.assertTrue(ops[compost]["annot"]["stale"])
        warned = [w for w in prop["checks"]["warnings"] if w.get("code") == "stale"]
        self.assertEqual([w["n"] for w in warned], [ops[compost]["n"]])
        self.assertIn("garden/term:compost is not in garden v2", warned[0]["message"])
        found = compose.status(self.g2t)["proposals"]
        self.assertEqual([(r["id"], [s["n"] for s in r["stale"]], r["recited"]) for r in found],
                         [(proposal["id"], [ops[compost]["n"]], [])])
        # the tomato bridge applies; the stale compost op is rejected, which is not a judgement on the pair
        pipeline.review(self.g2t, proposal["id"], {str(ops[tomato]["n"]): "accept", str(ops[compost]["n"]): "reject"})
        pipeline.commit(self.g2t, proposal["id"])
        clear()
        self.assertEqual(problems(self.g2t), [])
        self.assertEqual(graph.Ontology.load(self.g2t).members("kitchen/ingredient:tomato"),
                         ["garden/crop:tomato", "kitchen/ingredient:tomato"])
        # garden v3 shares compost again: the pair is offered again, not skipped as rejected
        mutate.apply_ops(self.garden, [{"n": 1, "op": "update_node", "id": "term:compost",
                                        "set": {"visibility": "shared"}, "unset": [], "prov": []}],
                         by="user", change_type="apply", summary="compost is shared again")
        release(self.garden, "v3")
        compose.update(self.g2t, "garden", "v3")
        self.assertIn(compost, [(p["a"], p["b"]) for p in compose.candidates(self.g2t, "garden", "kitchen")["pairs"]])

    def test_stale_notes_name_the_review_call_of_the_caller(self):
        proposal = compose.suggest(self.g2t, "garden", "kitchen")
        result = compose.update(self.g2t, "garden", "v2", preview=True, mcp=True)
        self.assertTrue(any("review it (onto_review id=%s)" % proposal["id"] in n for n in result["notes"]),
                        result["notes"])
        result = compose.update(self.g2t, "garden", "v2")
        self.assertTrue(any("review it (onto review %s)" % proposal["id"] in n for n in result["notes"]),
                        result["notes"])

    def test_a_rejected_stale_proposal_does_not_block_the_pair(self):
        proposal = compose.suggest(self.g2t, "garden", "kitchen")
        ops, _prop = self.ops_by_pair(proposal["id"])
        compose.update(self.g2t, "garden", "v2")
        pipeline.review(self.g2t, proposal["id"], {str(op["n"]): "reject" for op in ops.values()})
        self.assertEqual(pipeline.load(self.g2t, proposal["id"])["status"], "rejected")
        clear()
        found = compose.candidates(self.g2t, "garden", "kitchen")
        # tomato was judged (its op applied at v2) and stays rejected; compost could not apply, so it is not
        self.assertNotIn(("garden/crop:tomato", "kitchen/ingredient:tomato"),
                         [(p["a"], p["b"]) for p in found["pairs"]])
        _o, rejected = compose._proposed_pairs(self.g2t)
        self.assertEqual(rejected, {("garden/crop:tomato", "kitchen/ingredient:tomato")})

    def test_a_pin_moved_by_git_is_re_cited_by_suggest_and_by_update(self):
        proposal = compose.suggest(self.g2t, "garden", "kitchen")
        path = self.g2t.path("proposals/pending/%s.json" % proposal["id"])
        with open(path, "rb") as fh:
            old = fh.read()
        compose.update(self.g2t, "garden", "v2")
        store.write_bytes(path, old)  # as if the lock moved under a proposal made elsewhere (git merge)
        clear()
        found = compose.status(self.g2t)["proposals"]
        self.assertEqual([(r["id"], r["recited"][0]["ns"]) for r in found], [(proposal["id"], "garden")])
        result = compose.cmd_import(ctx_for(self.g2t), {"action": "status"})
        text = "\n".join(compose.render_import(result, "compact", ctx_for(self.g2t)))
        self.assertIn("proposal %s: op" % proposal["id"], text)
        self.assertIn("Next: onto import update --ns garden --ref v2", text)
        done = compose.update(self.g2t, "garden", "v2")  # nothing to change, but it re-cites
        self.assertFalse(done["written"])
        self.assertTrue(any(n.startswith("re-cited 1 open proposal(s)") for n in done["notes"]), done["notes"])
        store.write_bytes(path, old)
        clear()
        # suggest brings it in step too, so the tomato pair counts as waiting in a proposal that applies
        result = compose.cmd_import(ctx_for(self.g2t), {"action": "suggest", "ns": "garden", "with": "kitchen"})
        self.assertIsNone(result["proposal"])
        self.assertEqual(result["offered"], {proposal["id"]: 1})
        self.assertTrue(any(n.startswith("re-cited 1 open proposal(s)") for n in result["notes"]), result["notes"])
        ops, _prop = self.ops_by_pair(proposal["id"])
        pipeline.review(self.g2t, proposal["id"], {str(op["n"]): "accept" if not op["annot"]["stale"] else "reject"
                                                   for op in ops.values()})
        pipeline.commit(self.g2t, proposal["id"])
        clear()
        self.assertEqual(problems(self.g2t), [])

    def test_suggest_replaces_a_proposal_whose_bridges_all_went_stale(self):
        draft = {"by": "agent", "summary": "compost is humus", "ops": [{
            "op": "add_edge", "edge": {"src": "garden/term:compost", "rel": "same_as", "dst": "kitchen/term:humus",
                                       "key": ""}, "conf": 0.9, "basis": "inferred",
            "prov": [{"src": "imp:garden@%s" % self.v1[:12], "loc": "garden/term:compost", "by": "agent"}]}]}
        dead = pipeline.prepare(self.g2t, draft, by="agent")
        compose.update(self.g2t, "garden", "v2")
        self.assertTrue(pipeline.load(self.g2t, dead["id"])["ops"][0]["annot"]["stale"])
        result = compose.cmd_import(ctx_for(self.g2t), {"action": "suggest", "ns": "garden", "with": "kitchen"})
        self.assertEqual(result["proposal"]["supersedes"], dead["id"])
        self.assertIn("%s replaces %s, whose bridges no longer apply at the current pins (superseded, not rejected)"
                      % (result["proposal"]["id"], dead["id"]), result["notes"])
        self.assertEqual(pipeline.load(self.g2t, dead["id"])["status"], "superseded")
        self.assertEqual(compose._proposed_pairs(self.g2t)[1], set())


class CommandTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        world(self, "pair")
        open_repos(self, "garden", "kitchen", "g2t")

    def test_cli_add_status_and_suggest(self):
        code, out, err = _support.run_cli(["import", "add", "--from", "../garden", "--ref", "v1"], self.g2t.root)
        self.assertEqual(code, 0, err)
        self.assertIn("import add garden: written chg-", out)
        self.assertIn("+ garden v1", out)
        code, out, err = _support.run_cli(["import", "add", "--from", "../kitchen"], self.g2t.root)
        self.assertEqual(code, 0, err)
        self.assertIn("onto import suggest --with kitchen", out)
        code, out, err = _support.run_cli(["import", "status"], self.g2t.root)
        self.assertEqual(code, 0, err)
        self.assertIn("garden v1", out)
        self.assertIn("| ok | current", out)
        code, out, err = _support.run_cli(["import", "suggest", "--with", "kitchen", "--json"], self.g2t.root)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["candidates"][0]["a"], "garden/term:compost")  # alias match first: no kind map yet
        self.assertTrue(data["proposal"]["id"].startswith("prop-"))
        code, out, err = _support.run_cli(["status", "--json"], self.g2t.root)
        self.assertEqual(code, 0, err)
        self.assertEqual([p["verdict"] for p in json.loads(out)["imports"]], ["current", "current"])

    def test_mcp_add_without_confirm_previews(self):
        cmd = commands.get("onto_import")
        before = _support.snapshot(self.g2t.root)
        text, is_error, obj = commands.dispatch(cmd, {"action": "add", "from": "../garden", "ref": "v1"},
                                                ctx_for(self.g2t, mcp=True))
        self.assertTrue(is_error)
        self.assertIn("Preview only", text)
        self.assertEqual(obj["lock_diff"][0]["ns"], "garden")
        self.assertEqual(_support.snapshot(self.g2t.root), before)
        text, is_error, obj = commands.dispatch(cmd, {"action": "add", "from": "../garden", "ref": "v1",
                                                      "confirm": True}, ctx_for(self.g2t, mcp=True))
        self.assertFalse(is_error, text)
        self.assertTrue(obj["written"])
        text, is_error, obj = commands.dispatch(cmd, {"action": "status"}, ctx_for(self.g2t, mcp=True))
        self.assertFalse(is_error, text)
        self.assertIn("onto_import", text + "onto_import")

    def test_errors_are_domain_errors(self):
        code, out, err = _support.run_cli(["import", "remove", "--ns", "orchard"], self.g2t.root)
        self.assertEqual(code, 1)
        self.assertIn("not imported", err)
        code, out, err = _support.run_cli(["import", "update"], self.g2t.root)
        self.assertEqual(code, 2)
        self.assertIn("needs ns", err)
        code, out, err = _support.run_cli(["import", "suggest", "--with", "self"], self.g2t.root)  # no imports yet
        self.assertEqual(code, 2, out + err)
        self.assertIn("imports: none", err)

def _read(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def _changes(repo):
    return store.read_jsonl(ledger.changes_path(repo))[0]


class ImportCrashTest(_support.TempCase):
    """import-write-not-crash-safe: an import write names its files in a write intent first, so a process killed
    half way is rolled back by the next writer (as an apply is), never left as an unlogged import change."""

    FILES = (lockfile.LOCK, "imports/garden/export.json", "imports/kitchen/export.json", ledger.CHANGES,
             history.HISTORY)

    def files(self, repo):
        return {rel: _read(repo.path(rel)) for rel in self.FILES}

    def killed(self, repo, at, call):
        """Run ``call`` in another process that dies (exit 9) when ``at`` (``module.function``) is called."""
        mod, name = at.split(".")
        script = (
            "import os, sys\n"
            "sys.path.insert(0, %r)\n"
            "from ontokit import compose, history, ledger, store\n"
            "def kill(*args, **kwargs):\n"
            "    os._exit(9)\n"
            "setattr({'history': history, 'ledger': ledger}[%r], %r, kill)\n"
            "repo = store.Repo.open(%r)\n"
            "%s\n" % (_support.PLUGIN_DIR, mod, name, repo.root, call)
        )
        env = dict(os.environ, ONTO_FIXED_NOW=_support.FIXED_NOW)
        proc = subprocess.run([sys.executable, "-c", script], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(proc.returncode, 9, proc.stderr.decode("utf-8", "replace"))
        clear()

    def rolled_back(self, repo, before):
        self.assertIsNotNone(store.pending_intent(repo))
        found = validate.validate(repo).problems
        self.assertTrue(any(p.code == "P22" and p.file == store.INTENT_REL for p in found), [p.text() for p in found])
        report = validate.validate(repo, fix=True)  # takes the write lock, which rolls the write back
        self.assertEqual([p.text() for p in report.problems], [])
        self.assertIsNone(store.pending_intent(repo))
        self.assertEqual(self.files(repo), before)

    def test_a_killed_import_remove_is_rolled_back(self):
        world(self, "imported")
        open_repos(self, "g2t")
        before = self.files(self.g2t)
        self.killed(self.g2t, "ledger.append_change", "compose.remove(repo, 'garden')")
        # half written: the export is gone and the lock changed, with no change line
        self.assertIsNone(_read(self.g2t.path("imports/garden/export.json")))
        self.assertNotEqual(_read(self.g2t.path(lockfile.LOCK)), before[lockfile.LOCK])
        self.assertEqual(_read(self.g2t.path(ledger.CHANGES)), before[ledger.CHANGES])
        self.rolled_back(self.g2t, before)
        self.assertEqual(sorted(lock_of(self.g2t)), ["garden", "kitchen"])
        # the remove runs again and is logged once
        count = len(_changes(self.g2t))
        self.assertTrue(compose.remove(self.g2t, "garden")["written"])
        added = _changes(self.g2t)[count:]
        self.assertEqual([(c["type"], c["extra"]) for c in added], [("import", {"action": "remove", "ns": "garden"})])
        self.assertEqual(problems(self.g2t), [])

    def test_a_killed_import_add_is_rolled_back_with_its_change_line(self):
        world(self, "pair")
        open_repos(self, "g2t")
        before = self.files(self.g2t)
        self.killed(self.g2t, "history.append_point", "compose.add(repo, None, '../garden', 'v1')")
        self.assertIsNotNone(_read(self.g2t.path("imports/garden/export.json")))
        self.assertEqual(_changes(self.g2t)[-1]["type"], "import")  # the change line landed, the history point not
        self.rolled_back(self.g2t, before)
        self.assertEqual(lock_of(self.g2t), {})
        self.assertTrue(compose.add(self.g2t, None, "../garden", "v1")["written"])
        self.assertEqual([c["type"] for c in _changes(self.g2t)].count("import"), 1)
        self.assertEqual(problems(self.g2t), [])

    def test_the_intent_names_the_lock_bytes_the_lock_writer_writes(self):
        world(self, "imported")
        open_repos(self, "g2t")
        lock = lockfile.read(self.g2t)
        for variant in (dict(lock, imports=list(reversed(lock["imports"]))),
                        {"imports": list(reversed(lock["imports"]))}):
            lockfile.write(self.g2t, variant)
            self.assertEqual(_read(self.g2t.path(lockfile.LOCK)), compose._lock_bytes(variant))


class RemoveOverrideTest(_support.TempCase):
    """import-remove-ignores-override: removing a direct pin that settled two bundled releases of one topic leaves
    a pin conflict that remove settles with override and keep, as add does."""

    def setUp(self):
        super().setUp()
        self.info = world(self, "settled")
        open_repos(self, "top")
        self.v1_sha, self.v2_sha = self.info["pa_v1"][1], self.info["pa_v2"][1]

    def test_remove_takes_override_and_keep(self):
        entry = lock_of(self.top)["pa"]
        self.assertEqual((entry["via"], entry["ref"]), (None, "v3"))
        before = _support.snapshot(self.top.root)
        with self.assertRaises(errors.Refused) as cm:
            compose.remove(self.top, "pa")
        message = cm.exception.message
        self.assertIn("pin conflict: pa is pinned at v1", message)
        self.assertIn("via cone", message)
        self.assertIn("via ctwo", message)
        self.assertIn('then run again with that decision and the export sha kept (the option id): onto import remove '
                      '--ns pa --override "<decision>" --keep "pa=<option>"', message)
        with self.assertRaises(errors.Refused) as cm:
            compose.remove(self.top, "pa", mcp=True)
        self.assertIn("onto_import action=remove ns=pa override=<decision> keep=pa=<option> confirm=true",
                      cm.exception.message)
        self.assertEqual(_support.snapshot(self.top.root), before)
        # the way out works: a decision, then remove with override and keep (the CLI passes both through)
        dec = ledger.decide(self.top, "Which pa release should top keep without the direct pin?",
                            ["%s=v1 via cone" % self.v1_sha[:12], "%s=v2 via ctwo" % self.v2_sha[:12]],
                            self.v1_sha[:12], scope=["pa/"])
        code, out, err = _support.run_cli(["import", "remove", "--ns", "pa", "--override", dec["id"], "--keep",
                                           "pa=%s" % self.v1_sha[:12]], self.top.root)
        self.assertEqual(code, 0, err)
        self.assertIn("import remove pa: written chg-", out)
        self.assertIn("(override %s)" % dec["id"], out)
        entry = lock_of(self.top)["pa"]
        self.assertEqual((entry["via"], entry["export_sha256"]), ("cone", self.v1_sha))
        self.assertEqual(entry["override"], {"decision": dec["id"], "kept": self.v1_sha, "dropped": [self.v2_sha]})
        self.assertEqual(problems(self.top), [])

    def test_mcp_remove_passes_override_and_keep(self):
        dec = ledger.decide(self.top, "Which pa release should top keep without the direct pin?", [], "pa v2 via ctwo")
        cmd = commands.get("onto_import")
        args = {"action": "remove", "ns": "pa", "override": dec["id"], "keep": "pa=%s" % self.v2_sha[:12]}
        text, is_error, obj = commands.dispatch(cmd, args, ctx_for(self.top, mcp=True))
        self.assertTrue(is_error)  # a preview without confirm
        self.assertEqual([(d["ns"], d["change"]) for d in obj["lock_diff"]], [("pa", "changed")])
        text, is_error, obj = commands.dispatch(cmd, dict(args, confirm=True), ctx_for(self.top, mcp=True))
        self.assertFalse(is_error, text)
        self.assertEqual(lock_of(self.top)["pa"]["via"], "ctwo")
        self.assertEqual(problems(self.top), [])


class CommitRefTest(_support.TempCase):
    """import-commit-ref-pins-older-release: between releases a topic's export is still the last release, so a
    commit that is not a release commit is refused with the release it carries; a release commit is pinned under
    its tag."""

    def setUp(self):
        super().setUp()
        info = world(self, "garden")
        open_repos(self, "garden", "g2t")
        self.v1, self.v1_sha = info["commit"], info["sha"]
        add_records(self.garden, [("crop:squash", "Squash", {})])
        self.later = _support.commit_all(self.garden.root, "squash, not released yet")
        clear()

    def test_a_commit_after_a_release_is_refused_with_the_release_it_carries(self):
        before = _support.snapshot(self.g2t.root)
        for ref in (self.later, self.later[:7]):
            with self.assertRaises(errors.Refused) as cm:
                compose.add(self.g2t, None, "../garden", ref)
            self.assertIn("../garden at %s is not a release: its MANIFEST.json there is still release v1, made at %s"
                          % (ref, self.v1[:7]), cm.exception.message)
            self.assertIn("Import v1 (onto import add --from ../garden --ref v1), or release the topic first",
                          cm.exception.message)
            self.assertEqual((cm.exception.extra["version"], cm.exception.extra["release_commit"]), ("v1", self.v1))
        self.assertEqual(_support.snapshot(self.g2t.root), before)
        # the release commit itself is pinned under its tag
        result = compose.add(self.g2t, None, "../garden", self.v1[:10])
        self.assertTrue(result["written"])
        entry = lock_of(self.g2t)["garden"]
        self.assertEqual((entry["ref"], entry["commit"], entry["export_sha256"]), ("v1", self.v1, self.v1_sha))
        self.assertIn("ref %s is the commit of release v1 of ../garden; pinned as v1" % self.v1[:10], result["notes"])
        self.assertEqual(compose.pin_status(self.g2t)[0]["verdict"], "current")
        with self.assertRaises(errors.Refused) as cm:  # update says the same, in the caller's words
            compose.update(self.g2t, "garden", self.later[:9], mcp=True)
        self.assertIn("Import v1 (onto_import action=update ns=garden ref=v1)", cm.exception.message)
        # released, the commit imports what it holds
        v2 = release(self.garden, "v2")[0]
        result = compose.update(self.g2t, "garden", v2)
        self.assertEqual(lock_of(self.g2t)["garden"]["ref"], "v2")
        self.assertIn("garden/crop:squash", result["node_diff"]["added"])
        self.assertEqual(problems(self.g2t), [])

    def test_a_release_commit_without_its_tag_is_pinned_by_commit(self):
        v2 = release(self.garden, "v2")[0]
        _support.git(self.garden.root, "tag", "-d", "v2")
        compose.add(self.g2t, None, "../garden", v2[:12])
        entry = lock_of(self.g2t)["garden"]
        self.assertEqual((entry["ref"], entry["commit"]), (v2[:12], v2))
        add_records(self.garden, [("crop:leek", "Leek", {})])
        later = _support.commit_all(self.garden.root, "leek, not released yet")
        with self.assertRaises(errors.Refused) as cm:  # no tag names that release: the advice names its commit
            compose.update(self.g2t, "garden", later)
        self.assertIn("still release v2, made at %s" % v2[:7], cm.exception.message)
        self.assertIn("Import %s (onto import update --ns garden --ref %s)" % (v2[:12], v2[:12]), cm.exception.message)


class McpHintTest(_support.TempCase):
    """compose-hints-in-cli-form: refusals and notes name MCP tools and arguments over MCP, onto commands and
    --flags on the CLI, and the CLI hints run as written."""

    def test_status_with_no_imports(self):
        world(self, "pair")
        open_repos(self, "g2t")
        cmd = commands.get("onto_import")
        text, is_error, _obj = commands.dispatch(cmd, {"action": "status"}, ctx_for(self.g2t, mcp=True))
        self.assertFalse(is_error, text)
        self.assertIn('imports: none (to add one: onto_import action=add from="<released topic repo>")', text)
        self.assertNotIn("onto import", text)
        code, out, err = _support.run_cli(["import", "status"], self.g2t.root)
        self.assertEqual(code, 0, err)
        self.assertIn('imports: none (to add one: onto import add --from "<released topic repo>")', out)

    def test_a_pin_conflict_names_the_decision_and_the_call_again(self):
        world(self, "bundle")
        open_repos(self, "garden", "market")
        compose.add(self.market, None, "../g2t", "v1")
        add_records(self.garden, [("crop:basil", "Basil", {})])
        _commit2, sha2 = release(self.garden, "v2")
        cmd = commands.get("onto_import")
        args = {"action": "add", "ns": "garden", "from": "../garden", "ref": "v2", "confirm": True}
        text, is_error, _obj = commands.dispatch(cmd, args, ctx_for(self.market, mcp=True))
        self.assertTrue(is_error)
        self.assertIn('Record which one to keep (onto_decide question="Which garden release should this topic '
                      'keep?" options=["%s=v2 ' % sha2[:12], text)
        self.assertIn("onto_import action=add ns=garden from=../garden ref=v2 override=<decision> "
                      "keep=garden=<option> confirm=true", text)
        self.assertNotIn("onto decide", text)
        self.assertNotIn(" --", text)
        # on the CLI the same refusal names commands that run as written
        code, out, err = _support.run_cli(["import", "add", "--ns", "garden", "--from", "../garden", "--ref", "v2"],
                                          self.market.root)
        self.assertEqual(code, 1, out)
        decide = re.search(r"Record which one to keep \((onto decide .*?)\), then run again", err).group(1)
        again = re.search(r"\(the option id\): (onto import add .*)$", err.strip()).group(1)
        code, out, derr = _support.run_cli(shlex.split(decide.replace("<option>", sha2[:12]))[1:] + ["--json"],
                                           self.market.root)
        self.assertEqual(code, 0, derr)
        dec = json.loads(out)["decision"]["id"]
        code, out, aerr = _support.run_cli(
            shlex.split(again.replace("<decision>", dec).replace("<option>", sha2[:12]))[1:], self.market.root)
        self.assertEqual(code, 0, aerr)
        entry = lock_of(self.market)["garden"]
        self.assertEqual((entry["via"], entry["export_sha256"], entry["override"]["decision"]), (None, sha2, dec))
        self.assertEqual(problems(self.market, "P15"), [])

    def test_no_such_override_and_keep_without_override(self):
        world(self, "bundle")
        open_repos(self, "garden", "market")
        compose.add(self.market, None, "../g2t", "v1")
        add_records(self.garden, [("crop:basil", "Basil", {})])
        _commit2, sha2 = release(self.garden, "v2")
        with self.assertRaises(errors.NotFound) as cm:
            compose.add(self.market, None, "../garden", "v2", override="dec-20260928-nothing-0000",
                        keep="garden=%s" % sha2, mcp=True)
        self.assertIn("record one with onto_decide", cm.exception.message)
        with self.assertRaises(errors.Refused) as cm:
            compose.add(self.market, None, "../garden", "v2", keep="garden=%s" % sha2, mcp=True)
        self.assertIn("(onto_decide)", cm.exception.message)

    def test_a_tampered_pin_names_the_update_at_its_own_ref(self):
        world(self, "update")
        open_repos(self, "g2t")
        tamper(self.g2t, "garden")
        for mcp, call in ((False, "onto import update --ns garden --ref v1"),
                          (True, "onto_import action=update ns=garden ref=v1")):
            with self.assertRaises(errors.Refused) as cm:
                compose.add(self.g2t, None, "../kitchen", "v1", mcp=mcp)
            self.assertIn("run %s to vendor it again" % call, cm.exception.message)


class KeepParsingTest(unittest.TestCase):
    def test_forms(self):
        sha = "ab" * 32
        self.assertEqual(compose.parse_keep("garden=%s" % sha[:12]), {"garden": sha[:12]})
        self.assertEqual(compose.parse_keep("garden=%s, kitchen=%s" % (sha, sha[:8])),
                         {"garden": sha, "kitchen": sha[:8]})
        self.assertEqual(compose.parse_keep(sha[:10]), {None: sha[:10]})
        self.assertEqual(compose.parse_keep(None), {})
        with self.assertRaises(errors.UsageError):
            compose.parse_keep("garden=xyz")
        with self.assertRaises(errors.UsageError):
            compose.parse_keep("garden=abc")
        for mixed in ("%s,garden=%s" % (sha[:12], sha[:12]), "garden=%s %s" % (sha[:12], sha[:12]),
                      "%s %s" % (sha[:12], sha[:10]), "garden=%s,garden=%s" % (sha[:12], "cd" * 6)):
            with self.assertRaises(errors.UsageError, msg=mixed):
                compose.parse_keep(mixed)
        self.assertEqual(compose.parse_keep("garden=%s garden=%s" % (sha[:12], sha[:12])), {"garden": sha[:12]})


if __name__ == "__main__":
    unittest.main()
