"""Review round 5 of kit 0.2.0: one regression test per confirmed finding that a code change fixed.

The interview: an archived-premise question is asked once per premise and a drafted-link question once per set of
drafted links; compact next names the fields an answer sets; an answer about a person never names them in the
export. The depth guard: a deeply nested list formats the same on every Python version. Erase: the finder lists the
agent's scratch copies in .onto/. Packs and decisions: a clashing local kind gets a way out, and decide reads its
words from files. Personal data: typographic separators and fullwidth forms do not hide a number. The viewer shows
which decision narrows which. Doctor: its version line is the checked folder's, and no printed URL shows a query
token. Setup: the goal is quoted as stored and can carry a confirmed name, a skipped goal is logged, every value is
checked for NUL once, a shared topic keeps its origin, an unrelated main is refused, the branch fix works in another
worktree, a conditional git identity is found, kit URLs are normalized, a failed commit names its cause, the folder
prompt asks again, a dragged folder is unescaped, the launchers work through a symlink and read a relative answers
file from the caller's folder, slow steps say so, and printed commands quote for cmd.exe on Windows.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from tests import _support
from tests import test_field_learnings as learnings
from tests.test_field_learnings import edge, node
from tests.test_setup import KIT_URL, SetupCase, make_template
from ontokit import doctor, graph, interview, ledger, needs, onboard, render, store, util, validate
from ontokit.errors import UsageError


def clear():
    graph.clear_cache()
    store.clear_cache()


def inferred_edge(src, rel, dst):
    return {"op": "add_edge", "edge": {"src": src, "rel": rel, "dst": dst}, "basis": "inferred", "prov": []}


# the interview ---------------------------------------------------------------------------------------------------
class LaterPremiseTest(learnings.TopicCase):
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

    def archive_premise(self):
        ops = [{"op": "archive", "id": "premise:well", "archived": {
            "reason": "the well ran dry in July", "decision": self.dec, "superseded_by": []}}]
        return self.answer("q.deepen.change", "The well ran dry in July.", ops)

    def premise_gaps(self, nid="goal:harvest"):
        return [g for g in needs.needs(self.onto(), nid)["gaps"] if g["type"] == "archived_premise"]

    def test_answering_one_premise_leaves_a_later_one_open(self):
        self.answer("q.constraints.premises", "The harvest also rests on the premise that bees visit the beds.", [
            node("premise:bees", "premise", "Bees visit the beds", "bees visit the beds",
                 "Pollinators visit the beds each week."),
            edge("goal:harvest", "rests_on", "$bees", "rests on the premise")])
        self.archive_premise()
        self.assertEqual([g["note"] for g in self.premise_gaps()], ["premise:well"])
        asked = [q["id"] for q in interview.next_questions(self.onto(), 30) if "archived_premise" in q["q"]]
        self.assertEqual(asked, ["q.gap.archived_premise.premise-well@goal:harvest"])
        self.answer(asked[0], "The harvest goal still holds; we will haul water.")
        self.assertEqual(self.premise_gaps(), [])
        dec = ledger.decide(self.repo, "Do bees still visit?", [], "No, the colony collapsed")["id"]
        self.answer("q.deepen.change", "The bee colony collapsed.", [{"op": "archive", "id": "premise:bees",
                    "archived": {"reason": "the colony collapsed", "decision": dec, "superseded_by": []}}])
        self.assertEqual([g["note"] for g in self.premise_gaps()], ["premise:bees"])
        asked = [q["id"] for q in interview.next_questions(self.onto(), 30) if "archived_premise" in q["q"]]
        self.assertEqual(asked, ["q.gap.archived_premise.premise-bees@goal:harvest"])

    def test_the_bare_id_answers_the_one_open_premise(self):
        self.archive_premise()
        self.answer("q.gap.archived_premise@goal:harvest", "The harvest goal still holds; we will haul water.")
        self.assertEqual(self.premise_gaps(), [])
        self.assertEqual(validate.validate(self.repo).problems, [])

    def test_two_open_premises_need_the_full_id(self):
        self.answer("q.constraints.premises", "The harvest also rests on the premise that bees visit the beds.", [
            node("premise:bees", "premise", "Bees visit the beds", "bees visit the beds",
                 "Pollinators visit the beds each week."),
            edge("goal:harvest", "rests_on", "$bees", "rests on the premise")])
        self.archive_premise()
        dec = ledger.decide(self.repo, "Do bees still visit?", [], "No, the colony collapsed")["id"]
        self.answer("q.deepen.change", "The bee colony collapsed.", [{"op": "archive", "id": "premise:bees",
                    "archived": {"reason": "the colony collapsed", "decision": dec, "superseded_by": []}}])
        with self.assertRaises(UsageError) as caught:
            self.answer("q.gap.archived_premise@goal:harvest", "It still holds.")
        self.assertIn("q.gap.archived_premise.premise-bees@goal:harvest", caught.exception.message)
        self.assertIn("q.gap.archived_premise.premise-well@goal:harvest", caught.exception.message)

    def test_a_premise_id_that_does_not_fit_gets_a_hash(self):
        self.assertEqual(needs.id_detail("premise:well"), "premise-well")
        long = needs.id_detail("premise:" + "a" * 60)
        self.assertRegex(long, r"^h[0-9a-f]{10}\Z")
        self.assertEqual(long, needs.id_detail("premise:" + "a" * 60))


class LaterDraftLinkTest(learnings.TopicCase):
    def drafted(self):
        return [q["id"] for q in interview.next_questions(self.onto(), 30) if "draft_link" in q["q"]]

    def test_a_link_drafted_after_the_answer_brings_the_question_back(self):
        self.answer("q.people.key", "Ana runs the garden. Ben waters it. Cy owns the land.", [
            node("person:ana", "person", "Ana", "Ana runs the garden"),
            node("person:ben", "person", "Ben", "Ben waters it"),
            node("person:cy", "person", "Cy", "Cy owns the land")])
        self.answer("q.deepen.more", "Ana works with Ben, I think.",
                    [inferred_edge("person:ana", "related_to", "person:ben")])
        first = self.drafted()
        self.assertEqual(len(first), 1, first)
        self.assertRegex(first[0], r"^q\.gap\.draft_link\.[0-9a-f]{8}@person:ana\Z")
        self.answer(first[0], "I am not sure yet, leave it.")
        self.assertEqual(self.drafted(), [])
        self.answer("q.deepen.more", "Ana probably knows Cy too.",
                    [inferred_edge("person:ana", "related_to", "person:cy")])
        second = self.drafted()
        self.assertEqual(len(second), 1, second)
        self.assertNotEqual(second, first)
        gap = [g for g in needs.needs(self.onto(), "person:ana")["gaps"] if g["type"] == "draft_link"][0]
        self.assertEqual(gap["note"], "related to Ben; related to Cy")
        # the bare id still answers the one open set
        self.answer("q.gap.draft_link@person:ana", "Leave both for now.")
        self.assertEqual(self.drafted(), [])


class FillsShownTest(learnings.TopicCase):
    def test_compact_next_names_the_fields_an_answer_sets(self):
        _code, out, _err = self.cli("next", "--n", "5")
        lines = out.splitlines()
        at = [i for i, line in enumerate(lines) if " q.frame.deliverable " in line]
        self.assertTrue(at, out)
        block = lines[at[0] + 1:at[0] + 5]
        self.assertIn("   sets: audience (attrs of the record the answer adds)", block)


class LocalTitleTest(learnings.TopicCase):
    def test_an_answer_about_a_person_never_names_them_in_the_export(self):
        self.answer("q.people.key", "Dave sells the honey at the market.", [
            node("person:dave", "person", "Dave", "Dave sells the honey"),
            node("role:seller", "role", "Seller", "sells the honey at the market"),
            edge("$dave", "member_of", "$seller", "Dave sells the honey"),
            edge("$seller", "part_of", "topic:garden", "sells the honey at the market")])
        onto = self.onto()
        self.assertEqual(onto.node("person:dave").get("visibility"), "local")
        result = self.answer("q.gap.missing_relation.works_on.out@person:dave",
                             "He sells honey; the seller role supports the garden.", [
                                 edge("role:seller", "supports", "topic:garden", "the seller role supports the garden")])
        src = result["answer"]["src"]
        entry = self.onto().sources[src]
        self.assertEqual(entry["title"], "Answer to q.gap.missing_relation.works_on.out on a local record")
        code, out, err = self.cli("build")
        self.assertEqual(code, 0, out + err)
        with open(os.path.join(self.root, "build", "export.json"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn(src, text)
        self.assertNotIn("person:dave", text)

    def test_an_older_title_is_scrubbed_at_build(self):
        from ontokit import build

        self.assertEqual(build._scrub_title("Answer to q.gap.x on person:dave (ans-1)", {"person:dave"}),
                         "Answer to q.gap.x on a local record (ans-1)")
        self.assertEqual(build._scrub_title("Notes on person:davey", {"person:dave"}), "Notes on person:davey")


# the depth guard ---------------------------------------------------------------------------------------------------
class DeepValueTest(learnings.TopicCase):
    def test_fmt_walks_nested_lists_without_recursion(self):
        deep = 1
        for _ in range(util.JSON_MAX_DEPTH):
            deep = [deep]
        limit = sys.getrecursionlimit()
        sys.setrecursionlimit(100)
        try:
            self.assertEqual(render.fmt(deep), "1")
        finally:
            sys.setrecursionlimit(limit)
        self.assertEqual(render.fmt([1, [], [["a", None]], True, {"b": 1}]), '1, , a, null, true, {"b":1}')

    def test_a_deep_attrs_value_gets_the_same_refusal_on_every_version(self):
        note = os.path.join(self.root, "inbox", "notes.txt")
        os.makedirs(os.path.dirname(note), exist_ok=True)
        with open(note, "w", encoding="utf-8") as fh:
            fh.write("Some notes about the beds.\n")
        src = self.cli_json("ingest", note, "--title", "notes")["source"]["id"]
        depth = 400
        value = "[" * depth + "1" + "]" * depth
        text = ('{"source":"%s","summary":"deep","ops":[{"op":"add_node","basis":"inferred","node":{"kind":"term",'
                '"name":"Deep","summary":"A deep one.","attrs":{"x":%s}},"prov":[{"src":"%s","loc":"L1-L1"}]}]}'
                % (src, value, src))
        path = os.path.join(self.root, ".onto", "deep.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        done = subprocess.run([sys.executable, os.path.join(_support.PLUGIN_DIR, "bin", "onto"), "propose",
                               "--proposal", "@" + path, "--repo=%s" % self.root],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertNotIn("internal error", done.stdout + done.stderr)
        self.assertNotIn("RecursionError", done.stdout + done.stderr)
        self.assertIn("fix every problem and propose again", done.stdout + done.stderr)


# packs and decisions -----------------------------------------------------------------------------------------------
class PackClashTest(learnings.TopicCase):
    def test_a_local_kind_named_like_the_pack_gets_a_way_out(self):
        code, out, err = self.cli("answer", "q.constraints.rules", "We track risks.", "--ops",
                                  json.dumps([{"op": "add_kind", "name": "risk", "kind": {"label": "Risk"}}]),
                                  "--apply", "--confirm")
        self.assertEqual(code, 0, out + err)
        code, out, err = self.cli("pack", "add", "assessment")
        self.assertEqual(code, 1, out + err)
        self.assertIn("P20 duplicate kind 'risk'", out + err)
        self.assertIn("the local pack declares kind risk too", out + err)
        self.assertIn("take it out of packs/local.pack.json by hand", out + err)
        path = os.path.join(self.root, "packs", "local.pack.json")
        with open(path, encoding="utf-8") as fh:
            pack = json.load(fh)
        del pack["kinds"]["risk"]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(pack, fh)
        code, out, err = self.cli("pack", "add", "assessment")
        self.assertEqual(code, 0, out + err)


class DecideFromFileTest(learnings.TopicCase):
    def test_the_rationale_and_choice_come_from_files(self):
        why = os.path.join(self.root, ".onto", "why.txt")
        os.makedirs(os.path.dirname(why), exist_ok=True)
        with open(why, "w", encoding="utf-8") as fh:
            fh.write("Nah, not bothered, it's only $bees and `hives`.\n")
        choice = os.path.join(self.root, ".onto", "choice.txt")
        with open(choice, "w", encoding="utf-8") as fh:
            fh.write("None of them, \"thanks\".\n")
        found = self.cli_json("decide", "--question", "Which extra built-in packs should it use?", "--options",
                              "assessment=yes,none=no,later=later", "--chosen", "none", "--rationale-file", why,
                              "--chosen-text-file", choice, "--scope", "setup")["decision"]
        self.assertEqual(found["rationale"], "Nah, not bothered, it's only $bees and `hives`.")
        self.assertEqual(found["chosen_text"], 'None of them, "thanks".')

    def test_an_at_file_rationale_is_refused_not_stored(self):
        why = os.path.join(self.root, ".onto", "why.txt")
        os.makedirs(os.path.dirname(why), exist_ok=True)
        with open(why, "w", encoding="utf-8") as fh:
            fh.write("Because.\n")
        code, out, err = self.cli("decide", "--question", "Keep it?", "--chosen", "yes", "--rationale", "@" + why)
        self.assertEqual(code, 2, out + err)
        self.assertIn("--rationale-file", err)
        self.assertFalse(os.listdir(os.path.join(self.root, "ledger", "decisions")))
        code, out, err = self.cli("decide", "--question", "Keep it?", "--chosen", "yes", "--rationale", "x",
                                  "--rationale-file", why)
        self.assertEqual(code, 2, out + err)
        self.assertIn("not both", err)


# doctor -----------------------------------------------------------------------------------------------------------
class DoctorVersionLineTest(SetupCase):
    def test_the_version_line_is_the_checked_folders_not_onto_repos(self):
        other = os.path.join(self.place, "other")
        code, obj, err = self.setup("--yes", "--plugin", "skip", "--launch", "none", "--title", "Other topic",
                                    "--new", other)
        self.assertEqual(code, 0, (obj, err))
        mine = os.path.join(self.place, "mine")
        code, obj, err = self.setup("--yes", "--plugin", "skip", "--launch", "none", "--title", "Mine",
                                    "--new", mine)
        self.assertEqual(code, 0, (obj, err))
        with mock.patch.dict(os.environ, {"ONTO_REPO": other}):
            old = os.getcwd()
            os.chdir(self.template_root)
            try:
                code, out, err = _support.run_cli(["doctor"])
            finally:
                os.chdir(old)
            self.assertEqual(out.splitlines()[0], "no topic", out)
            self.assertTrue(out.splitlines()[1].startswith("doctor: template at"), out)
            self.assertNotIn("other", out.splitlines()[0])
            os.chdir(mine)
            try:
                code, out, err = _support.run_cli(["doctor"])
            finally:
                os.chdir(old)
            self.assertTrue(out.splitlines()[0].startswith("mine "), out)
            self.assertIn("doctor: topic at", out.splitlines()[1])


class QueryTokenTest(SetupCase):
    TOKEN = "0123456789abcdef0123456789abcdef"

    def test_doctor_and_setup_never_print_a_query_token(self):
        target = os.path.join(self.place, "t")
        code, obj, err = self.setup("--yes", "--plugin", "skip", "--launch", "none", "--title", "Bee keeping",
                                    "--new", target)
        self.assertEqual(code, 0, (obj, err))
        _support.git(target, "remote", "set-url", "kit",
                     "https://git.example.net/team/kit.git?access_token=%s" % self.TOKEN)
        code, out, err = _support.run_cli(["doctor"], repo=target)
        self.assertNotIn(self.TOKEN, out + err)
        self.assertIn("kit https://git.example.net/team/kit.git?<hidden>", out)
        _support.git(target, "remote", "set-url", "kit", KIT_URL)
        _support.git(target, "remote", "add", "origin",
                     "https://git.example.net/team/topic.git?private_token=%s" % self.TOKEN)
        code, obj, err = self.setup("--origin", "https://git.example.net/team/other.git", "--plugin", "skip",
                                    "--launch", "none", cwd=target)
        self.assertNotIn(self.TOKEN, json.dumps(obj) + err)
        branch = [s for s in obj["steps"] if s["id"] == "branch"][0]
        self.assertIn("origin already points at https://git.example.net/team/topic.git?<hidden>", branch["detail"])
        self.assertEqual(doctor.safe_url("https://example.invalid/%s/x.git" % _support.fake_secret("github")),
                         doctor.HIDDEN_URL)


# the viewer --------------------------------------------------------------------------------------------------------
from tests import test_build_release as viewer_tests  # noqa: E402


@unittest.skipIf(viewer_tests.NODE is None, "node is not installed; the viewer DOM checks need it")
class ViewerNarrowsTest(viewer_tests.Base):
    page = viewer_tests.ViewerDomTest.page  # the DOM harness, without running that class's own tests again

    def test_a_narrowing_decision_is_linked_both_ways(self):
        from ontokit import build

        root = self.mini()
        repo = self.repo(root)
        broad = ledger.decide(repo, "How often do we water the beds?", [], "every morning",
                              scope=["process:watering"])["id"]
        narrow = ledger.decide(repo, "How often in the hot weeks?", [], "morning and evening",
                               scope=["process:watering"], narrows=broad)["id"]
        clear()
        got = build._node_decisions(self.repo(root), ["process:watering"])["items"]
        self.assertEqual(got[narrow].get("narrows"), broad)
        self.assertEqual(got[broad].get("narrowed_by"), [narrow])
        build.write(self.repo(root), html=True)
        for page in self.page(root, "#kind/process", [{"peek": "#node/process:watering"}])[1:]:
            text = " | ".join(page["peek"])
            self.assertIn("Narrows " + broad, text)
            self.assertIn("Narrowed by " + narrow, text)
        lines = " | ".join(self.page(root, "#node/process:watering")[0]["lines"])
        self.assertIn("Narrows " + broad, lines)


# erase -------------------------------------------------------------------------------------------------------------
class EraseFindScratchTest(learnings.TopicCase):
    def test_find_lists_the_agents_scratch_copies_on_the_cli_only(self):
        scratch = os.path.join(self.root, ".onto")
        os.makedirs(scratch, exist_ok=True)
        with open(os.path.join(scratch, "answer.txt"), "w", encoding="utf-8") as fh:
            fh.write("Pat Green runs the watering rota.\n")
        found = self.cli_json("erase", "--find", "Pat Green")["find"]
        self.assertEqual([h["file"] for h in found["hits"]], [".onto/answer.txt"])
        self.assertIn("not in git: delete the file", found["hits"][0]["what"])
        os.remove(os.path.join(scratch, "answer.txt"))
        _code, out, _err = self.cli("erase", "--find", "Pat Green")
        self.assertIn("imports, inbox/ and .onto/", out)
        with open(os.path.join(scratch, "ops.json"), "w", encoding="utf-8") as fh:
            fh.write('[{"quote": "Pat Green"}]\n')
        from ontokit import mutate

        self.assertEqual(mutate.find_text(self.repo, "Pat Green", raw=False)["hits"], [])


# personal data -----------------------------------------------------------------------------------------------------
TYPOGRAPHIC = [  # (text, the kind it holds): separators a word processor, a PDF or a web page writes
    ("Call her on 617\u2013555\u20130142.", "phone"),
    ("She gave 617\u2011555\u20110142", "phone"),
    ("Call her on 617\u00a0555\u00a00142", "phone"),
    ("Call +44\u00a07700\u00a0900123", "phone"),
    ("Her mobile is 07700\u00a0900123", "phone"),
    ("SSN 078\u201305\u20131120", "government_id"),
    ("Lives at 12\u00a0Elm Street", "address"),
    ("Phone \uff16\uff11\uff17-555-0142", "phone"),
    ("pat.green\uff20plotmail.org", "email"),
]


class TypographicSeparatorTest(learnings.TopicCase):
    def test_redact_finds_numbers_written_with_typographic_separators(self):
        from ontokit import sanitize

        policy = {"personal": {k: "redact" for k in sanitize.PERSONAL_KINDS}}
        for text, kind in TYPOGRAPHIC:
            clean, counts = sanitize.sanitize_text(text, policy)
            self.assertEqual(counts, {kind: 1}, (text, clean))
            self.assertIn("[redacted:%s]" % kind, clean)
        prose = "An en \u2013 dash in prose, and 3\u20134 beds by the\u00a0shed."
        self.assertEqual(sanitize.sanitize_text(prose, policy), (prose, {}))

    def test_refuse_refuses_them_on_ingest(self):
        from ontokit import sanitize

        manifest = os.path.join(self.root, "ontology.json")
        with open(manifest, encoding="utf-8") as fh:
            data = json.load(fh)
        data.setdefault("policy", {})["personal"] = {k: "refuse" for k in sanitize.PERSONAL_KINDS}
        with open(manifest, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        clear()
        for text, kind in TYPOGRAPHIC:
            note = os.path.join(self.root, "inbox", "note.txt")
            os.makedirs(os.path.dirname(note), exist_ok=True)
            with open(note, "w", encoding="utf-8") as fh:
                fh.write("Interview 8. %s\n" % text)
            code, out, err = self.cli("ingest", note, "--title", "Interview 8 note")
            self.assertEqual(code, 1, (text, out, err))
            self.assertIn("personal data the policy refuses (%s); nothing was stored" % kind, out + err)


# setup ------------------------------------------------------------------------------------------------------------
def rows(root, name):
    found, _bad = store.read_jsonl(os.path.join(root, "graph", name))
    return found


class SetupRedactedGoalTest(SetupCase):
    def test_a_redacted_goal_answer_stays_a_stated_goal(self):
        target = os.path.join(self.place, "bees")
        data = {"setup": {"title": "Bee keeping", "personal": "redact",
                          "summary": "Our hives; write to jo@hivemail.org with news."},
                "answers": [{"q": "q.frame.goal",
                             "text": "Decide whether to split the hives this spring; contact me at jo@hivemail.org."}]}
        code, obj, err = self.setup("--yes", "--plugin", "skip", "--launch", "none", "--new", target,
                                    "--answers", json.dumps(data))
        self.assertEqual(code, 0, (obj, err))
        goals = [n for n in rows(target, "nodes.jsonl") if n["kind"] == "goal"]
        self.assertEqual(len(goals), 1, goals)
        goal = goals[0]
        self.assertEqual((goal["id"], goal["name"]), ("goal:decide-whether-to-split-the-hives-this-spring",
                                                      "Decide whether to split the hives this spring"))
        self.assertEqual((goal["status"], goal["trust"]), ("confirmed", "user"))
        self.assertTrue(any(p.get("quote") for p in goal["prov"]), goal["prov"])
        self.assertNotIn("jo@hivemail.org", json.dumps(rows(target, "nodes.jsonl")))
        part_of = [e for e in rows(target, "edges.jsonl") if e["src"] == goal["id"]]
        self.assertEqual([e["status"] for e in part_of], ["confirmed"])
        root = [n for n in rows(target, "nodes.jsonl") if n["id"] == "topic:bee-keeping"][0]
        self.assertEqual(root["summary"], "Our hives; write to [redacted:email] with news.")

    def test_the_goal_name_stops_before_a_redaction(self):
        self.assertEqual(onboard._before_redaction("Split the hives; mail [redacted:email] first."),
                         "Split the hives")
        self.assertEqual(onboard._before_redaction("[redacted:email] knows the plan."),
                         "[redacted:email] knows the plan.")
        self.assertEqual(onboard._before_redaction("Split the hives."), "Split the hives.")


class SetupGoalNameTest(SetupCase):
    def test_a_confirmed_name_names_the_goal_and_fillers_are_dropped(self):
        target = os.path.join(self.place, "apiary")
        reply = "Like when I last did a varroa treatment on each hive. Keep track of the usual stuff."
        data = {"answers": [{"q": "q.frame.goal", "text": reply, "name": "Track hive treatments"}]}
        code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target,
                                    "--answers", json.dumps(data))
        self.assertEqual(code, 0, (obj, err))
        goal = [n for n in rows(target, "nodes.jsonl") if n["kind"] == "goal"][0]
        self.assertEqual((goal["id"], goal["name"], goal["status"]),
                         ("goal:track-hive-treatments", "Track hive treatments", "confirmed"))
        self.assertEqual(goal["summary"], reply)
        self.assertEqual(onboard.goal_ops("Like when I last did a varroa treatment on each hive.", "x")[0]["node"][
            "name"], "When I last did a varroa treatment on each hive")
        self.assertEqual(onboard.goal_ops("Um, so, track the hives.", "x")[0]["node"]["name"], "Track the hives")
        self.assertEqual(onboard.goal_ops("Like-minded growers share seeds.", "x")[0]["node"]["name"],
                         "Like-minded growers share seeds")

    def test_a_name_on_another_answer_is_refused(self):
        target = os.path.join(self.place, "apiary2")
        data = {"answers": [{"q": "q.frame.you", "text": "I keep bees.", "name": "Bees"}]}
        code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target,
                                    "--answers", json.dumps(data))
        self.assertEqual(code, 2, (obj, err))
        self.assertIn("name is only for q.frame.goal", obj["message"])


class SetupSkippedGoalTest(SetupCase):
    def test_a_skipped_goal_is_logged_and_not_asked_at_once(self):
        target = os.path.join(self.place, "pond")
        data = {"setup": {"title": "Pond life", "summary": "The frogs and newts in our garden pond.",
                          "skipped": [2, 4, 6]}}
        code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target,
                                    "--answers", json.dumps(data))
        self.assertEqual(code, 0, (obj, err))
        log, _bad = store.read_jsonl(os.path.join(target, "interview", "log.jsonl"))
        self.assertIn(("q.frame.goal", "skipped"), [(r["q"], r["status"]) for r in log])
        clear()
        onto = graph.Ontology.load(store.Repo.open(target))
        self.assertNotIn("q.frame.goal", [q["id"] for q in interview.next_questions(onto, 10)])
        answers = [s for s in obj["steps"] if s["id"] == "answers"][0]
        self.assertIn("(1 answer, 1 summary, 4 decisions)", answers["detail"])
        self.assertTrue(answers["detail"].startswith("6 recorded"), answers["detail"])


class SetupValueCheckTest(SetupCase):
    def refused(self, *args):
        target = os.path.join(self.place, "topic-a")
        code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target, *args)
        self.assertEqual(code, 2, (obj, err))
        self.assertFalse(os.path.exists(target), "nothing is written before the values are checked")
        return obj["message"]

    def test_a_nul_anywhere_is_found_before_anything_is_written(self):
        message = self.refused("--answers", json.dumps({"answers": [{"q": "q.frame.you",
                                                                     "text": "I keep bees\u0000 at home."}]}))
        self.assertIn("answers[0].text holds the control character U+0000", message)
        message = self.refused("--answers", json.dumps({"decisions": [{"question": "Which hive first?",
                                                                       "chosen": "North\u0000 hive"}]}))
        self.assertIn("decisions[0].chosen holds the control character U+0000", message)

    def test_one_bad_text_is_one_problem(self):
        message = self.refused("--answers", json.dumps({"answers": [{"q": "q.frame.goal",
                                                                     "text": "Decide\u0000 things."}]}))
        self.assertIn("--answers: 1 value setup cannot record", message)
        self.assertEqual(message.count("U+0000"), 1, message)
        message = self.refused("--summary", "two\x1b[2Jlines")
        self.assertIn("--summary: 1 value setup cannot record", message)
        self.assertEqual(message.count("U+001B"), 1, message)

    def test_a_summary_flag_is_named_as_the_flag(self):
        token = _support.fake_secret("github")
        message = self.refused("--summary", "token %s here" % token)
        self.assertIn("--summary holds credential-like text (github)", message)
        self.assertIn("Fix the --summary text and run onto setup again", message)
        self.assertNotIn("answers file", message)
        self.assertNotIn(token, message)


class TopicOriginTest(SetupCase):
    TEAM_URL = "https://git.example.invalid/team/shared-topic.git"

    def shared_topic(self):
        first = os.path.join(self.place, "u1")
        code, obj, err = self.setup("--yes", "--plugin", "skip", "--launch", "none", "--new", first,
                                    "--answers", "{}")
        self.assertEqual(code, 0, (obj, err))
        team = os.path.join(self.place, "team.git")
        _support.git(self.place, "clone", "-q", "--bare", first, team)
        _support.git(first, "push", "-q", team, "kit/general-ontology:refs/heads/general-ontology")
        return team

    def remotes(self, root):
        return {name: _support.git(root, "remote", "get-url", name) for name in _support.git(root, "remote").split()}

    def test_new_on_a_clone_of_a_shared_topic_keeps_its_origin(self):
        team = self.shared_topic()
        clone = os.path.join(self.place, "teamclone")
        _support.git(self.place, "clone", "-q", team, clone)
        _support.git(clone, "remote", "set-url", "origin", self.TEAM_URL)
        code, obj, err = self.setup("--yes", "--plugin", "skip", "--launch", "none", "--new", clone)
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(self.remotes(clone), {"kit": KIT_URL, "origin": self.TEAM_URL})
        self.assertEqual(_support.git(clone, "config", "branch.main.remote"), "origin")
        self.assertNotIn("renamed", json.dumps(obj))

    def test_setup_in_a_clone_of_a_shared_topic_keeps_its_origin(self):
        team = self.shared_topic()
        clone = os.path.join(self.place, "teamclone2")
        _support.git(self.place, "clone", "-q", team, clone)
        code, obj, err = self.setup("--plugin", "skip", "--launch", "none", cwd=clone)
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(self.remotes(clone), {"origin": team})
        self.assertEqual(_support.git(clone, "config", "branch.main.remote"), "origin")
        branch = [s for s in obj["steps"] if s["id"] == "branch"][0]
        self.assertIn("no kit remote (pass --kit-url URL to add one)", branch["detail"])


class HereUnrelatedMainTest(SetupCase):
    def test_here_refuses_a_main_made_from_another_project(self):
        clone = self.clone_template("mainclone")
        _support.git(clone, "checkout", "-q", "--orphan", "main")
        _support.git(clone, "rm", "-rq", "--cached", ".")
        other = os.path.join(clone, "other.txt")
        with open(other, "w", encoding="utf-8") as fh:
            fh.write("another project\n")
        _support.git(clone, "add", "other.txt")
        _support.git(clone, "commit", "-qm", "Another project")
        os.remove(other)
        _support.git(clone, "checkout", "-q", "-f", "general-ontology")
        code, obj, err = self.setup("--here", "--yes", "--title", "Here main", "--plugin", "skip", "--launch",
                                    "none", cwd=clone)
        self.assertEqual(code, 1, (obj, err))
        branch = [s for s in obj["steps"] if s["id"] == "branch"][0]
        self.assertEqual(branch["status"], "failed")
        self.assertIn("shares no history with general-ontology", branch["detail"])
        self.assertEqual(_support.git(clone, "branch", "--show-current"), "general-ontology")
        self.assertEqual(_support.git(clone, "remote").split(), ["origin"])
        self.assertFalse(os.path.exists(os.path.join(clone, "ontology.json")))
        self.assertTrue(os.path.isfile(os.path.join(clone, "new-topic")))


class BranchInWorktreeTest(SetupCase):
    def test_an_older_branch_in_another_worktree_gets_a_fix_that_works(self):
        source = make_template(os.path.join(self.tmp, "old"))
        init = os.path.join(source, "plugins", "general-ontology", "ontokit", "__init__.py")
        with open(init, encoding="utf-8") as fh:
            text = fh.read()
        _support.git(source, "checkout", "-q", "-b", "dev")
        _support.git(source, "checkout", "-q", "general-ontology")
        with open(init, "w", encoding="utf-8") as fh:
            fh.write(doctor.VERSION_RE.sub('__version__ = "0.0.1"', text))
        _support.commit_all(source, "an older kit")
        _support.git(source, "checkout", "-q", "dev")
        _support.git(source, "reset", "-q", "--hard", "general-ontology")
        with open(init, "w", encoding="utf-8") as fh:
            fh.write(text)
        _support.commit_all(source, "the current kit")
        tree = os.path.join(self.tmp, "wt go")
        _support.git(source, "worktree", "add", "-q", tree, "general-ontology")
        code, obj, _err = self.setup("--yes", "--plugin", "skip", "--launch", "none", "--new",
                                     os.path.join(self.place, "garden"), cwd=source)
        self.assertEqual(code, 2, obj)
        message = obj["message"]
        self.assertIn("merge --ff-only", message)
        self.assertNotIn("branch -f general-ontology", message)
        # the fix it names works
        fix = message.split("update the branch (", 1)[1].split(" (general-ontology is checked out")[0]
        argv = shlex.split(fix)
        self.assertEqual(argv[:2], ["git", "-C"])
        self.assertEqual(os.path.realpath(argv[2]), os.path.realpath(tree))
        _support.git(argv[2], *argv[3:])
        code, obj, err = self.setup("--yes", "--plugin", "skip", "--launch", "none", "--new",
                                    os.path.join(self.place, "garden"), cwd=source)
        self.assertEqual(code, 0, (obj, err))


class ConditionalIdentityTest(SetupCase):
    def test_an_identity_set_for_the_topic_folders_is_found(self):
        from tests.test_setup import NO_IDENTITY, config_env

        place = os.path.realpath(self.place)
        extra = os.path.join(self.home, ".gitconfig-onto")
        with open(extra, "w", encoding="utf-8") as fh:
            fh.write("[user]\n\tname = Onto Me\n\temail = me@example.org\n")
        main = os.path.join(self.home, ".gitconfig")
        with open(main, "w", encoding="utf-8") as fh:
            fh.write('[includeIf "gitdir:%s/"]\n\tpath = %s\n' % (place, extra))
        env = config_env(self.home, NO_IDENTITY)
        env["GIT_CONFIG_GLOBAL"] = main
        target = os.path.join(place, "nested", "garden")
        with mock.patch.dict(os.environ, env):
            self.assertIsNone(onboard.identity_at(target))
            self.assertFalse(os.path.exists(os.path.join(place, "nested")), "the probe leaves nothing behind")
            self.assertIsNotNone(onboard.identity_at(os.path.join(self.tmp, "elsewhere")))
            code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target)
            self.assertEqual(code, 0, (obj, err))
            self.assertEqual(_support.git(target, "log", "-1", "--format=%an <%ae>"), "Onto Me <me@example.org>")


class KitUrlFormTest(SetupCase):
    def new(self, name, url, *extra):
        target = os.path.join(self.place, name)
        code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target, "--kit-url",
                                    url, *extra)
        return target, code, obj, err

    def test_a_relative_folder_becomes_a_full_path_that_git_can_fetch(self):
        caller = os.path.join(self.place, "caller")
        os.makedirs(caller)
        _support.git(caller, "clone", "-q", "--bare", self.template_root, os.path.join(caller, "kit"))
        with mock.patch.dict(os.environ, {onboard.CALLER_CWD: caller}):
            target, code, obj, err = self.new("relkit", "./kit")
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(_support.git(target, "remote", "get-url", "kit"), os.path.join(caller, "kit"))
        _support.git(target, "fetch", "-q", "kit")
        with mock.patch.dict(os.environ, {onboard.CALLER_CWD: caller}):
            target, code, obj, _err = self.new("relkit2", "./nope")
        self.assertEqual(code, 2, obj)
        self.assertIn("no folder", obj["message"])
        self.assertFalse(os.path.exists(target))

    def test_a_host_and_path_without_a_scheme_is_https(self):
        target, code, obj, err = self.new("u11", "github.com/example-owner/garden-kit")
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(_support.git(target, "remote", "get-url", "kit"),
                         "https://github.com/example-owner/garden-kit")

    def test_the_marketplace_repo_never_ends_in_git(self):
        self.assertEqual(onboard.marketplace_source("example-owner/garden-kit.git")["repo"],
                         "example-owner/garden-kit")
        self.assertEqual(onboard.marketplace_source("https://github.com/example-owner/garden-kit.git")["repo"],
                         "example-owner/garden-kit")


class CommitFailureTest(SetupCase):
    def test_a_signing_failure_is_named(self):
        from tests.test_setup import GIT_CONFIG, config_env

        target = os.path.join(self.place, "gpg1")
        failing = GIT_CONFIG + (("commit.gpgsign", "true"), ("gpg.program", "false"))
        with mock.patch.dict(os.environ, config_env(self.home, failing)):
            code, obj, _err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target)
        self.assertEqual(code, 1, obj)
        commit = [s for s in obj["steps"] if s["id"] == "commit"][0]
        self.assertEqual(commit["status"], "failed")
        self.assertIn("error: gpg failed to sign the data", commit["detail"])
        self.assertIn("config commit.gpgsign false", commit["detail"])

    def test_git_failure_keeps_the_cause(self):
        self.assertEqual(onboard.git_failure("error: gpg failed to sign the data\nfatal: failed to write commit "
                                             "object\n"),
                         "error: gpg failed to sign the data; fatal: failed to write commit object")
        self.assertEqual(onboard.git_failure("fatal: only this"), "fatal: only this")
        self.assertEqual(onboard.git_failure(""), "no output")
        self.assertIn("hook", onboard.commit_fix("pre-commit hook exited with 1", "/x"))
        self.assertIsNone(onboard.commit_fix("fatal: disk full", "/x"))


class FolderPromptTest(SetupCase):
    def run_prompts(self, replies):
        onboard._interactive = lambda: True
        answers = iter(replies)
        asked = []
        with mock.patch.object(onboard, "_input", side_effect=lambda q: (asked.append(q), next(answers))[1]):
            code, obj, err = self.setup("--launch", "none", "--plugin", "skip")
        return code, obj, err, asked

    def test_a_folder_that_holds_files_is_asked_again(self):
        full = os.path.join(self.place, "full")
        os.makedirs(full)
        with open(os.path.join(full, "notes.txt"), "w", encoding="utf-8") as fh:
            fh.write("hi\n")
        empty = os.path.join(self.place, "fresh")
        code, obj, err, asked = self.run_prompts(["new", "Full one", "", "", full, empty])
        self.assertEqual(code, 0, (obj, err))
        self.assertIn("is not empty (it holds notes.txt). Pick another folder", asked[-1])
        self.assertEqual(obj["topic"]["path"], empty)

    def test_a_declined_cloud_folder_is_asked_again_and_a_yes_is_not_asked_twice(self):
        os.makedirs(os.path.join(self.home, "Library", "Mobile Documents", "com~apple~CloudDocs", "Desktop"))
        cloud = os.path.join(self.home, "Desktop", "dt2")
        other = os.path.join(self.place, "dt3")
        code, obj, err, asked = self.run_prompts(["new", "Cloud one", "", "", cloud, "n", other])
        self.assertEqual(code, 0, (obj, err))
        self.assertIn("Pick a folder outside the cloud-synced one", asked[-1])
        self.assertEqual(obj["topic"]["path"], other)
        code, obj, err, asked = self.run_prompts(["new", "Cloud two", "", "", os.path.join(self.home, "Desktop",
                                                                                          "dt4"), "y"])
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(sum("Use it anyway" in q for q in asked), 1, asked)
        self.assertIn("cloud-synced folder accepted", obj["steps"][0]["detail"])

    def test_a_dragged_folder_is_read_as_the_shell_reads_it(self):
        drag = os.path.join(self.place, "My Drag")
        os.makedirs(drag)
        dragged = os.path.join(drag, "garden").replace(" ", "\\ ") + " "
        code, obj, err, _asked = self.run_prompts(["new", "Dragged", "", "", dragged])
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(obj["topic"]["path"], os.path.join(drag, "garden"))
        self.assertEqual(sorted(os.listdir(self.place)), ["My Drag"])
        self.assertEqual(onboard._unquote_path("'/a b/c' "), "/a b/c")
        self.assertEqual(onboard._unquote_path("/a’s folder/c"), "/a’s folder/c")


@unittest.skipIf(os.name == "nt", "a POSIX launcher")
class LauncherTest(SetupCase):
    def test_the_command_file_runs_through_a_symlink(self):
        desktop = os.path.join(self.tmp, "Desk top")
        os.makedirs(desktop)
        link = os.path.join(desktop, "New topic.command")
        os.symlink(os.path.join(self.template_root, "New topic.command"), link)
        target = os.path.join(self.place, "linked")
        for shell in ("sh", "bash", "zsh"):
            exe = shutil.which(shell)
            if not exe:
                continue
            proc = subprocess.run([exe, link, "--help"], cwd=self.tmp, env=self.env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
            self.assertEqual(proc.returncode, 0, (shell, proc.stderr))
            self.assertIn(b"usage: onto setup", proc.stdout)
        proc = subprocess.run([shutil.which("sh"), link, "--yes", "--launch", "none", "--plugin", "skip", "--new",
                               target], cwd=self.tmp, env=self.env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.isfile(os.path.join(target, "ontology.json")))

    def test_a_relative_answers_file_is_read_from_where_new_topic_ran(self):
        caller = os.path.join(self.place, "urls")
        os.makedirs(caller)
        with open(os.path.join(caller, "ans.json"), "w", encoding="utf-8") as fh:
            json.dump({"setup": {"title": "Relative answers"}, "answers": [], "decisions": []}, fh)
        proc = subprocess.run([shutil.which("sh"), os.path.join(self.template_root, "new-topic"), "--yes",
                               "--launch", "none", "--plugin", "skip", "--new", "rel-ans", "--answers",
                               "@ans.json"], cwd=caller, env=self.env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        self.assertEqual(proc.returncode, 0, (proc.stdout, proc.stderr))
        target = os.path.join(caller, "rel-ans")
        with open(os.path.join(target, "ontology.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["title"], "Relative answers")


class ProgressTest(SetupCase):
    def test_slow_steps_say_so_before_they_start(self):
        import io

        from tests.test_setup import SYSTEM_PATH, write_stub

        bin_dir = os.path.join(self.tmp, "stub-bin")
        write_stub(bin_dir)
        shown = io.StringIO()
        target = os.path.join(self.place, "slow1")
        with mock.patch.object(onboard, "_progress_stream", lambda: shown), \
                mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + SYSTEM_PATH,
                                             "CLAUDE_LOG": os.path.join(self.tmp, "claude.log")}):
            code, obj, err = self.setup("--yes", "--launch", "none", "--new", target)
        self.assertEqual(code, 0, (obj, err))
        lines = shown.getvalue().splitlines()
        self.assertTrue(any(line.startswith("[onto] Copying the kit into") for line in lines), lines)
        self.assertTrue(any("Installing the plugin with claude" in line and "5 minutes" in line for line in lines),
                        lines)

    def test_no_progress_without_a_terminal(self):
        import io

        with mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertIsNone(onboard._progress_stream())

    def test_ctrl_c_is_a_failed_step_not_a_traceback(self):
        target = os.path.join(self.place, "stopped")

        def stop(_self):
            raise KeyboardInterrupt

        with mock.patch.object(onboard.Setup, "do_init", stop):
            code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target)
        self.assertEqual(code, 1, (obj, err))
        init = [s for s in obj["steps"] if s["id"] == "init"][0]
        self.assertEqual(init["status"], "failed")
        self.assertIn("stopped (Ctrl-C)", init["detail"])


class WindowsCommandTest(_support.TempCase):
    def test_printed_commands_use_double_quotes_for_cmd(self):
        folder = "C:\\Users\\Ann Smith\\Ontologies\\bees"
        setup = onboard.Setup.__new__(onboard.Setup)
        setup.target, setup.steps, setup.effective_plugin = folder, [], "plugin-dir"
        with mock.patch.object(doctor, "_cmd_shell", lambda: True):
            line = [x for x in setup._next() if x.startswith("Open it")][0]
            self.assertIn('cd /d "C:\\Users\\Ann Smith\\Ontologies\\bees" && claude --plugin-dir '
                          './plugins/general-ontology "Start the ontology"', line)
            self.assertNotIn("'", line)
            self.assertEqual(doctor.shell_quote("C:\\Users\\ana\\garden"), "C:\\Users\\ana\\garden")
            spaced = os.path.join(self.tmp, "kit checkout")
            os.makedirs(spaced)
            fix = doctor.missing_branch_fix(spaced)
            self.assertIn('git -C "%s" branch general-ontology HEAD' % spaced, fix)
        with mock.patch.object(doctor, "_cmd_shell", lambda: False):
            line = [x for x in setup._next() if x.startswith("Open it")][0]
            self.assertIn("cd 'C:\\Users\\Ann Smith\\Ontologies\\bees' && claude --plugin-dir "
                          "./plugins/general-ontology 'Start the ontology'", line)


if __name__ == "__main__":
    unittest.main()
