"""The main kinds (``q.vocab.kinds``): asked right after the quick start, before stage 1 and before gap questions on
single records; not a quick question (the quick start stays five); named by the answer result once the quick start
ends; its answer proposes local kinds (``add_kind``, which waits for confirm) plus one node per example. Also the
inverse label of ``rests_on`` (``underpins``), which no longer shares the name of the ``supports`` relation."""

from __future__ import annotations

import json
import os
import re

from tests import _support
from tests.test_graph import clear
from ontokit import commands, graph, interview, migrate, packs, store, validate

QUICK = list(interview.QUICK)
ASK = "What are the main kinds of things in Fresh garden? Name a few, with one example of each."
TEXT = "Beds, crops and harvests. The north bed is a bed, tomatoes are a crop, and the July harvest is a harvest."


def kinds_ops(ns="fresh"):
    """The ops the agent drafts from TEXT: three local kinds (pack changes) and one linked node per example."""
    ops = []
    for name, label, plural in (("bed", "Bed", "beds"), ("crop", "Crop", "crops"), ("harvest", "Harvest", "harvests")):
        ops.append({"op": "add_kind", "name": name, "kind": {"label": label, "plural": plural}})
    for ref, kind, name, quote in (("$north", "bed", "North bed", "The north bed is a bed"),
                                   ("$tomato", "crop", "Tomatoes", "tomatoes are a crop"),
                                   ("$july", "harvest", "July harvest", "the July harvest is a harvest")):
        ops.append({"op": "add_node", "ref": ref, "basis": "stated", "prov": [{"quote": quote}],
                    "node": {"kind": kind, "name": name}})
        ops.append({"op": "add_edge", "basis": "stated", "prov": [{"quote": quote}],
                    "edge": {"src": ref, "rel": "part_of", "dst": "topic:%s" % ns}})
    return ops


class MainKindsTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        clear()
        self.root = _support.init_topic(self.tmp, "fresh", "Fresh garden")
        self.repo = store.Repo.open(self.root)

    def onto(self):
        clear()
        return graph.Ontology.load(self.repo)

    def ids(self, n=10):
        return [q["id"] for q in interview.next_questions(self.onto(), n)]

    def finish_quick_start(self):
        for qid in QUICK:
            interview.answer(self.repo, qid, "An answer to %s for the garden." % qid)

    def test_the_bank_question(self):
        reg = packs.load(None, {"packs": ["core", "discovery"]})
        q = {r["id"]: r for r in reg.questions()}[interview.KINDS]
        self.assertEqual(q["ask"], "What are the main kinds of things in {topic}? Name a few, with one example of "
                                   "each.")
        self.assertIn("own vocabulary", q["why"])
        self.assertEqual((q["stage"], q["dimension"], q["quick"], q["repeatable"]), (3, "vocabulary", False, False))
        self.assertEqual(sorted(r["id"] for r in reg.questions() if r.get("quick")), sorted(QUICK))  # still five
        self.assertIn(interview.KINDS, migrate.KIT_STEPS["0.2.0"]["questions"])  # a 0.2.0 name, folded like the rest

    def test_comes_right_after_the_quick_start_on_a_fresh_topic(self):
        found = interview.next_questions(self.onto(), 7)
        self.assertEqual([q["id"] for q in found[:6]], QUICK + [interview.KINDS])
        kinds = found[5]
        self.assertEqual(kinds["ask"], ASK)
        self.assertTrue(kinds["after_quick"])
        self.assertNotIn("pinned", kinds)
        self.assertNotIn("quick", kinds)
        self.assertTrue(found[6]["id"].startswith("q.gap."))  # a gap on one record comes after it
        prog = interview.progress(self.onto())
        self.assertEqual(prog["quick"]["total"], 5)
        self.assertEqual(prog["stage"], 0)

    def test_first_once_the_quick_start_is_done_and_named_by_the_last_quick_answer(self):
        for qid in QUICK[:-1]:
            interview.answer(self.repo, qid, "An answer to %s for the garden." % qid)
        result = interview.answer(self.repo, QUICK[-1], "We keep the harvest log in the tool shed.")
        nxt = interview._with_next(self.repo, result)["next"]
        # q.data.where has the follow-up q.data.owner, yet the main kinds come first
        self.assertEqual(nxt["id"], interview.KINDS)
        self.assertEqual(nxt["reason"], "the main kinds of things, asked right after the quick start")
        prog = interview.progress(self.onto())
        self.assertTrue(prog["stages"][0]["done"])  # stage 0 is done without it
        self.assertNotEqual(prog["stage"], 0)
        found = self.ids(3)
        self.assertEqual(found[0], interview.KINDS)
        self.assertNotIn("q.people.decides", found[:1])  # stage 1 waits
        code, out, err = _support.run_cli(["next", "--n", "1"], self.root)
        self.assertEqual(code, 0, err)
        self.assertIn("1. q.vocab.kinds", out)
        self.assertIn("right after the quick start, stage 3", out)

    def test_an_answer_with_new_kinds_needs_confirm(self):
        self.finish_quick_start()
        ops = kinds_ops()
        self.assertEqual(interview.confirm_ops(self.onto(), ops), [1, 2, 3])
        cmd = commands.get("onto_answer")
        args = {"q": interview.KINDS, "text": TEXT, "ops": ops, "apply": True}
        before = _support.snapshot(self.root, skip=[".onto"])
        text, is_error, obj = commands.dispatch(cmd, args, commands.Context(repo=self.repo, mcp=True, profile="full"))
        self.assertTrue(is_error, text)
        self.assertTrue(obj["preview"])
        self.assertIn("pack or question changes: 1 (add_kind), 2 (add_kind), 3 (add_kind) need confirm=true", text)
        self.assertEqual(before, _support.snapshot(self.root, skip=[".onto"]))  # nothing written, not even the log
        self.assertEqual(self.ids(1), [interview.KINDS])  # still open: asked again after the OK turn
        text, is_error, obj = commands.dispatch(cmd, dict(args, confirm=True),
                                                commands.Context(repo=self.repo, mcp=True, profile="full"))
        self.assertFalse(is_error, text)
        self.assertEqual(obj["proposal"]["status"], "applied")
        onto = self.onto()
        for kind in ("bed", "crop", "harvest"):
            self.assertIsNotNone(onto.registry.kind(kind), kind)
        names = {n.get("name"): (n.get("kind"), n.get("status")) for n in onto.nodes.values()}
        self.assertEqual(names["North bed"], ("bed", "confirmed"))
        self.assertEqual(names["Tomatoes"], ("crop", "confirmed"))
        self.assertEqual(names["July harvest"], ("harvest", "confirmed"))
        self.assertNotIn(interview.KINDS, self.ids(40))
        clear()
        self.assertEqual([p.text() for p in validate.validate(self.repo).problems], [])

    def test_skipped_it_is_an_ordinary_question(self):
        self.finish_quick_start()
        interview.answer(self.repo, interview.KINDS, None, status="skipped")
        found = interview.next_questions(self.onto(), 40, include_put_off=True)
        item = [q for q in found if q["id"] == interview.KINDS][0]
        self.assertNotIn("after_quick", item)
        self.assertEqual(found[-1]["id"], interview.KINDS)  # put off: last
        self.assertNotIn(interview.KINDS, self.ids(40))

    def test_the_docs_give_the_flow_and_the_garden_example(self):
        repo_root = os.path.dirname(os.path.dirname(_support.PLUGIN_DIR))
        with open(os.path.join(repo_root, "AGENTS.md"), encoding="utf-8") as fh:
            agents = fh.read()
        how = agents.split("## 2. How to interview", 1)[1].split("\n## 3.", 1)[0]
        skill_path = os.path.join(_support.PLUGIN_DIR, "skills", "onto-interview", "SKILL.md")
        with open(skill_path, encoding="utf-8") as fh:
            skill = fh.read()
        for text in (how, skill):
            flat = re.sub(r"\s+", " ", text)
            self.assertIn("q.vocab.kinds", flat)
            self.assertIn("add_kind", flat)
            self.assertIn("confirm", flat)
            self.assertIn("beds, crops", flat.lower())


class UnderpinsTest(_support.TempCase):
    def test_the_inverse_of_rests_on_is_not_a_relation_name(self):
        reg = packs.load(None, {"packs": ["core", "discovery", "assessment"]})
        self.assertEqual(reg.inverse("rests_on"), "underpins")
        names = set(reg.relations())
        self.assertNotIn("underpins", names)
        inverses = [reg.inverse(r) for r in sorted(names)]
        self.assertEqual(inverses.count("underpins"), 1)
        self.assertNotIn(reg.inverse("rests_on"), names)


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()
