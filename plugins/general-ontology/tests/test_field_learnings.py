"""Field learnings in the core (kit 0.2.0): the explicit JSON depth guard (same answer on Python 3.9 to 3.14),
premises and ``rests_on`` with the archived-premise gap, decisions that narrow another, and the interview support
an agent needs to run one question per turn: ``next about=<id>``, the progress, why, options and follow-ups on
every item, the best next question in the answer result, ``onto log --last`` and the hook's resume hint."""

from __future__ import annotations

import io
import json
import os

from tests import _support
from tests.test_graph import clear
from ontokit import formats, graph, hook, interview, ledger, mcp_server, needs, packs, store, util, validate
from ontokit.errors import DataError, NotFound, Refused, UsageError

FOLDER = "My topic’s folder"  # a space and a curly apostrophe, as real paths have
DEPTH = util.JSON_MAX_DEPTH


def nested_list(depth):
    return "[" * depth + "]" * depth


def nested_dict(depth):
    value = {}
    node = value
    for _ in range(depth - 1):
        node["x"] = {}
        node = node["x"]
    return value


# C1: the depth guard ------------------------------------------------------------------------------------------------
class DepthGuardTest(_support.TempCase):
    def test_text_depth_counts_only_brackets_outside_strings(self):
        self.assertFalse(util.json_depth_over(nested_list(DEPTH)))
        self.assertTrue(util.json_depth_over(nested_list(DEPTH + 1)))
        quoted = json.dumps({"text": "[" * (DEPTH * 3) + "\\\"{" * DEPTH})
        self.assertFalse(util.json_depth_over(quoted))
        self.assertEqual(util.loads_strict(quoted)["text"][:3], "[[[")
        mixed = '{"a":' * DEPTH + '"x"' + "}" * DEPTH
        self.assertFalse(util.json_depth_over(mixed))
        self.assertTrue(util.json_depth_over('{"a":' + mixed + "}"))
        self.assertFalse(util.json_depth_over(nested_list(10), limit=10))
        self.assertTrue(util.json_depth_over(nested_list(11), limit=10))

    def test_reindenting_json_uses_the_kit_limit(self):
        deep = '{"a":' * DEPTH + "1" + "}" * DEPTH
        self.assertTrue(formats.json_text(deep).startswith('{\n "a": {'))
        for text in ('{"a":' + deep + "}", nested_list(DEPTH + 1), nested_list(990)):
            with self.assertRaises(Refused) as caught:
                formats.json_text(text, "the file")
            self.assertEqual(caught.exception.message,
                             "the file nests JSON too deeply to re-indent; pass it as plain text instead")

    def test_value_depth_walks_without_recursion(self):
        self.assertFalse(util.value_depth_over(nested_dict(DEPTH)))
        self.assertTrue(util.value_depth_over(nested_dict(DEPTH + 1)))
        self.assertTrue(util.value_depth_over(nested_dict(100000)))  # far past any recursion limit
        self.assertFalse(util.value_depth_over({"a": [1, 2, {"b": "c"}], "d": "e"}))
        self.assertFalse(util.value_depth_over("text"))

    def test_loads_refuse_deep_nesting_with_one_error_on_every_version(self):
        for loads in (util.loads_strict, util.loads_record):
            self.assertIsInstance(loads(nested_list(DEPTH)), list)  # the limit itself still reads
            for text in (nested_list(DEPTH + 1), "[" * 100000, '{"a":' * 50000):
                with self.assertRaises(util.NestingError) as caught:
                    loads(text)
                self.assertIsInstance(caught.exception, ValueError)
                self.assertIsInstance(caught.exception, RecursionError)
                self.assertEqual(str(caught.exception), "nested too deeply")

    def test_store_reads_report_deep_nesting(self):
        root = os.path.join(self.tmp, FOLDER)
        os.makedirs(root)
        path = os.path.join(root, "deep.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(nested_list(DEPTH + 1))
        with self.assertRaises(DataError) as caught:
            store.read_json(path)
        self.assertEqual(caught.exception.message, "%s: not valid JSON: nested too deeply" % path)
        lines = os.path.join(root, "deep.jsonl")
        with open(lines, "w", encoding="utf-8") as fh:
            fh.write('{"id":"x"}\n{"id":"y","v":%s}\n' % nested_list(DEPTH + 5))
        rows, problems = store.read_jsonl_lines(lines)
        self.assertEqual([n for n, _row in rows], [1])
        self.assertEqual(problems, [(2, "not valid JSON: nested too deeply")])

    def test_mcp_lines_and_replies_past_the_limit(self):
        server = mcp_server.Server("query", cwd=self.tmp, env={}, err=io.StringIO())
        reply = server.handle_line(nested_list(DEPTH + 1))
        self.assertEqual(reply["error"], {"code": -32700, "message": "Parse error: nested too deep"})
        reply = server.handle_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping",
                                               "params": {"_meta": nested_dict(DEPTH - 3)}}))
        self.assertNotEqual((reply.get("error") or {}).get("code"), -32700)
        line = mcp_server._encode({"jsonrpc": "2.0", "id": 7, "result": nested_dict(DEPTH + 1)})
        self.assertEqual(json.loads(line), {"jsonrpc": "2.0", "id": 7, "error": {
            "code": -32603, "message": "Internal error: the reply could not be encoded (RecursionError)"}})
        fine = mcp_server._encode({"jsonrpc": "2.0", "id": 8, "result": nested_dict(DEPTH - 3)})
        self.assertEqual(json.loads(fine)["id"], 8)
        self.assertNotIn("error", json.loads(fine))


