"""validate: the mini fixture is clean, mini-broken has its one dangling reference, one case per problem and
warning code, check_graph on rows in memory, and --fix repairing only P04 and P05."""

from __future__ import annotations

import json
import os
import shutil
import sys
import types
import unittest
from unittest import mock

from tests import _support
from tests.test_graph import add_import, archive_block, garden_export, mk_edge
from ontokit import graph, ids, store, util, validate

S1 = "src-e08998852112"  # the interview source of the mini fixture
S2 = "src-000a61a61d03"  # the handbook note


def node(nid, **kw):
    """A confirmed local node citing the fixture's interview source."""
    rec = {
        "id": nid, "kind": nid.split(":", 1)[0], "name": nid.split(":", 1)[1].replace("-", " ").capitalize(),
        "summary": "A node added by a validation test.", "status": "confirmed", "trust": "user", "conf": 0.8,
        "visibility": "shared", "attrs": {}, "aliases": [], "gaps": [],
        "prov": [{"src": S1, "loc": "L2-L2", "by": "user"}], "created": "2026-09-28", "updated": "2026-09-28",
        "change": None, "archived": None,
    }
    rec.update(kw)
    return rec


class Base(_support.TempCase):
    fixture = "mini"

    def setUp(self):
        super().setUp()
        self.root = _support.make_topic(self.tmp, self.fixture)
        self.repo = store.Repo.open(self.root)
        changes, _ = store.read_jsonl(self.repo.path("ledger/changes.jsonl"))
        self.change = changes[-1]["id"]
        self.decision = sorted(os.listdir(self.repo.path("ledger/decisions")))[0][:-5]

    def rows(self, rel):
        return store.read_jsonl(self.repo.path(rel))[0]

    def put(self, rel, rows):
        store.write_jsonl(self.repo.path(rel), rows)

    def edit(self, rel, rid, **changes):
        rows = self.rows(rel)
        for row in rows:
            if row.get("id") == rid:
                row.update(changes)
        self.put(rel, rows)

    def add_nodes(self, *nodes):
        rows = self.rows("graph/nodes.jsonl")
        for n in nodes:
            n.setdefault("change", self.change)
            if n.get("change") is None:
                n["change"] = self.change
        self.put("graph/nodes.jsonl", rows + list(nodes))

    def add_edges(self, *edges):
        for e in edges:
            e["change"] = self.change
            e["prov"] = [{"src": S1, "loc": "L2-L2", "by": "user"}]
        self.put("graph/edges.jsonl", self.rows("graph/edges.jsonl") + list(edges))

    def append(self, rel, text):
        with open(self.repo.path(rel), "a", encoding="utf-8") as fh:
            fh.write(text)

    def report(self, fix=False):
        return validate.validate(store.Repo.open(self.root), fix=fix)

    def codes(self, fix=False):
        return sorted({p.code for p in self.report(fix).problems})

    def assertCode(self, code, contains=None):
        rep = self.report()
        found = [p for p in rep.problems + rep.warnings if p.code == code]
        self.assertTrue(found, "%s not reported; got %s" % (code, [p.text() for p in rep.problems + rep.warnings]))
        if contains:
            self.assertTrue(any(contains in p.message for p in found), [p.text() for p in found])
        return found


class FixturesTest(Base):
    def test_mini_is_clean(self):
        rep = self.report()
        self.assertEqual(rep.problems, [])
        self.assertTrue(rep.ok)
        self.assertEqual([w.code for w in rep.warnings], ["W01"])
        self.assertRegex(rep.lines()[-1], r"^ok: 12 nodes, 15 edges, 2 sources, 0 imports, richness (n/a|[0-9]+)$")
        self.assertEqual(rep.summary["nodes"], 12)
        self.assertIn("problems", rep.to_json())

    def test_mini_broken_has_one_dangling_reference(self):
        root = _support.make_topic(self.tmp, "mini-broken", ns="broken")
        rep = validate.validate(store.Repo.open(root))
        self.assertEqual([(p.code, p.file) for p in rep.problems], [("P10", "graph/edges.jsonl")])
        self.assertIn("dataset:seed-bank", rep.problems[0].message)
        self.assertEqual(rep.lines(), [rep.problems[0].text()])

    def test_lines_cap_at_200(self):
        rep = validate.Report([validate.Problem("P02", "f", n, "m") for n in range(1, 251)], [], {})
        lines = rep.lines()
        self.assertEqual(len(lines), 201)
        self.assertEqual(lines[-1], "+50 more")