# topics for the interview, premise and decision tests ----------------------------------------------------------------
class TopicCase(_support.TempCase):
    def setUp(self):
        super().setUp()
        clear()
        parent = os.path.join(self.tmp, FOLDER)
        os.makedirs(parent)
        self.root = _support.init_topic(parent, "garden", "A community garden")
        self.repo = store.Repo.open(self.root)

    def onto(self):
        clear()
        return graph.Ontology.load(self.repo)

    def answer(self, q, text, ops=None, status="answered"):
        result = interview.answer(self.repo, q, text, status=status, ops=ops, apply=bool(ops))
        clear()
        return result

    def cli(self, *args):
        clear()
        code, out, err = _support.run_cli(list(args), repo=self.root)
        clear()
        return code, out, err

    def cli_json(self, *args):
        code, out, err = self.cli("--json", *args)
        self.assertEqual(code, 0, out + err)
        return json.loads(out)


def node(nid, kind, name, quote, summary=None):
    out = {"op": "add_node", "ref": "$" + nid.split(":", 1)[1], "node": {"id": nid, "kind": kind, "name": name},
           "basis": "stated", "prov": [{"quote": quote}]}
    if summary:
        out["node"]["summary"] = summary
    return out


def edge(src, rel, dst, quote):
    return {"op": "add_edge", "edge": {"src": src, "rel": rel, "dst": dst}, "basis": "stated",
            "prov": [{"quote": quote}]}