class ProblemCodesTest(Base):
    def test_p01_unreadable_line(self):
        self.append("graph/nodes.jsonl", "not json\n")
        found = self.assertCode("P01")
        self.assertEqual((found[0].file, found[0].line), ("graph/nodes.jsonl", 13))

    def test_p02_schema(self):
        self.edit("graph/nodes.jsonl", "role:bed-steward", conf=1.5)
        self.assertCode("P02", "role:bed-steward")

    def test_p03_format(self):
        manifest = store.read_json(self.repo.path("ontology.json"))
        store.write_json(self.repo.path("ontology.json"), dict(manifest, format=2))
        self.assertCode("P03", "kit too old")

    def test_p04_not_sorted_or_not_canonical(self):
        rows = self.rows("graph/nodes.jsonl")
        store.write_bytes(self.repo.path("graph/nodes.jsonl"),
                          "".join(util.canonical_line(r) + "\n" for r in reversed(rows)).encode("utf-8"))
        manifest = store.read_json(self.repo.path("ontology.json"))
        import json

        store.write_text(self.repo.path("ontology.json"), json.dumps(manifest, indent=2))
        found = self.assertCode("P04")
        self.assertEqual(sorted(p.file for p in found), ["graph/nodes.jsonl", "ontology.json"])

    def test_p04_append_only_needs_time_order_only(self):
        self.assertEqual(self.codes(), [])  # fixture lines share one timestamp in append order
        rows = self.rows("ledger/changes.jsonl")
        rows[0]["at"] = "2026-09-28T13:00:00Z"
        self.put("ledger/changes.jsonl", rows)
        store.write_bytes(self.repo.path("ledger/changes.jsonl"),
                          "".join(util.canonical_line(r) + "\n" for r in rows).encode("utf-8"))
        self.assertCode("P04")

    def test_p05_duplicate_line(self):
        with open(self.repo.path("graph/edges.jsonl"), encoding="utf-8") as fh:
            first = fh.readline()
        self.append("graph/edges.jsonl", first)
        found = self.assertCode("P05")
        self.assertEqual(found[0].line, 16)
        self.assertNotIn("P06", self.codes())

    def test_p06_same_id_and_alias_collisions(self):
        with open(self.repo.path("graph/nodes.jsonl"), encoding="utf-8") as fh:
            first = fh.readline()
        self.append("graph/nodes.jsonl", first.replace('"conf":0.8', '"conf":0.5', 1))
        self.assertCode("P06", "already used")

    def test_p06_alias_is_another_id(self):
        self.edit("graph/nodes.jsonl", "role:bed-steward", aliases=["bed captain", "role:plot-coordinator"])
        self.add_nodes(node("term:captain", aliases=["Bed Captain"]))
        self.assertCode("P06", "is the id of role:plot-coordinator")
        self.assertCode("P06", "also an alias")

    def test_p07_ids(self):
        self.add_nodes(node("goal:rename", kind="role"))
        rows = self.rows("graph/edges.jsonl")
        rows[0]["key"] = "second"
        self.put("graph/edges.jsonl", rows)
        found = self.assertCode("P07")
        messages = " ".join(p.message for p in found)
        self.assertIn("goal:rename", messages)
        self.assertIn("does not match", messages)

    def test_p08_kind_and_attrs(self):
        self.add_nodes(node("gadget:thing"))
        self.edit("graph/nodes.jsonl", "tool:rain-gauge", attrs={"interface": "fax", "invoke": "look"})
        self.assertCode("P08", "unknown kind")
        self.assertCode("P08", "tool:rain-gauge")

    def test_p09_relations(self):
        self.add_edges(mk_edge("role:bed-steward", "likes", "process:watering"),
                       mk_edge("dataset:harvest-log", "owns", "goal:shared-harvest"),
                       mk_edge("term:companion-planting", "related_to", "constraint:no-pesticides"))
        self.assertCode("P09", "unknown relation")
        self.assertCode("P09", "does not link")
        self.assertCode("P09", "sorted")

    def test_p11_provenance(self):
        self.add_nodes(node("term:mulch", prov=[]),
                       node("term:straw", prov=[{"src": "src-aaaaaaaaaaaa", "loc": "L1-L1", "by": "user"}]),
                       node("term:hay", prov=[{"src": "imp:garden@aaaaaaaaaaaa", "loc": "garden/crop:x",
                                               "by": "agent"}]))
        self.assertCode("P11", "without provenance")
        self.assertCode("P11", "not in the sources index")
        self.assertCode("P11", "not a pinned import")

    def test_p12_archive_blocks(self):
        self.edit("graph/nodes.jsonl", "process:old-rota",
                  archived=dict(archive_block(reason="too short"), decision="dec-20260928-nothing-0000"))
        self.edit("graph/nodes.jsonl", "process:watering", archived=archive_block(decision=self.decision))
        self.add_nodes(node("term:gone", status="archived"),
                       node("term:dropped", status="archived", archived=archive_block()))
        self.assertCode("P12", "20 or more")
        self.assertCode("P12", "not in the ledger")
        self.assertCode("P12", "on a record that is confirmed")
        self.assertCode("P12", "without an archive block")
        self.assertCode("P12", "required unless a merge")

    def test_p12_decision_no_longer_active(self):
        from ontokit import ledger, util

        ledger.decide(self.repo, "Water in the evening instead?", [], "no", supersedes=self.decision)
        # an archive keeps citing a decision superseded after it was applied, while the chain resolves
        self.assertNotIn("P12", self.codes())
        path = ledger.decision_path(self.repo, self.decision)
        with open(path, encoding="utf-8") as fh:
            old = json.load(fh)
        old["superseded_by"] = "dec-20260928-nothing-here-0000"
        with open(path, "wb") as fh:
            fh.write(util.canonical_bytes(old))
        self.assertCode("P12", "not active")

    def test_p13_supersession_and_archived_ends(self):
        self.edit("graph/nodes.jsonl", "process:old-rota",
                  archived=archive_block(decision=self.decision, superseded_by=["process:old-rota"]))
        self.add_edges(mk_edge("role:plot-coordinator", "owns", "process:old-rota"))
        self.assertCode("P13", "names the record itself")
        self.assertCode("P13", "touches the archived node")

    def test_p13_superseded_by_missing_or_archived(self):
        # term:older -> term:old -> (missing): an archived target whose chain ends nowhere
        self.add_nodes(node("term:old", status="archived",
                            archived=archive_block(decision=self.decision, superseded_by=["term:nothing"])),
                       node("term:older", status="archived",
                            archived=archive_block(decision=self.decision, superseded_by=["term:old"])),
                       node("term:chained", status="archived",
                            archived=archive_block(decision=None, superseded_by=["process:old-rota"])))
        self.assertCode("P13", "does not exist")
        self.assertCode("P13", "chain ends nowhere")
        # process:old-rota is archived, but superseded by the active process:watering: that chain resolves
        found = [p.text() for p in self.report().problems if p.code == "P13"]
        self.assertFalse(any("term:chained" in t for t in found), found)

    def test_p13_not_reported_for_an_erased_target(self):
        # a node merged into one that was later erased: both are archived and final, so this is not a problem
        erased = node("term:gone", status="archived", erased=True,
                      archived=archive_block(decision=self.decision, superseded_by=[]))
        self.add_nodes(erased, node("term:merged", status="archived",
                                    archived=archive_block(decision=None, superseded_by=["term:gone"])))
        self.assertNotIn("P13", self.codes())

    def test_archived_bridges_and_older_import_provenance(self):
        from tests.test_graph import COMMIT

        add_import(self.repo, "garden", garden_export())
        old_pin = ids.impsrc("garden", "f" * 40)  # the pin before an import update
        self.add_edges(mk_edge("garden/crop:gone", "related_to", "process:watering", status="archived",
                               archived=archive_block(decision=self.decision, superseded_by=[])))
        self.add_nodes(node("term:hay", prov=[{"src": old_pin, "loc": "garden/crop:tomato", "by": "agent"}]))
        self.assertEqual(self.codes(), [])
        self.add_nodes(node("term:straw", prov=[{"src": ids.impsrc("orchard", COMMIT), "loc": "orchard/x:y",
                                                 "by": "agent"}]))
        self.assertCode("P11", "not a pinned import")

    def test_p14_source_files(self):
        with open(self.repo.path("sources/%s.txt" % S2), "a", encoding="utf-8") as fh:
            fh.write("one more line\n")
        os.unlink(self.repo.path("sources/%s.txt" % S1))
        store.write_text(self.repo.path("sources/src-aaaaaaaaaaaa.txt"), "stray\n")
        self.assertCode("P14", "sha256 differs")
        self.assertCode("P14", "missing")
        self.assertCode("P14", "does not list")

    def test_p14_erased_source_skips_the_sha(self):
        self.edit("sources/index.jsonl", S2, erased=True)
        store.write_text(self.repo.path("sources/%s.txt" % S2), "[erased by %s]\n" % self.decision)
        self.assertNotIn("P14", self.codes())

    def test_p15_tampered_import(self):
        add_import(self.repo, "garden", garden_export())
        self.assertNotIn("P15", self.codes())
        with open(self.repo.path("imports/garden/export.json"), "ab") as fh:
            fh.write(b"\n")
        self.assertCode("P15", "differs from the lock")

    def test_p16_qualified_id_in_nodes(self):
        self.add_nodes(node("garden/crop:tomato", kind="crop"))
        self.assertCode("P16", "read-only")

    def test_p17_secret_in_a_topic_file(self):
        secret = _support.fake_secret("github")
        self.edit("graph/nodes.jsonl", "term:companion-planting", summary="token %s" % secret)
        found = self.assertCode("P17", "github")
        self.assertNotIn(secret, " ".join(p.text() for p in found))

    def test_p18_personal_data_through_sanitize(self):
        stub = types.ModuleType("ontokit.sanitize")
        stub.check_text = lambda text, policy: ["email"] if "IGNORE" in text else []
        with mock.patch.dict(sys.modules, {"ontokit.sanitize": stub}):
            found = self.assertCode("P18", "email")
        self.assertEqual([p.file for p in found], ["sources/%s.txt" % S2])

    def test_p19_paths(self):
        store.write_text(self.repo.path("graph/nodes 2.jsonl"), "")
        store.write_text(self.repo.path("ledger/notes.txt"), "")
        found = self.assertCode("P19")
        self.assertEqual(sorted(p.file for p in found), ["graph/nodes 2.jsonl", "ledger/notes.txt"])

    def test_p20_pack(self):
        pack = store.read_json(self.repo.path("packs/local.pack.json"))
        pack["kinds"] = {"role": {"label": "Role again"}}
        store.write_json(self.repo.path("packs/local.pack.json"), pack)
        self.assertCode("P20", "duplicate kind")

    def test_p20_local_question(self):
        self.append("packs/local.questions.jsonl", util.canonical_line({"id": "not-a-question-id", "ask": "x"}) + "\n")
        self.assertCode("P20")

    def test_p21_proposal_decision_and_change_files(self):
        name = os.listdir(self.repo.path("proposals/pending"))[0]
        os.makedirs(self.repo.path("proposals/done"))
        shutil.move(self.repo.path("proposals/pending/" + name), self.repo.path("proposals/done/" + name))
        path = self.repo.path("ledger/decisions/%s.json" % self.decision)
        store.write_json(path, dict(store.read_json(path), colour="blue"))
        rows = self.rows("ledger/changes.jsonl")
        rows[0]["by"] = "robot"
        store.write_bytes(self.repo.path("ledger/changes.jsonl"),
                          "".join(util.canonical_line(r) + "\n" for r in rows).encode("utf-8"))
        found = self.assertCode("P21")
        files = {p.file for p in found}
        self.assertIn("proposals/done/" + name, files)
        self.assertIn("ledger/decisions/%s.json" % self.decision, files)
        self.assertIn("ledger/changes.jsonl", files)

    def test_p22_change_links(self):
        self.edit("graph/nodes.jsonl", "term:companion-planting", change="chg-20260928-ffffff")
        from ontokit import ledger

        ledger.append_change(self.repo, "apply", "user", [], "names a missing proposal",
                             proposal="prop-20260928-abcdef")
        self.assertCode("P22", "chg-20260928-ffffff")
        self.assertCode("P22", "unknown proposal")