# C2: premises ----------------------------------------------------------------------------------------------------
class PremisePackTest(_support.TempCase):
    def test_core_declares_the_premise_kind_and_rests_on(self):
        reg = packs.load(None, {"packs": ["core", "discovery"]})
        kind = reg.kind("premise")
        self.assertEqual((kind["label"], kind["plural"], kind["dimension"]), ("Premise", "premises", "constraints"))
        self.assertEqual(reg.kind_key("assumption"), "premise")
        self.assertEqual(list(kind["fields"]), ["checked_on"])
        self.assertEqual(reg.inverse("rests_on"), "underpins")
        self.assertNotIn("underpins", reg.relations())  # the inverse is a label, never a second relation
        self.assertTrue(reg.allowed("rests_on", "goal", "premise"))
        self.assertTrue(reg.allowed("rests_on", "premise", "premise"))
        self.assertFalse(reg.allowed("rests_on", "premise", "goal"))
        self.assertEqual(reg.forward("rests_on"), ("rests_on", False))
        self.assertEqual(reg.problems(), [])
        core_only = packs.load(None, {"packs": ["core"]})
        self.assertEqual(core_only.problems(), [])
        self.assertEqual(core_only.kind_key("premise"), "premise")

    def test_checked_on_is_a_date(self):
        from ontokit import schema_lite

        reg = packs.load(None, {"packs": ["core"]})
        field = reg.fields("premise")["checked_on"]
        self.assertEqual(schema_lite.validate("2026-10-05", "checked_on", {"$defs": {"checked_on": field}}), [])
        self.assertNotEqual(schema_lite.validate("last spring", "checked_on", {"$defs": {"checked_on": field}}), [])

    def test_the_core_question_and_the_gap_template(self):
        reg = packs.load(None, {"packs": ["core", "discovery"]})
        by_id = {q["id"]: q for q in reg.questions()}
        q = by_id["q.constraints.premises"]
        self.assertEqual(q["ask"], "What are you assuming is true that, if it turned out wrong, would change the "
                                   "plan?")
        self.assertEqual((q["stage"], q["dimension"], q["fills"]["kinds"]), (5, "constraints", ["premise"]))
        self.assertEqual(by_id["q.gap.archived_premise"]["for_gap"], "archived_premise")
        self.assertIn("archived_premise", interview.ASKED_GAPS)
        self.assertEqual(needs.GAP_TYPES["archived_premise"]["scope"], "node")


class ArchivedPremiseTest(TopicCase):
    def setUp(self):
        super().setUp()
        text = "Our goal is a full harvest. That rests on the premise that the well keeps water all summer."
        self.answer("q.constraints.premises", text, [
            node("goal:harvest", "goal", "Full harvest", "a full harvest", "A harvest from every bed."),
            node("premise:well", "premise", "The well keeps water", "the well keeps water all summer",
                 "The well holds water through the summer."),
            edge("$harvest", "rests_on", "$well", "rests on the premise"),
        ])
        self.dec = ledger.decide(self.repo, "Does the well still hold water?", [], "No, it ran dry in July")["id"]

    def archive_premise(self, superseded_by=()):
        ops = [{"op": "archive", "id": "premise:well", "archived": {
            "reason": "the well ran dry in July", "decision": self.dec, "superseded_by": list(superseded_by)}}]
        return self.answer("q.deepen.change", "The well ran dry in July.", ops)

    def premise_gaps(self, nid="goal:harvest"):
        return [g for g in needs.needs(self.onto(), nid)["gaps"] if g["type"] == "archived_premise"]

    def test_neighbors_of_a_premise_list_what_rests_on_it(self):
        found = self.cli_json("neighbors", "premise:well")
        self.assertEqual([(i["id"], i["edge"]) for i in found["items"]], [("goal:harvest", "underpins")])
        _code, out, _err = self.cli("neighbors", "premise:well")
        self.assertIn("underpins", out)
        self.assertNotIn("supports", out)
        self.assertIn("goal:harvest", out)
        self.assertEqual(self.premise_gaps(), [])

    def test_an_archived_premise_with_an_active_dependent_is_a_gap(self):
        self.archive_premise()
        gaps = self.premise_gaps()
        self.assertEqual(len(gaps), 1)
        self.assertEqual((gaps[0]["severity"], gaps[0]["note"], gaps[0]["rel"]), (7, "premise:well", "rests_on"))
        self.assertIn("rests on The well keeps water, which was archived", gaps[0]["ask"])
        listed = self.cli_json("gaps", "--type", "archived_premise")
        self.assertEqual([(g.get("node"), g["type"], g["severity"]) for g in listed["gaps"]],
                         [("goal:harvest", "archived_premise", 7)])
        asked = [q for q in interview.next_questions(self.onto(), 20) if q["q"].startswith("q.gap.archived_premise")]
        # one question per premise (review round 5): a premise archived later is asked about too
        self.assertEqual([q["id"] for q in asked], ["q.gap.archived_premise.premise-well@goal:harvest"])
        # the user says the goal still holds: the gap settles
        self.answer("q.gap.archived_premise.premise-well@goal:harvest",
                    "The harvest goal still holds; we will haul water.")
        self.assertEqual(self.premise_gaps(), [])
        self.assertEqual([p for p in validate.validate(self.repo).problems], [])

    def test_a_replacement_premise_or_an_archived_dependent_settles_it(self):
        self.answer("q.constraints.premises", "We now assume the rain barrels fill each week.", [
            node("premise:barrels", "premise", "Rain barrels fill weekly", "the rain barrels fill each week",
                 "The barrels refill from the roof every week."),
            edge("goal:harvest", "rests_on", "$barrels", "rain barrels")])
        self.archive_premise(superseded_by=["premise:barrels"])
        self.assertEqual(self.premise_gaps(), [])

    def test_a_link_archived_on_its_own_was_dealt_with(self):
        onto = self.onto()
        eid = onto.edges_of("goal:harvest", "out", rels=["rests_on"])[0]["edge"]["id"]
        self.answer("q.deepen.more", "The harvest goal no longer depends on the well.", [
            {"op": "archive", "id": eid, "archived": {"reason": "no longer depends on the well",
                                                      "decision": self.dec, "superseded_by": []}}])
        self.archive_premise()
        self.assertEqual(self.premise_gaps(), [])