class WarningCodesTest(Base):
    def test_w01_expected_field(self):
        self.assertCode("W01", "attrs.audience")

    def test_w02_orphan(self):
        self.add_nodes(node("term:lonely"))
        self.assertCode("W02", "term:lonely")

    def test_w03_only_archived_references(self):
        self.add_nodes(node("term:forgotten"))
        self.add_edges(mk_edge("term:forgotten", "related_to", "term:zz", status="archived",
                               archived=archive_block(decision=None, superseded_by=["term:forgotten"])))
        self.add_nodes(node("term:zz"))
        self.assertCode("W03", "term:forgotten")

    def test_w04_bridge_target_archived_upstream(self):
        add_import(self.repo, "garden", garden_export())
        self.add_edges(mk_edge("garden/crop:old-bean", "related_to", "process:watering"))
        self.assertEqual(self.codes(), [])
        self.assertCode("W04", "garden/crop:old-bean")

    def test_w05_stale_source(self):
        self.edit("sources/index.jsonl", S2, stale_after_days=7, captured_at="2026-09-01T00:00:00Z")
        self.assertCode("W05", S2)

    def test_w06_pending_backlog(self):
        with mock.patch.dict(os.environ, {"ONTO_FIXED_NOW": "2026-12-01T00:00:00Z"}):
            self.assertCode("W06", "pending for 63 days")
        manifest = store.read_json(self.repo.path("ontology.json"))
        manifest["policy"]["max_pending"] = 1
        store.write_json(self.repo.path("ontology.json"), manifest)
        name = os.listdir(self.repo.path("proposals/pending"))[0]
        prop = store.read_json(self.repo.path("proposals/pending/" + name))
        store.write_json(self.repo.path("proposals/pending/prop-20260928-abcdef.json"),
                         dict(prop, id="prop-20260928-abcdef"))
        self.assertCode("W06", "more than max_pending 1")

    def test_w07_kit_differs(self):
        manifest = store.read_json(self.repo.path("ontology.json"))
        store.write_json(self.repo.path("ontology.json"), dict(manifest, kit="0.0.9"))
        self.assertCode("W07", "0.0.9")
        self.assertEqual(self.codes(), [])


class FixTest(Base):
    def test_fix_repairs_only_p04_and_p05(self):
        rows = self.rows("graph/nodes.jsonl")
        rows.append(node("gadget:thing", change=self.change))
        store.write_bytes(self.repo.path("graph/nodes.jsonl"),
                          "".join(util.canonical_line(r) + "\n" for r in reversed(rows)).encode("utf-8"))
        with open(self.repo.path("graph/edges.jsonl"), encoding="utf-8") as fh:
            first = fh.readline()
        self.append("graph/edges.jsonl", first)
        self.assertEqual(self.codes(), ["P04", "P05", "P08"])
        rep = self.report(fix=True)
        self.assertEqual(sorted({p.code for p in rep.problems}), ["P08"])
        self.assertTrue(rep.summary["fixed"])
        self.assertEqual(self.codes(), ["P08"])
        with open(self.repo.path("graph/nodes.jsonl"), "rb") as fh:
            data = fh.read()
        self.assertEqual(data, store.jsonl_bytes(self.rows("graph/nodes.jsonl")))

    def test_repeated_history_points_are_not_duplicates(self):
        # regression: with a fixed clock, equal measurements A, B, A are byte-identical lines; they are two points,
        # so they must not be P05 (nor dropped by --fix)
        path = self.repo.path("metrics/history.jsonl")
        with open(path, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        self.assertGreaterEqual(len(lines), 2)
        self.append("metrics/history.jsonl", lines[0] + "\n")
        self.assertNotIn("P05", self.codes())
        self.assertNotIn("P04", self.codes())
        before = _support.snapshot(self.root, skip=[".onto"])
        self.assertFalse(self.report(fix=True).summary["fixed"])
        self.assertEqual(_support.snapshot(self.root, skip=[".onto"]), before)
        with open(self.repo.path("ledger/changes.jsonl"), encoding="utf-8") as fh:
            self.append("ledger/changes.jsonl", fh.readline())
        self.assertIn("P05", self.codes())  # other append-only files still report duplicate lines

    def test_fix_on_a_clean_topic_writes_nothing(self):
        before = _support.snapshot(self.root, skip=[".onto"])
        rep = self.report(fix=True)
        self.assertFalse(rep.summary["fixed"])
        self.assertEqual(_support.snapshot(self.root, skip=[".onto"]), before)


class CheckGraphTest(Base):
    def test_rows_in_memory(self):
        nodes = self.rows("graph/nodes.jsonl")
        edges = self.rows("graph/edges.jsonl")
        srcs = self.rows("sources/index.jsonl")
        onto = graph.Ontology.from_rows(self.repo, self.repo.manifest, nodes, edges, srcs)
        self.assertEqual(validate.check_graph(onto), [])
        bad = dict(nodes[0], kind="role", conf=2)
        broken = graph.Ontology.from_rows(self.repo, self.repo.manifest, [bad] + nodes[1:], edges, srcs)
        codes = sorted({p.code for p in validate.check_graph(broken)})
        self.assertIn("P02", codes)
        self.assertIn("P07", codes)
        self.assertEqual(validate.check_graph(broken, touched=["role:bed-steward"]), [])
        ends = [e for e in edges if e["src"] != "role:plot-coordinator" and e["dst"] != "role:plot-coordinator"]
        dangling = graph.Ontology.from_rows(self.repo, self.repo.manifest,
                                            [n for n in nodes if n["id"] != "role:plot-coordinator"],
                                            edges, srcs)
        self.assertIn("P10", {p.code for p in validate.check_graph(dangling, touched=[])})
        self.assertNotIn("P10", {p.code for p in validate.check_graph(
            graph.Ontology.from_rows(self.repo, self.repo.manifest, nodes, ends, srcs), touched=[])})

    def test_edge_id_extension_is_accepted(self):
        edges = self.rows("graph/edges.jsonl")
        e = edges[0]
        full = util.sha256_text("\x1f".join((e["src"], e["rel"], e["dst"], e["key"])))
        edges[0] = dict(e, id="e:" + full[:14])
        onto = graph.Ontology.from_rows(self.repo, self.repo.manifest, self.rows("graph/nodes.jsonl"), edges,
                                        self.rows("sources/index.jsonl"))
        self.assertNotIn("P07", {p.code for p in validate.check_graph(onto)})
        self.assertTrue(ids.EDGE_RE.match(edges[0]["id"]))


if __name__ == "__main__":
    unittest.main()