# C3: decisions that narrow ---------------------------------------------------------------------------------------
class NarrowsTest(TopicCase):
    def setUp(self):
        super().setUp()
        self.broad = ledger.decide(self.repo, "Which beds grow vegetables?", [], "The south beds")

    def test_a_narrowing_decision_keeps_both_active(self):
        narrow = ledger.decide(self.repo, "Which vegetables in the south beds?", [], "Tomatoes and beans",
                               narrows=self.broad["id"])
        self.assertEqual(narrow["narrows"], self.broad["id"])
        found = {d["id"]: d for d in ledger.all_decisions(self.repo).values()}
        self.assertEqual(found[self.broad["id"]]["status"], "active")
        self.assertEqual(found[narrow["id"]]["status"], "active")
        self.assertNotIn("narrows", found[self.broad["id"]])  # a decision without it keeps its old shape
        self.assertEqual(ledger.narrowed_by(found), {self.broad["id"]: [narrow["id"]]})
        listed = self.cli_json("decisions")
        by_id = {d["id"]: d for d in listed["decisions"]}
        self.assertEqual(by_id[self.broad["id"]]["narrowed_by"], [narrow["id"]])
        _code, out, _err = self.cli("decisions")
        self.assertIn("narrows %s" % self.broad["id"], out)
        self.assertIn("narrowed by %s" % narrow["id"], out)
        self.assertEqual(validate.validate(self.repo).problems, [])
        # the same decision narrowing nothing is a different decision
        again = ledger.decide(self.repo, "Which vegetables in the south beds?", [], "Tomatoes and beans")
        self.assertNotEqual(again["id"], narrow["id"])
        same = ledger.decide(self.repo, "Which vegetables in the south beds?", [], "Tomatoes and beans",
                             narrows=self.broad["id"])
        self.assertEqual(same["id"], narrow["id"])

    def test_the_target_must_exist_and_be_active(self):
        with self.assertRaises(NotFound):
            ledger.decide(self.repo, "Which vegetables?", [], "Beans", narrows="dec-20260928-nothing-0000")
        newer = ledger.decide(self.repo, "Which beds grow vegetables?", [], "The north beds",
                              supersedes=self.broad["id"])
        with self.assertRaises(Refused) as caught:
            ledger.decide(self.repo, "Which vegetables?", [], "Beans", narrows=self.broad["id"])
        self.assertIn("superseded by %s" % newer["id"], caught.exception.message)
        with self.assertRaises(UsageError):
            ledger.decide(self.repo, "Which vegetables?", [], "Beans", narrows=newer["id"], supersedes=newer["id"])
        code, out, err = self.cli("decide", "--question", "Which vegetables?", "--chosen", "Beans", "--narrows",
                                  newer["id"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("narrows %s" % newer["id"], out)

    def test_validate_reports_a_missing_target(self):
        path = ledger.decision_path(self.repo, self.broad["id"])
        record = dict(self.broad, narrows="dec-20260928-gone-0000")
        store.write_bytes(path, util.canonical_bytes(record))
        clear()
        problems = [p for p in validate.validate(self.repo).problems if p.code == "P21"]
        self.assertEqual([p.message for p in problems], ["narrows dec-20260928-gone-0000, which is not in the ledger"])

    def test_superseding_a_narrowed_decision_warns(self):
        narrow = ledger.decide(self.repo, "Which vegetables in the south beds?", [], "Tomatoes and beans",
                               narrows=self.broad["id"])
        code, out, err = self.cli("decide", "--question", "Which beds grow vegetables?", "--chosen", "The north beds",
                                  "--supersedes", self.broad["id"])
        self.assertEqual(code, 0, out + err)
        newer = [d for d in ledger.all_decisions(self.repo).values() if d.get("supersedes") == self.broad["id"]][0]
        self.assertIn("%s still narrows %s; record a decision that supersedes it and narrows %s"
                      % (narrow["id"], self.broad["id"], newer["id"]), out)
        report = validate.validate(self.repo)
        self.assertEqual(report.problems, [])  # superseding stays legal; the narrower is flagged, not refused
        found = [p for p in report.warnings if p.code == "W10"]
        self.assertEqual([(p.file, p.message) for p in found], [(
            "ledger/decisions/%s.json" % narrow["id"],
            "narrows %s, which is superseded by %s; record a decision that supersedes %s and narrows %s"
            % (self.broad["id"], newer["id"], narrow["id"], newer["id"]))])
        replaced = ledger.decide(self.repo, "Which vegetables in the north beds?", [], "Tomatoes and beans",
                                 supersedes=narrow["id"], narrows=newer["id"])
        self.assertEqual(replaced["narrows"], newer["id"])
        self.assertNotIn("W10", [p.code for p in validate.validate(self.repo).warnings])

    def test_a_plain_supersede_names_no_narrower(self):
        result = self.cli_json("decide", "--question", "Which beds grow vegetables?", "--chosen", "The north beds",
                               "--supersedes", self.broad["id"])
        self.assertNotIn("still_narrowing", result)

    def test_mcp_decide_takes_narrows(self):
        server = mcp_server.Server("full", repo=self.root, cwd=self.tmp, env={}, err=io.StringIO())
        result = _support.mcp_call(server, "onto_decide", question="Which vegetables in the south beds?",
                                   chosen="Beans", narrows=self.broad["id"], format="json")
        self.assertFalse(result["isError"], result)
        self.assertEqual(json.loads(result["content"][0]["text"])["decision"]["narrows"], self.broad["id"])
        listed = mcp_server.published_schema(server.tools["onto_decide"])
        self.assertEqual(listed["properties"]["narrows"], {"type": "string"})


# B3: next and answer -------------------------------------------------------------------------------------------
PROGRESS_KEYS = ["answered", "quick_answered", "quick_total", "skipped", "stage", "stage_name"]


class NextItemsTest(TopicCase):
    def test_every_item_carries_why_options_follow_ups_and_progress(self):
        result = self.cli_json("next", "--n", "20")
        self.assertTrue(result["questions"])
        for item in result["questions"]:
            self.assertIsInstance(item["why"], str)
            self.assertTrue(item["why"], item["id"])
            self.assertIsInstance(item["options"], list)
            self.assertIsInstance(item["follow_ups"], list)
            self.assertEqual(sorted(item["progress"]), PROGRESS_KEYS)
        first = result["questions"][0]
        self.assertEqual(first["progress"], {"stage": 0, "stage_name": "frame", "quick_answered": 0,
                                             "quick_total": 5, "answered": 0, "skipped": 0})
        self.assertEqual(first["follow_ups"], ["q.frame.goal"])
        api = interview.next_questions(self.onto(), 1)
        self.assertEqual(api[0]["progress"], first["progress"])

    def test_the_compact_text_and_mcp_show_why_and_follow_ups(self):
        _code, compact, _err = self.cli("next")
        self.assertIn("stage 0 frame | quick start 0/5 | answered 0, skipped 0", compact)
        self.assertIn("   why: The first answers come from your view", compact)
        self.assertIn("   follow-ups: q.frame.goal", compact)
        server = mcp_server.Server("full", repo=self.root, cwd=self.tmp, env={}, err=io.StringIO())
        text = _support.mcp_call(server, "onto_next")["content"][0]["text"]
        self.assertIn("   why: ", text)
        self.assertIn("   follow-ups: q.frame.goal", text)
        data = json.loads(_support.mcp_call(server, "onto_next", format="json")["content"][0]["text"])
        self.assertEqual(sorted(data["questions"][0]["progress"]), PROGRESS_KEYS)

    def test_options_are_listed_in_text(self):
        self.answer("q.deepen.more", "We want a question with options.", [
            {"op": "add_question", "question": {
                "ask": "How often should the beds be checked?", "dimension": "process", "fills": {"kinds": []},
                "follow_ups": [], "id": "q.local.check", "options": [{"id": "weekly", "label": "Every week"},
                                                                     "monthly"],
                "priority": 500, "quick": False, "repeatable": False, "stage": 0, "until": [], "when": [],
                "why": "Checks keep the beds healthy."}}])
        found = [q for q in interview.next_questions(self.onto(), 20) if q["q"] == "q.local.check"]
        self.assertEqual(found[0]["options"], [{"id": "weekly", "label": "Every week"}, "monthly"])
        _code, out, _err = self.cli("next", "--n", "20")
        self.assertIn("   options: weekly=Every week; monthly=monthly", out)


class AboutTest(TopicCase):
    def setUp(self):
        super().setUp()
        self.answer("q.frame.you", "I am the steward of the garden.", [
            node("role:steward", "role", "Steward", "the steward of the garden")])

    def test_about_puts_the_node_gaps_and_follow_ups_first(self):
        result = self.cli_json("next", "--about", "steward", "--n", "5")
        self.assertEqual(result["about"], {"id": "role:steward", "first": 3})
        ids = [q["id"] for q in result["questions"]]
        self.assertEqual(ids[:3], ["q.gap.orphan@role:steward", "q.gap.thin@role:steward", "q.frame.goal"])
        self.assertTrue(all(q.get("about") for q in result["questions"][:3]))
        self.assertFalse(any(q.get("about") for q in result["questions"][3:]))
        plain = self.cli_json("next", "--n", "5")
        self.assertNotIn("about", plain)
        self.assertEqual(plain["questions"][0]["id"], "q.frame.goal")  # the quick start stays first otherwise
        _code, out, _err = self.cli("next", "--about", "role:steward")
        self.assertIn("| about role:steward: 3 first", out)
        api = interview.next_questions(self.onto(), 2, about="role:steward")
        self.assertEqual([q["id"] for q in api], ids[:2])

    def test_an_unknown_node_is_an_error(self):
        code, out, err = self.cli("next", "--about", "nothing-like-this")
        self.assertNotEqual(code, 0)
        self.assertIn("not in the ontology", out + err)
        with self.assertRaises(NotFound):
            interview.next_questions(self.onto(), 1, about="nothing-like-this")


class BestNextTest(TopicCase):
    def test_a_follow_up_of_the_answered_question_comes_first(self):
        result = self.answer("q.frame.you", "I am the steward of the garden.", [
            node("role:steward", "role", "Steward", "the steward of the garden")])
        self.assertEqual(result["next"]["id"], "q.frame.goal")
        self.assertEqual(result["next"]["reason"], "a follow-up of q.frame.you")
        self.assertEqual(sorted(result["next"]["progress"]), PROGRESS_KEYS)
        self.assertEqual(result["next"]["progress"]["quick_answered"], 1)

    def test_then_a_gap_on_a_node_the_answer_added(self):
        result = self.answer("q.vocab.terms", "Mulch is a layer of straw that keeps the soil moist.", [
            node("term:mulch", "term", "Mulch", "Mulch is a layer of straw")])
        self.assertEqual(result["next"]["node"], "term:mulch")
        self.assertTrue(result["next"]["id"].endswith("@term:mulch"), result["next"])
        self.assertEqual(result["next"]["reason"], "a gap on term:mulch, which this answer added")

    def test_else_the_top_ranked_question_and_never_the_same_one(self):
        result = self.answer("q.frame.you", "", status="skipped")
        self.assertEqual(result["next"]["reason"], "the top-ranked question")
        self.assertNotEqual(result["next"]["id"], "q.frame.you")
        self.assertEqual(result["next"]["id"], interview.next_questions(self.onto(), 1)[0]["id"])

    def test_the_answer_text_names_it(self):
        code, out, err = self.cli("answer", "q.frame.you", "I keep the garden going.")
        self.assertEqual(code, 0, out + err)
        self.assertIn('ask next: q.frame.goal  "What is the main goal for A community garden?', out)
        self.assertIn("(a follow-up of q.frame.you)", out)
        self.assertTrue(out.rstrip().endswith("Next: onto next"), out)


# B3: resuming ----------------------------------------------------------------------------------------------------
class ResumeTest(TopicCase):
    def test_log_last_shows_the_last_checkpoint(self):
        _code, out, _err = self.cli("log", "--last")
        self.assertIn("no checkpoint yet", out)
        self.assertEqual(self.cli_json("log", "--last")["last_checkpoint"], None)
        ledger.checkpoint(self.repo, ["covered the frame"], ["ask about the beds"], ["who waters on weekends?"])
        ledger.checkpoint(self.repo, ["covered the people"], ["ask about the tools"], [])
        _code, out, _err = self.cli("log", "--last")
        self.assertIn("resume from here", out)
        self.assertIn("    - covered the people", out)
        self.assertIn("    - ask about the tools", out)
        self.assertNotIn("covered the frame", out)
        last = self.cli_json("log", "--last")["last_checkpoint"]
        self.assertEqual((last["done"], last["next"], last["open_questions"]),
                         (["covered the people"], ["ask about the tools"], []))

    def test_the_hook_names_the_checkpoint_on_the_next_line(self):
        before = hook.lines_for(self.root, {})
        self.assertEqual(len(before), 4)
        self.assertNotIn("checkpoint", before[-1])
        self.assertTrue(before[-1].endswith("(onto_next, then onto_answer)."), before[-1])
        ledger.checkpoint(self.repo, ["covered the frame"], ["ask about the beds"], [])
        after = hook.lines_for(self.root, {})
        self.assertEqual(len(after), 4)
        self.assertEqual(after[-1], "Next: use the onto-interview skill to continue the interview (onto_next, then "
                                    "onto_answer); resume from the last checkpoint (onto log --last).")
        self.assertLessEqual(len("\n".join(after)) + 1, hook.MAX_CHARS)
        self.assertTrue(all(len(line) <= hook.LINE_WIDTH for line in after))
        self.assertTrue(hook.has_checkpoint(self.repo))
