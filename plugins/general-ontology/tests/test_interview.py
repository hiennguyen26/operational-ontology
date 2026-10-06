"""interview (WP4): the question banks (exactly 5 quick questions, stage 8, gap templates), the golden next order on
the mini fixture, skip fatigue and the 2-day recency, a skipped or put-off question held back from the next turn,
n/a closing a dimension, imported nodes satisfying count predicates, answers with stated ops (a quote found gives
confirmed/user; not found gives a draft), destructive ops waiting for confirm, and progress resumed from the log."""

from __future__ import annotations

import contextlib
import json
import os
import re
import unittest

from tests import _support
from tests.test_graph import COMMIT, add_import, make_export, mk_edge, mk_node
from ontokit import commands, graph, interview, ledger, needs, packs, pipeline, records, sources, store, util, validate
from ontokit.errors import NotFound, Refused, UsageError

NOTE = "src-000a61a61d03"  # the handbook excerpt in the mini fixture (untrusted)
DECISION = "dec-20260928-keep-the-monthly-steward-rota-or-water-e-916a"
QUICK = ["q.frame.you", "q.frame.goal", "q.frame.deliverable", "q.people.key", "q.data.where"]
MINI_GOLDEN = [
    "q.frame.deliverable",  # the last open quick-start question comes first
    "q.vocab.kinds",  # the main kinds come right after the quick start, before every gap question
    "q.gap.missing_relation.measures.in@goal:shared-harvest",
    "q.gap.missing_field.audience@deliverable:harvest-report",
    # review round 4: a drafted link is asked about, never left unseen; round 5: once per set of drafted links
    "q.gap.draft_link.43ab51f9@role:plot-coordinator",
    "q.gap.open_question.cadence@dataset:harvest-log",
    "q.gap.draft_link.c4ab70e2@deliverable:harvest-report",
    "q.frame.scope",
    "q.questions.open",
    "q.questions.assumptions",
    "q.vocab.terms",
]
# the C.10 example line, its ask reworded so that a one-line title reads well in it
C10_EXAMPLE = {"id": "q.data.where", "stage": 2, "dimension": "data", "priority": 50, "quick": True,
               "ask": "Where does the data you rely on for {topic} live today?",
               "why": "Sources lead to evidence fastest.",
               "fills": {"kinds": ["dataset", "tool"], "fields": ["location"]},
               "when": [{"count": {"kind": "dataset", "lt": 3}}],
               "until": [{"count": {"kind": "dataset", "gte": 3}}],
               "follow_ups": ["q.data.owner"], "options": None, "repeatable": False}


def clear():
    graph.clear_cache()
    store.clear_cache()


@contextlib.contextmanager
def clock(ts):
    """Run with ``ONTO_FIXED_NOW`` set to ``ts`` (restored afterwards)."""
    saved = os.environ.get("ONTO_FIXED_NOW")
    os.environ["ONTO_FIXED_NOW"] = ts
    try:
        yield
    finally:
        os.environ["ONTO_FIXED_NOW"] = saved or _support.FIXED_NOW


def bank(name):
    path = os.path.join(packs.BUILTIN_DIR, "%s.questions.jsonl" % name)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    return text, [json.loads(line) for line in text.splitlines() if line.strip()]


class Base(_support.TempCase):
    def setUp(self):
        super().setUp()
        clear()
        self.root = _support.make_topic(self.tmp)
        self.repo = store.Repo.open(self.root)

    def onto(self):
        return graph.Ontology.load(self.repo)

    def next_ids(self, n=30, stage=None):
        return [q["id"] for q in interview.next_questions(self.onto(), n, stage)]

    def scores(self, n=40):
        """Every question's score, the ones put off in the last 2 days included (``next`` holds those back)."""
        return {q["id"]: q["score"] for q in interview.next_questions(self.onto(), n, include_put_off=True)}

    def log(self):
        rows, problems = store.read_jsonl(self.repo.path(interview.LOG))
        self.assertEqual(problems, [])
        return rows

    def assert_valid(self):
        clear()
        report = validate.validate(self.repo)
        self.assertEqual([p.text() for p in report.problems], [])


# the banks -----------------------------------------------------------------------------------------------------
class BankTest(unittest.TestCase):
    def test_quick_start_is_exactly_five(self):
        _core_text, core = bank("core")
        _disc_text, disc = bank("discovery")
        self.assertEqual(sorted(q["id"] for q in core + disc if q.get("quick")), sorted(QUICK))
        self.assertEqual(sorted(q["id"] for q in core if q.get("quick")),
                         ["q.frame.deliverable", "q.frame.goal", "q.frame.you"])
        self.assertEqual(sorted(q["id"] for q in disc if q.get("quick")), ["q.data.where", "q.people.key"])
        self.assertEqual(interview.QUICK, tuple(QUICK))
        reg = packs.load(None, {"packs": ["core", "discovery"]})
        self.assertEqual(sorted(q["id"] for q in reg.questions() if q.get("quick")), sorted(QUICK))

    def test_banks_are_canonical_valid_and_consistent(self):
        all_ids = set()
        for name in ("core", "discovery", "assessment"):
            text, rows = bank(name)
            self.assertEqual(text, "".join(util.canonical_line(r) + "\n" for r in rows), name)
            ids = [r["id"] for r in rows]
            self.assertEqual(ids, sorted(ids), name)
            for row in rows:
                self.assertEqual(packs.check_question(row), [], row["id"])
                self.assertNotIn("\u2014", row["ask"] + row.get("why", ""))  # no em dash
            all_ids |= set(ids)
        reg = packs.load(None, {"packs": ["core", "discovery", "assessment"]})
        self.assertEqual([p for p in reg.problems() if "questions" in p.file], [])
        for q in reg.questions():
            for ref in q.get("follow_ups") or []:
                self.assertIn(ref, all_ids, q["id"])
            for pred in (q.get("when") or []) + (q.get("until") or []):
                for key in ("answered", "not_answered"):
                    if key in pred:
                        self.assertIn(pred[key], all_ids, q["id"])
            if not q.get("for_gap"):
                self.assertNotIn("{name}", q["ask"], q["id"])  # only gap templates name a node

    def test_the_title_follows_a_preposition(self):
        """{topic} is the one-line title, often a phrase ("Launching our indie board game"): it follows a
        preposition and never stands as the subject or as a modifier ("the {topic} data")."""
        for name in ("core", "discovery", "assessment"):
            for q in bank(name)[1]:
                for field in ("ask", "why"):
                    text = q.get(field) or ""
                    for found in re.finditer(r"\{topic\}", text):
                        before = text[:found.start()].split()
                        self.assertIn(before[-1] if before else "", ("in", "for", "about", "to"), (q["id"], text))

    def test_asks_are_self_contained(self):
        """A bank question can come back sessions later, so its ask never points at an earlier question."""
        back = re.compile(r"\b(those|these|that data|the first deliverable)\b", re.I)
        for name in ("core", "discovery", "assessment"):
            for q in bank(name)[1]:
                if not q.get("for_gap"):
                    self.assertIsNone(back.search(q["ask"]), (q["id"], q["ask"]))

    def test_the_c10_example_line_ships(self):
        _text, disc = bank("discovery")
        self.assertEqual([q for q in disc if q["id"] == "q.data.where"], [C10_EXAMPLE])

    def test_stage_eight_and_the_catch_all(self):
        reg = packs.load(None, {"packs": ["core", "discovery"]})
        by_id = {q["id"]: q for q in reg.questions()}
        meet = by_id[interview.MEET]
        self.assertEqual(meet["stage"], 8)
        self.assertIn({"has_imports": True}, meet["when"])
        compose = [q for q in reg.questions() if q.get("stage") == 8]
        self.assertTrue(all({"has_imports": True} in q["when"] for q in compose))
        self.assertTrue(by_id[interview.CATCH_ALL]["repeatable"])
        for stage in range(0, 10):
            if stage == 0:
                continue
            self.assertTrue([q for q in reg.questions() if q.get("stage") == stage], stage)

    def test_gap_templates(self):
        reg = packs.load(None, {"packs": ["core", "discovery"]})
        templates = {q["for_gap"]: q for q in reg.questions() if q.get("for_gap")}
        self.assertEqual(set(templates) - set(interview.ASKED_GAPS), set())
        self.assertNotIn("missing_relation", templates)  # expects[].ask names the relation itself
        self.assertEqual(templates["orphan"]["ask"], "How does {name} connect to other things in {topic}?")
        self.assertEqual(templates["orphan"]["id"], "q.gap.orphan")
        self.assertEqual(templates["missing_field"]["ask"], "What is the {field} of {name}?")
        for gap_type, q in templates.items():
            self.assertEqual(q["id"], "q.gap." + gap_type)


# next -----------------------------------------------------------------------------------------------------------
class NextTest(Base):
    def test_golden_next_order_on_mini(self):
        self.assertEqual(self.next_ids(len(MINI_GOLDEN)), MINI_GOLDEN)
        first = interview.next_questions(self.onto(), 1)[0]
        self.assertEqual(first["ask"], "What is the first thing you want to produce for Mini garden, and who will read "
                                       "or use it?")
        self.assertTrue(first["pinned"])
        self.assertEqual(first["score"], 270.0)  # 70 + 200 (stage 0 is open) + 100 x (1 - deliverables 1.0)
        kinds = interview.next_questions(self.onto(), 2)[1]
        self.assertEqual((kinds["id"], kinds.get("after_quick"), kinds.get("pinned")), (interview.KINDS, True, None))
        gap = interview.next_questions(self.onto(), 3)[2]
        self.assertEqual(gap["node"], "goal:shared-harvest")
        self.assertEqual(gap["ask"], "How will you know Shared harvest is met?")
        self.assertEqual(gap["gap"]["type"], "missing_relation")
        # 30 + 200 + 0 + 20 x 6 x log2(2 + 4) x 2 (a goal is 0 hops from a goal)
        self.assertEqual(gap["score"], 850.39)
        self.assertEqual(self.next_ids(len(MINI_GOLDEN)), MINI_GOLDEN)  # same input, same order

    def test_quick_questions_come_first_on_a_fresh_topic(self):
        root = _support.init_topic(self.tmp, "fresh", "Fresh topic")
        onto = graph.Ontology.load(root)
        found = interview.next_questions(onto, 7)
        self.assertEqual([q["id"] for q in found[:5]], QUICK)
        self.assertTrue(all(q.get("pinned") for q in found[:5]))
        self.assertEqual(found[5]["id"], interview.KINDS)  # right after the quick start, and not one of it
        self.assertEqual(found[6]["id"], "q.gap.thin@topic:fresh")
        self.assertGreater(found[6]["score"], found[5]["score"])  # placed after the quick start, not outscored
        self.assertGreater(found[6]["score"], found[0]["score"])  # pinned, not outscored
        self.assertEqual(interview.progress(onto)["stage"], 0)

    def test_skip_fatigue_and_two_day_recency(self):
        base = self.scores()["q.vocab.terms"]
        with clock("2026-09-28T12:00:00Z"):
            interview.answer(self.repo, "q.vocab.terms", None, status="skipped")
        clear()
        self.assertEqual(self.scores()["q.vocab.terms"], base - 30 - 300)
        self.assertNotIn("q.vocab.terms", self.next_ids(40))  # put off: held back while others are open
        with clock("2026-09-29T11:00:00Z"):
            clear()
            self.assertEqual(self.scores()["q.vocab.terms"], base - 330)  # still within 2 days
            self.assertNotIn("q.vocab.terms", self.next_ids(40))
        with clock("2026-09-30T12:00:01Z"):
            clear()
            self.assertEqual(self.scores()["q.vocab.terms"], base - 30)  # the recency is over; the fatigue stays
            self.assertIn("q.vocab.terms", self.next_ids(40))
            interview.answer(self.repo, "q.vocab.terms", None, status="skipped")
        with clock("2026-10-03T12:00:00Z"):
            clear()
            self.assertEqual(self.scores()["q.vocab.terms"], base - 60)
        self.assertEqual([r["status"] for r in self.log()[-2:]], ["skipped", "skipped"])

    def test_later_waits_two_days_without_fatigue(self):
        base = self.scores()["q.process.main"]
        interview.answer(self.repo, "q.process.main", None, status="later")
        clear()
        self.assertEqual(self.scores()["q.process.main"], base - 300)
        self.assertNotIn("q.process.main", self.next_ids(40))
        with clock("2026-10-01T12:00:00Z"):
            clear()
            self.assertEqual(self.scores()["q.process.main"], base)
            self.assertIn("q.process.main", self.next_ids(40))

    def test_skipped_quick_question_is_handled_and_no_longer_pinned(self):
        res = interview.answer(self.repo, "q.frame.deliverable", "", status="skipped")
        self.assertEqual(res["answer"]["src"], None)
        clear()
        prog = interview.progress(self.onto())
        self.assertEqual(prog["quick"], {"total": 5, "handled": 5, "open": []})
        self.assertTrue(prog["stages"][0]["done"])
        self.assertEqual(prog["stage"], 3)  # vocabulary is the earliest open stage of the mini fixture
        found = interview.next_questions(self.onto(), 40)
        self.assertFalse(any(q.get("pinned") for q in found))
        self.assertNotIn("q.frame.deliverable", [q["id"] for q in found])  # skipped just now: held back
        found = interview.next_questions(self.onto(), 40, include_put_off=True)
        self.assertEqual(found[-1]["id"], "q.frame.deliverable")  # and last when every question is listed
        self.assertEqual(found[-1]["put_off"], {"status": "skipped", "until": "2026-09-30T12:00:00Z"})

    def test_na_closes_a_dimension(self):
        before = self.next_ids()
        self.assertIn("q.vocab.terms", before)
        self.assertIn("q.vocab.synonyms", before)
        interview.answer(self.repo, "q.vocab.terms", None, status="na")
        clear()
        prog = interview.progress(self.onto())
        self.assertEqual(prog["closed_dimensions"], ["vocabulary"])
        self.assertNotIn("vocabulary", prog["coverage"])
        self.assertTrue([s for s in prog["stages"] if s["n"] == 3][0]["done"])
        after = interview.next_questions(self.onto(), 40)
        self.assertFalse([q for q in after if q.get("dimension") == "vocabulary"])
        # answering the question again reopens the dimension
        interview.answer(self.repo, "q.vocab.terms", "Mulch is a layer of straw over the soil.")
        clear()
        self.assertEqual(interview.progress(self.onto())["closed_dimensions"], [])

    def test_na_on_a_gap_question_closes_only_that_node(self):
        gap_id = "q.gap.missing_field.audience@deliverable:harvest-report"
        self.assertIn(gap_id, self.next_ids())
        res = interview.answer(self.repo, gap_id, None, status="na")
        self.assertEqual(res["question"]["node"], "deliverable:harvest-report")
        row = self.log()[-1]
        self.assertEqual((row["q"], row["node"], row["status"]), ("q.gap.missing_field.audience",
                                                                 "deliverable:harvest-report", "na"))
        clear()
        self.assertNotIn(gap_id, self.next_ids())
        self.assertEqual(interview.progress(self.onto())["closed_dimensions"], [])

    def test_skip_stage_closes_the_stage(self):
        interview.answer(self.repo, "q.questions.open", None, status="skip_stage")
        clear()
        prog = interview.progress(self.onto())
        self.assertEqual(prog["skipped_stages"], [7])
        self.assertTrue([s for s in prog["stages"] if s["n"] == 7][0]["done"])
        self.assertFalse([q for q in interview.next_questions(self.onto(), 40) if q["stage"] == 7])

    def test_skip_stage_on_a_gap_question_is_refused(self):
        before = _support.snapshot(self.root, skip=[".onto"])
        with self.assertRaises(UsageError) as caught:
            interview.answer(self.repo, "q.gap.missing_field.audience@deliverable:harvest-report", None,
                             status="skip_stage")
        self.assertIn("sits in stage 9 (deepen), which is never skipped; use status na", caught.exception.message)
        gap_id = "q.gap.missing_relation.measures.in@goal:shared-harvest"  # stage 0 while the frame is open
        with self.assertRaises(UsageError) as caught:
            interview.answer(self.repo, gap_id, None, status="skip_stage")
        self.assertIn("answer q.frame.you with status skip_stage to skip stage 0 (frame)", caught.exception.message)
        self.assertEqual(before, _support.snapshot(self.root, skip=[".onto"]))
        # a gap line written anyway (a merged log, say) closes that question on that node and no stage
        store.append_jsonl(self.repo.path(interview.LOG), {
            "id": "ans-20260928-0a0a0a", "q": "q.gap.missing_relation.measures.in", "at": _support.FIXED_NOW,
            "status": "skip_stage", "src": None, "proposal": None, "node": "goal:shared-harvest"})
        clear()
        prog = interview.progress(self.onto())
        self.assertEqual(prog["skipped_stages"], [])
        self.assertFalse(prog["stages"][0]["done"])
        self.assertNotIn(gap_id, self.next_ids(40))

    def test_stage_filter_and_bad_stage(self):
        ids = self.next_ids(stage=4)
        self.assertEqual(ids, ["q.process.main", "q.process.steps", "q.process.inputs"])
        with self.assertRaises(UsageError):
            interview.next_questions(self.onto(), 3, stage=10)

    def test_stage_zero_gap_questions_move_to_deepen_when_their_stage_is_done(self):
        gap = [q for q in interview.next_questions(self.onto(), 40)
               if q["id"] == "q.gap.missing_field.audience@deliverable:harvest-report"][0]
        self.assertEqual(gap["stage"], 9)  # deliverables (stage 6) is done on the mini fixture
        goal = [q for q in interview.next_questions(self.onto(), 40) if q.get("node") == "goal:shared-harvest"][0]
        self.assertEqual(goal["stage"], 0)  # frame is still open

    def test_untrusted_node_is_marked(self):
        gap = [q for q in interview.next_questions(self.onto(), 40)
               if q.get("node") == "deliverable:harvest-report"][0]
        # C.7: the flags sit on the item itself, as every other command spreads them
        self.assertEqual((gap.get("untrusted"), gap.get("draft"), gap.get("archived")), (True, True, None))
        self.assertNotIn("flags", gap)
        code, out, err = _support.run_cli(["next", "--n", "4"], self.root)  # q.vocab.kinds takes the second place
        self.assertEqual(code, 0, err)
        line = [ln for ln in out.splitlines() if "deliverable:harvest-report" in ln][0]
        self.assertIn('[untrusted] "What is the audience of Harvest report?"', line)
        self.assertIn("[untrusted] deliverable:harvest-report (draft)", line)
        code, out, err = _support.run_cli(["next", "--json", "--n", "4"], self.root)
        item = [q for q in json.loads(out)["questions"] if q.get("node") == "deliverable:harvest-report"][0]
        self.assertTrue(item["untrusted"])
        self.assertTrue(item["draft"])
        bank_item = [q for q in json.loads(out)["questions"] if not q.get("node")][0]
        self.assertNotIn("untrusted", bank_item)

    def test_more_line_names_calls_that_reach_every_question(self):
        ctx = commands.Context(repo=self.repo, mcp=True, profile="full")
        text, is_error, obj = commands.dispatch(commands.get("onto_next"), {"n": 2}, ctx, "json")
        self.assertFalse(is_error, text)
        self.assertGreater(obj["available"], 20)
        self.assertEqual(sum(obj["by_stage"].values()), obj["available"])
        text, is_error, _obj = commands.dispatch(commands.get("onto_next"), {"n": 2}, ctx)
        line = [ln for ln in text.splitlines() if ln.startswith("+")][0]
        more = obj["available"] - 2
        self.assertTrue(line.startswith("+%d more: onto_next n=20 lists 18 of them; by stage "
                                        "(onto_next stage=<n> n=20): " % more), line)
        # each stage named with the questions it holds past the ones shown, and each stage call reaches them all
        shown = [q["stage"] for q in obj["questions"]]
        for stage, count in obj["by_stage"].items():
            left = count - shown.count(int(stage))
            if left:
                self.assertIn("%s (%d)" % (stage, left), line)
            self.assertLessEqual(count, 20)
            self.assertEqual(len(interview.next_questions(self.onto(), 20, int(stage))), count)
        # the CLI takes any n, so one call lists the rest
        code, out, err = _support.run_cli(["next", "--n", "2"], self.root)
        self.assertIn("+%d more (onto next --n %d)" % (more, obj["available"]), out)
        code, out, err = _support.run_cli(["next", "--n", str(obj["available"])], self.root)
        self.assertEqual(len([ln for ln in out.splitlines() if ln[:1].isdigit()]), obj["available"])
        # a stage filter is kept in the follow-up call
        text, _e, _o = commands.dispatch(commands.get("onto_next"), {"n": 1, "stage": 4}, ctx)
        self.assertIn("+2 more (onto_next n=3 stage=4)", text)


class TitleTest(_support.TempCase):
    def test_a_phrase_title_reads_well_in_the_asks(self):
        root = _support.init_topic(self.tmp, "meeple", "Launching our indie board game")
        clear()
        asks = {q["id"]: q["ask"] for q in interview.next_questions(graph.Ontology.load(root), 40)}
        title = "Launching our indie board game"
        self.assertEqual(asks["q.data.where"], "Where does the data you rely on for %s live today?" % title)
        self.assertEqual(asks["q.gap.thin@topic:meeple"],
                         "What should a newcomer know about %s? One or two sentences are enough." % title)
        self.assertEqual(asks["q.frame.goal"],
                         "What is the main goal for %s? What should be true once it is reached?" % title)
        self.assertEqual(asks["q.constraints.rules"], "Which rules, limits or requirements apply to %s?" % title)
        for qid, ask in asks.items():
            self.assertNotRegex(ask, r"(the|want|should|must) %s" % title, qid)
            self.assertFalse(ask.startswith(title), qid)


class ChangeQuestionTest(_support.TempCase):
    """q.deepen.change asks what has changed since we last spoke: never in the first session."""

    def setUp(self):
        super().setUp()
        clear()
        self.root = _support.init_topic(self.tmp, "bakery", "Opening a neighborhood bakery")
        self.repo = store.Repo.open(self.root)

    def asked(self):
        clear()
        return [q["id"] for q in interview.next_questions(graph.Ontology.load(self.repo), 60)]

    def earlier(self):
        clear()
        return interview._State(graph.Ontology.load(self.repo)).earlier_session()

    def test_asked_only_once_an_earlier_session_exists(self):
        self.assertFalse(self.earlier())  # no log at all
        for n, qid in enumerate(QUICK):
            with clock("2026-09-29T08:1%d:00Z" % n):
                interview.answer(self.repo, qid, "Answer %d about the bakery." % n)
        with clock("2026-09-29T08:15:00Z"):
            self.assertTrue(interview.progress(graph.Ontology.load(self.repo))["stages"][0]["done"])
            asked = self.asked()
            self.assertIn(interview.CATCH_ALL, asked)
            self.assertNotIn(interview.CHANGE, asked)  # stage 0 is done, but this is the first session
            self.assertNotIn(interview.CHANGE, [q["id"] for q in interview.next_questions(
                graph.Ontology.load(self.repo), 20, stage=9)])
        with clock("2026-09-29T11:14:00Z"):  # 3 hours after the last line: that session goes on
            self.assertFalse(self.earlier())
            self.assertNotIn(interview.CHANGE, self.asked())
        with clock("2026-09-29T11:14:01Z"):  # past 3 hours: a new session, and the first one is earlier
            self.assertTrue(self.earlier())
            self.assertIn(interview.CHANGE, self.asked())
            interview.answer(self.repo, interview.CHANGE, "The lease on the shop is signed.")
        with clock("2026-09-29T11:30:00Z"):  # the second session has lines now; the first is still earlier
            self.assertTrue(self.earlier())
            self.assertIn(interview.CHANGE, self.asked())  # repeatable, ranked down for 2 days

    def test_sessions_are_split_by_pauses_not_by_length(self):
        def line(at, n):
            store.append_jsonl(self.repo.path(interview.LOG), {
                "id": "ans-20260929-%06x" % n, "q": interview.CATCH_ALL, "at": at, "status": "later",
                "src": None, "proposal": None, "node": None})

        for n, at in enumerate(("2026-09-29T08:00:00Z", "2026-09-29T10:30:00Z", "2026-09-29T12:59:00Z")):
            line(at, n)
        with clock("2026-09-29T13:10:00Z"):  # five hours of lines, never 3 hours apart: one session
            self.assertFalse(self.earlier())
            clear()
            session = interview.progress(graph.Ontology.load(self.repo))["last_session"]
            self.assertEqual((session["started"], session["lines"]), ("2026-09-29T08:00:00Z", 3))
        line("2026-09-29T16:00:00Z", 3)
        with clock("2026-09-29T16:05:00Z"):  # a pause of 3 hours within the log
            self.assertTrue(self.earlier())


class QuickStartTest(_support.TempCase):
    """The quick start on a fresh topic, with the three frame questions answered."""

    def setUp(self):
        super().setUp()
        clear()
        self.root = _support.init_topic(self.tmp, "fresh", "Fresh topic")
        self.repo = store.Repo.open(self.root)
        for qid, text in (("q.frame.you", "I coordinate the plots."), ("q.frame.goal", "Share the harvest fairly."),
                          ("q.frame.deliverable", "A weekly harvest sheet for the members.")):
            interview.answer(self.repo, qid, text)

    def onto(self):
        clear()
        return graph.Ontology.load(self.repo)

    def test_na_through_a_sibling_question_ends_the_quick_start(self):
        self.assertEqual(interview.progress(self.onto())["quick"]["open"], ["q.data.where", "q.people.key"])
        interview.answer(self.repo, "q.people.orgs", None, status="na")
        interview.answer(self.repo, "q.data.documents", None, status="na")
        prog = interview.progress(self.onto())
        self.assertEqual(prog["closed_dimensions"], ["data", "people"])
        self.assertEqual(prog["quick"], {"total": 5, "handled": 5, "open": []})
        self.assertTrue(prog["stages"][0]["done"])
        self.assertEqual(prog["stage"], 3)  # people and data are n/a
        found = interview.next_questions(self.onto(), 40)
        self.assertNotIn(interview.CHANGE, [q["id"] for q in found])  # the first session is still going
        self.assertFalse(any(q.get("pinned") for q in found))
        with clock("2026-09-28T15:00:01Z"):  # the session is over: the next one asks what has changed
            self.assertIn(interview.CHANGE, [q["id"] for q in interview.next_questions(self.onto(), 40)])

    def test_skip_stage_through_a_sibling_question_ends_the_quick_start(self):
        interview.answer(self.repo, "q.people.key", "The plot coordinator and the bed stewards.")
        interview.answer(self.repo, "q.data.documents", None, status="skip_stage")
        prog = interview.progress(self.onto())
        self.assertEqual(prog["skipped_stages"], [2])
        self.assertEqual(prog["quick"]["open"], [])
        self.assertTrue(prog["stages"][0]["done"])
        self.assertEqual(prog["stage"], 1)

    def test_later_or_skipped_never_erase_an_answer(self):
        before = interview.progress(self.onto())
        snap = _support.snapshot(self.root, skip=[".onto"])
        for status in ("later", "skipped"):
            with self.assertRaises(UsageError) as caught:
                interview.answer(self.repo, "q.frame.goal", None, status=status)
            self.assertIn("q.frame.goal is already answered", caught.exception.message)
            self.assertIn("status na", caught.exception.message)
        self.assertEqual(snap, _support.snapshot(self.root, skip=[".onto"]))
        # a line written anyway (a merged log, say) does not undo the answer either
        store.append_jsonl(self.repo.path(interview.LOG), {
            "id": "ans-20260928-0b0b0b", "q": "q.frame.goal", "at": _support.FIXED_NOW, "status": "later",
            "src": None, "proposal": None, "node": None})
        state = interview._State(self.onto())
        self.assertTrue(state.log.answered("q.frame.goal"))
        prog = interview.progress(self.onto())
        self.assertEqual(prog["quick"], before["quick"])
        self.assertEqual(prog["answered"], before["answered"])
        self.assertIn("q.frame.scope", [q["id"] for q in interview.next_questions(self.onto(), 40)])
        # na stays an explicit override
        interview.answer(self.repo, "q.frame.goal", None, status="na")
        self.assertEqual(interview.progress(self.onto())["closed_dimensions"], ["frame"])
        # a repeatable question may be put off after an answer: the answer stands and it waits 2 days
        interview.answer(self.repo, "q.deepen.more", "We also swap seeds in spring.")
        base = [q["score"] for q in interview.next_questions(self.onto(), 40) if q["id"] == "q.deepen.more"][0]
        interview.answer(self.repo, "q.deepen.more", None, status="later")
        state = interview._State(self.onto())
        self.assertEqual(state.log.status("q.deepen.more"), "answered")
        self.assertNotIn("q.deepen.more", [q["id"] for q in interview.next_questions(self.onto(), 40)])
        found = [q for q in interview.next_questions(self.onto(), 40, include_put_off=True)
                 if q["id"] == "q.deepen.more"]
        self.assertEqual([q["score"] for q in found], [base])  # already within the 2 days of its answer
        self.assertEqual(found[0]["put_off"]["status"], "later")  # the later line, the answer's timestamp


def designer_ops(ns):
    """The answer to q.frame.you on a fresh topic: a role in the topic and a person in the role."""
    maya = [{"quote": "I am Maya, the designer.", "by": "user"}]
    priya = [{"quote": "Priya paints the cards.", "by": "user"}]
    return [
        {"op": "add_node", "ref": "$designer", "basis": "stated", "prov": maya,
         "node": {"kind": "role", "name": "Designer", "summary": "Designs the game."}},
        {"op": "add_edge", "basis": "stated", "prov": maya,
         "edge": {"src": "$designer", "rel": "part_of", "dst": "topic:%s" % ns}},
        {"op": "add_node", "ref": "$priya", "basis": "stated", "prov": priya,
         "node": {"kind": "person", "name": "Priya", "summary": "Paints the cards."}},
        {"op": "add_edge", "basis": "stated", "prov": priya,
         "edge": {"src": "$priya", "rel": "member_of", "dst": "$designer"}},
    ]


class PutOffTest(_support.TempCase):
    """Stage 9 (every other stage skipped), where a gap question outscores the catch-all by far: a question the user
    just skipped or put off leaves the next turn's questions and the status Next line, is named under held_back, and
    is listed again (flagged) only when nothing else is open or once its 2 days pass."""

    STAGES = ("q.frame.goal", "q.people.key", "q.data.where", "q.vocab.terms", "q.process.main",
              "q.constraints.rules", "q.deliverables.more", "q.questions.open")
    THIN = "q.gap.thin@topic:bg"
    PRIYA = "q.gap.missing_relation.works_on.out@person:priya"

    def setUp(self):
        super().setUp()
        clear()
        self.root = _support.init_topic(self.tmp, "bg", "Board game")
        self.repo = store.Repo.open(self.root)
        interview.answer(self.repo, "q.frame.you", "I am Maya, the designer. Priya paints the cards.",
                         ops=designer_ops("bg"), apply=True)
        for qid in self.STAGES:
            interview.answer(self.repo, qid, None, status="skip_stage")

    def onto(self):
        clear()
        return graph.Ontology.load(self.repo)

    def top(self, n=3):
        return [q["id"] for q in interview.next_questions(self.onto(), n)]

    def next_json(self, **args):
        clear()
        ctx = commands.Context(repo=self.repo, mcp=True, profile="full")
        text, is_error, obj = commands.dispatch(commands.get("onto_next"), args, ctx, "json")
        self.assertFalse(is_error, text)
        return obj

    def status_next(self):
        clear()
        code, out, err = _support.run_cli(["status", "--json"], self.root)
        self.assertEqual(code, 0, err)
        return json.loads(out)["next"]["question"]

    def test_a_skipped_top_gap_question_leaves_the_top_n(self):
        self.assertEqual(interview.progress(self.onto())["stage"], 9)
        self.assertEqual(self.top(), [self.THIN, self.PRIYA, interview.CATCH_ALL])
        self.assertEqual(self.status_next(), self.THIN)
        interview.answer(self.repo, self.THIN, None, status="skipped")
        # 488.5 - 30 - 300 still outscores the catch-all (305), yet it is not asked again in the next turn
        self.assertEqual(self.top(), [self.PRIYA, interview.CATCH_ALL])
        self.assertEqual(self.status_next(), self.PRIYA)  # status Next reads the same list
        obj = self.next_json(n=3)
        self.assertEqual(obj["held_back"], {"count": 1, "ids": [self.THIN], "days": 2})
        self.assertEqual((obj["available"], sum(obj["by_stage"].values())), (2, 2))
        self.assertFalse(any(q.get("put_off") for q in obj["questions"]))
        code, out, err = _support.run_cli(["next", "--n", "3"], self.root)
        self.assertEqual(code, 0, err)
        self.assertIn("held back: 1 put off (skipped or later) in the last 2 days, not asked while other questions "
                      "are open: " + self.THIN, out)
        self.assertNotIn(self.THIN + "  ", out)  # named, never listed as a question
        # later holds a question back the same way, without the skip fatigue
        interview.answer(self.repo, self.PRIYA, None, status="later")
        self.assertEqual(self.top(), [interview.CATCH_ALL])
        self.assertEqual(self.status_next(), interview.CATCH_ALL)
        self.assertCountEqual(self.next_json(n=3)["held_back"]["ids"], [self.THIN, self.PRIYA])

    def test_put_off_questions_are_listed_flagged_only_when_nothing_else_is_open(self):
        for qid in (self.THIN, self.PRIYA):
            interview.answer(self.repo, qid, None, status="skipped")
        interview.answer(self.repo, interview.CATCH_ALL, None, status="later")
        found = interview.next_questions(self.onto(), 3)
        self.assertEqual([q["id"] for q in found], [self.THIN, self.PRIYA, interview.CATCH_ALL])
        self.assertEqual(found[0]["put_off"], {"status": "skipped", "until": "2026-09-30T12:00:00Z"})
        self.assertEqual(found[2]["put_off"]["status"], "later")
        self.assertEqual(self.status_next(), self.THIN)
        obj = self.next_json(n=3)
        self.assertNotIn("held_back", obj)
        self.assertEqual(obj["questions"][0]["put_off"], {"status": "skipped", "until": "2026-09-30T12:00:00Z"})
        code, out, err = _support.run_cli(["next", "--n", "3"], self.root)
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[2], "only questions put off in the last 2 days are left: ask one only if the user "
                                   "wants to come back to it")
        self.assertIn("(gap thin on topic:bg, stage 9, put off (skipped) until 2026-09-30, score 158.5)", lines[3])
        # two days on they are ordinary questions again, the skip fatigue kept in the score
        with clock("2026-09-30T12:00:01Z"):
            found = {q["id"]: q for q in interview.next_questions(self.onto(), 10)}
            self.assertIn(self.THIN, found)
            self.assertNotIn("put_off", found[self.THIN])
            self.assertEqual(found[self.THIN]["score"], 458.5)  # 488.5 - 30
            self.assertNotIn("held_back", self.next_json(n=3))

    def test_a_declined_turn_never_comes_back_the_next_turn(self):
        root = _support.make_topic(self.tmp, ns="mini2")  # many open questions of every stage
        repo = store.Repo.open(root)
        clear()
        asked = [q["id"] for q in interview.next_questions(graph.Ontology.load(repo), 3)]
        self.assertEqual(asked, MINI_GOLDEN[:3])
        for qid in asked:
            interview.answer(repo, qid, None, status="skipped")
        clear()
        again = [q["id"] for q in interview.next_questions(graph.Ontology.load(repo), 3)]
        self.assertEqual(len(again), 3)
        self.assertFalse(set(again) & set(asked), again)
        clear()
        code, out, err = _support.run_cli(["status", "--json"], root)
        self.assertEqual(json.loads(out)["next"]["question"], again[0])


class PredicateTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        clear()
        self.root = _support.init_topic(self.tmp, "g2t", "Garden to table")
        self.repo = store.Repo.open(self.root)

    def state(self):
        clear()
        return interview._State(graph.Ontology.load(self.repo))

    def test_imported_nodes_satisfy_count_predicates(self):
        state = self.state()
        self.assertTrue(state.holds({"count": {"kind": "dataset", "lt": 3}}))
        self.assertIn("q.data.where", [q["id"] for q in interview.next_questions(state.onto, 10)])
        logs = [mk_node("dataset:%s-log" % w, "%s log" % w.capitalize()) for w in ("harvest", "seed", "water")]
        add_import(self.repo, "garden", make_export("garden", logs))
        state = self.state()
        self.assertTrue(state.holds({"count": {"kind": "dataset", "gte": 3}}))
        self.assertFalse(state.holds({"count": {"kind": "dataset", "lt": 3}}))
        self.assertTrue(state.holds({"count": {"kind": "dataset", "ns": "self", "lt": 1}}))
        self.assertTrue(state.holds({"count": {"kind": "dataset", "ns": "garden", "gte": 3}}))
        # the parent answered it: q.data.where is not asked, and it no longer holds the quick start open
        self.assertNotIn("q.data.where", [q["id"] for q in interview.next_questions(state.onto, 40)])
        self.assertNotIn("q.data.where", interview.progress(state.onto)["quick"]["open"])
        self.assertTrue(state.holds({"has_imports": True}))
        self.assertFalse(state.holds({"has_imports": False}))

    def test_other_predicates(self):
        state = self.state()
        self.assertFalse(state.holds({"answered": "q.frame.goal"}))
        self.assertTrue(state.holds({"not_answered": "q.frame.goal"}))
        self.assertFalse(state.holds({"stage_done": 0}))
        self.assertFalse(state.holds({"missing_field": {"kind": "dataset", "field": "format"}}))  # no dataset yet
        self.assertFalse(state.holds({"unknown": 1}))
        interview.answer(self.repo, "q.frame.goal", "A weekly menu built from what the garden grows.")
        state = self.state()
        self.assertTrue(state.holds({"answered": "q.frame.goal"}))
        self.assertFalse(state.holds({"not_answered": "q.frame.goal"}))

    def test_stage_eight_needs_the_meet_answer_and_a_bridge(self):
        add_import(self.repo, "garden", make_export("garden", [mk_node("crop:tomato", "Tomato")]))
        add_import(self.repo, "kitchen", make_export("kitchen", [mk_node("ingredient:tomato", "Tomato")]),
                   commit="beef" + "0" * 36)
        state = self.state()
        stages = [s["n"] for s in interview.progress(state.onto)["stages"]]
        self.assertIn(8, stages)
        self.assertEqual(interview.next_questions(state.onto, 1, stage=8)[0]["id"], interview.MEET)
        self.assertFalse(state.stage_done(8))
        interview.answer(self.repo, interview.MEET, "The harvest log feeds the menu planning.")
        self.assertFalse(self.state().stage_done(8))  # no bridge between garden and kitchen yet
        edges, _ = store.read_jsonl(self.repo.path("graph/edges.jsonl"))
        bridge = mk_edge("garden/crop:tomato", "same_as", "kitchen/ingredient:tomato", symmetric=True,
                         prov=[{"src": "imp:garden@" + COMMIT[:12], "loc": "garden/crop:tomato", "by": "agent"}])
        store.write_jsonl(self.repo.path("graph/edges.jsonl"), edges + [bridge])
        state = self.state()
        self.assertTrue(state.stage_done(8))
        self.assertEqual(interview.progress(state.onto)["import_pairs"], [{"pair": "garden|kitchen",
                                                                          "bridged": True}])

    def add_local(self, nodes=(), edges=()):
        for path, rows in (("graph/nodes.jsonl", nodes), ("graph/edges.jsonl", edges)):
            old, _ = store.read_jsonl(self.repo.path(path))
            store.write_jsonl(self.repo.path(path), old + list(rows))

    def test_stage_eight_counts_only_local_bridges(self):
        # the kitchen export joins garden and kitchen nodes itself: that edge is not a bridge of this topic (C.16)
        add_import(self.repo, "garden", make_export("garden", [mk_node("crop:tomato", "Tomato")]))
        add_import(self.repo, "kitchen", make_export("kitchen", [mk_node("crop:tomato", "Tomato")], [
            mk_edge("garden/crop:tomato", "same_as", "crop:tomato", symmetric=True)]), commit="beef" + "0" * 36)
        interview.answer(self.repo, interview.MEET, "The harvest log feeds the menu planning.")
        state = self.state()
        self.assertTrue(state.onto.bridges)  # the imported edge joins two namespaces
        self.assertFalse(state.stage_done(8))
        self.assertEqual(interview.progress(state.onto)["import_pairs"], [{"pair": "garden|kitchen",
                                                                          "bridged": False}])

    def test_a_bundled_import_counts_as_its_direct_import(self):
        add_import(self.repo, "g2t", make_export("g2t", [mk_node("goal:weekly-menu", "Weekly menu")]))
        add_import(self.repo, "garden", make_export("garden", [mk_node("crop:tomato", "Tomato")]), via="g2t",
                   commit="beef" + "0" * 36)
        prov = [{"src": "imp:garden@beef00000000", "loc": "garden/crop:tomato", "by": "agent"}]
        self.add_local([mk_node("goal:stall-sales", "Stall sales")],
                       [mk_edge("garden/crop:tomato", "related_to", "goal:stall-sales", symmetric=True, prov=prov)])
        state = self.state()
        self.assertEqual(state.pairs(), [("g2t", "self")])
        self.assertFalse(state.stage_done(8))  # q.compose.meet is not answered yet
        interview.answer(self.repo, interview.MEET, "The stall sells what the garden grows.")
        state = self.state()
        self.assertTrue(state.stage_done(8))
        self.assertEqual(interview.progress(state.onto)["import_pairs"], [{"pair": "g2t|self", "bridged": True}])

    def test_conflicts_between_imported_nodes_are_asked_in_stage_eight(self):
        add_import(self.repo, "garden", make_export("garden", [
            mk_node("dataset:harvest-log", "Harvest log", attrs={"format": "paper"})]))
        add_import(self.repo, "kitchen", make_export("kitchen", [
            mk_node("dataset:harvest-log", "Harvest log", attrs={"format": "spreadsheet"})]),
            commit="beef" + "0" * 36)
        prov = [{"src": "imp:garden@" + COMMIT[:12], "loc": "garden/dataset:harvest-log", "by": "agent"}]
        self.add_local(edges=[
            mk_edge("garden/dataset:harvest-log", "same_as", "kitchen/dataset:harvest-log", symmetric=True, prov=prov),
            mk_edge("garden/dataset:harvest-log", "related_to", "kitchen/dataset:old-log", symmetric=True, prov=prov),
        ])
        conflict = "q.gap.conflict.format@garden/dataset:harvest-log"
        dangling = "q.gap.dangling_bridge@garden/dataset:harvest-log"
        found = {q["id"]: q for q in interview.next_questions(self.state().onto, 20, stage=8)}
        self.assertIn(conflict, found)
        self.assertIn(dangling, found)
        self.assertNotIn("q.gap.conflict.format@kitchen/dataset:harvest-log", found)  # one question per class
        self.assertEqual((found[conflict]["stage"], found[conflict]["node"]), (8, "garden/dataset:harvest-log"))
        self.assertIn("paper (garden) vs spreadsheet (kitchen)", found[conflict]["ask"])
        # only what the local bridges made is asked about an imported node (it is read-only here)
        asked = [q["id"] for q in interview.next_questions(self.state().onto, 40)]
        self.assertEqual(sorted(i for i in asked if "@garden/" in i or "@kitchen/" in i), [conflict, dangling])
        res = interview.answer(self.repo, conflict, "The garden writes it on paper; the kitchen types it up.")
        self.assertEqual(res["question"]["node"], "garden/dataset:harvest-log")
        interview.answer(self.repo, dangling, None, status="na")
        rows, _ = store.read_jsonl(self.repo.path(interview.LOG))
        self.assertEqual([(r["q"], r["node"]) for r in rows[-2:]],
                         [("q.gap.conflict.format", "garden/dataset:harvest-log"),
                          ("q.gap.dangling_bridge", "garden/dataset:harvest-log")])
        self.assertEqual([records.check(r, "answer") for r in rows[-2:]], [[], []])
        asked = [q["id"] for q in interview.next_questions(self.state().onto, 40)]
        self.assertFalse([i for i in asked if "@garden/" in i or "@kitchen/" in i])
        # skip_stage on it names the stage 8 question to use instead
        with self.assertRaises(UsageError) as caught:
            interview.answer(self.repo, conflict, None, status="skip_stage")
        self.assertIn("answer q.compose.meet with status skip_stage to skip stage 8", caught.exception.message)
        # other gap types are still refused on an imported node
        with self.assertRaises(NotFound):
            interview.answer(self.repo, "q.gap.orphan@garden/dataset:harvest-log", "text")

    def test_no_stage_eight_without_imports(self):
        state = self.state()
        self.assertNotIn(8, [s["n"] for s in interview.progress(state.onto)["stages"]])
        self.assertEqual(interview.next_questions(state.onto, 5, stage=8), [])


def stated(kind, ref, name, quote, **extra):
    node = dict({"kind": kind, "name": name, "summary": "%s, as the user said." % name}, **extra)
    return {"op": "add_node", "ref": ref, "basis": "stated", "node": node, "prov": [{"quote": quote}]}


def link(src, rel, dst, quote):
    return {"op": "add_edge", "basis": "stated", "edge": {"src": src, "rel": rel, "dst": dst},
            "prov": [{"quote": quote}]}


class BankRepeatTest(_support.TempCase):
    """A bank question leaves ``next`` once the graph answers it, and never sits next to the gap question that asks
    the same of one node (bank-questions-repeat-answered)."""

    def setUp(self):
        super().setUp()
        clear()
        self.root = _support.init_topic(self.tmp, "bees", "Rooftop bees")
        self.repo = store.Repo.open(self.root)

    def asked(self):
        clear()
        return {q["id"]: q for q in interview.next_questions(graph.Ontology.load(self.repo), 80)}

    def answer(self, q, text, ops):
        res = interview.answer(self.repo, q, text, ops=ops, apply=True)
        self.assertEqual(res["proposal"]["status"], "applied", res)
        return res

    def test_owner_and_measure_leave_once_the_graph_answers_them(self):
        text = "Healthy hives through the winter. We watch the winter survival rate."
        self.answer("q.frame.goal", text, [
            stated("goal", "$g", "Healthy hives", "Healthy hives through the winter"),
            stated("metric", "$m", "Winter survival rate", "the winter survival rate"),
            link("$m", "measures", "$g", "We watch the winter survival rate")])
        text = "Everything is in the hive inspection log, which the hive keeper fills in."
        self.answer("q.data.where", text, [
            stated("dataset", "$d", "Hive inspection log", "the hive inspection log"),
            stated("role", "$r", "Hive keeper", "the hive keeper fills in"),
            link("$r", "owns", "$d", "which the hive keeper fills in")])
        clear()
        onto = graph.Ontology.load(self.repo)
        self.assertEqual([g for need in needs.all_needs(onto).values() for g in need.get("gaps") or []
                          if g.get("type") == "missing_relation"], [])
        asked = self.asked()
        self.assertNotIn("q.data.owner", asked)
        self.assertNotIn("q.deliverables.measure", asked)
        self.assertFalse([i for i in asked if i.startswith("q.gap.missing_relation")])

    def test_the_owner_question_stands_in_only_until_a_data_source_is_recorded(self):
        interview.answer(self.repo, "q.data.where", "In a notebook in the shed.")  # no ops: nothing recorded
        asked = self.asked()
        self.assertEqual(asked["q.data.owner"]["ask"], "Who keeps each data source for Rooftop bees up to date?")
        self.answer("q.deepen.more", "We also keep a hive inspection log.", [
            stated("dataset", "$d", "Hive inspection log", "a hive inspection log")])
        asked = self.asked()
        owner_asks = sorted(i for i, q in asked.items() if "up to date" in q["ask"])
        self.assertEqual(owner_asks, ["q.gap.missing_relation.owns.in@dataset:hive-inspection-log"])
        self.answer("q.gap.missing_relation.owns.in@dataset:hive-inspection-log", "The hive keeper fills it in.", [
            stated("role", "$r", "Hive keeper", "The hive keeper"),
            link("$r", "owns", "dataset:hive-inspection-log", "The hive keeper fills it in")])
        self.assertFalse([i for i, q in self.asked().items() if "up to date" in q["ask"]])

    def test_the_measure_question_leaves_with_the_first_metric(self):
        self.answer("q.frame.goal", "Healthy hives through the winter.", [
            stated("goal", "$g", "Healthy hives", "Healthy hives through the winter")])
        self.assertIn("q.deliverables.measure", self.asked())
        gap = "q.gap.missing_relation.measures.in@goal:healthy-hives"
        self.answer(gap, "We count the colonies alive in spring.", [
            stated("metric", "$m", "Spring colony count", "the colonies alive in spring"),
            link("$m", "measures", "goal:healthy-hives", "We count the colonies alive in spring")])
        asked = self.asked()
        self.assertNotIn("q.deliverables.measure", asked)
        self.assertNotIn(gap, asked)

    def test_the_main_process_question_leaves_at_the_dimension_target(self):
        target = packs.load(None, {"packs": ["core", "discovery"]}).dimensions()["process"]["target"]
        self.assertEqual(target, 2)
        self.answer("q.deepen.more", "We inspect every hive each week.", [
            stated("process", "$p", "Hive inspection", "We inspect every hive each week",
                   attrs={"cadence": "weekly"})])
        self.assertIn("q.process.main", self.asked())
        self.answer("q.deepen.more", "We wrap the hives each autumn.", [
            stated("process", "$p", "Winter wrapping", "We wrap the hives each autumn",
                   attrs={"cadence": "yearly"})])
        asked = self.asked()
        self.assertNotIn("q.process.main", asked)
        self.assertIn("q.process.steps", asked)  # the process stage still has more to ask


# answer ---------------------------------------------------------------------------------------------------------
TEXT = "We want a weekly harvest sheet for member households, kept by the plot coordinator."


def sheet_ops(edge_quote="a weekly harvest sheet"):
    return [
        {"op": "add_node", "ref": "$sheet", "basis": "stated",
         "node": {"kind": "deliverable", "name": "Weekly harvest sheet", "summary": "What was picked each week.",
                  "attrs": {"audience": "member households"}},
         "prov": [{"quote": "a weekly harvest sheet for member households"}]},
        {"op": "add_edge", "basis": "stated", "edge": {"src": "$sheet", "rel": "serves", "dst": "goal:shared-harvest"},
         "prov": [{"src": "$answer", "quote": edge_quote}]},
        {"op": "add_edge", "edge": {"src": "role:plot-coordinator", "rel": "owns", "dst": "$sheet"}},
    ]


class AnswerTest(Base):
    def test_stated_ops_confirmed_by_the_user_and_drafts(self):
        res = interview.answer(self.repo, "q.frame.deliverable", TEXT,
                               ops=sheet_ops(edge_quote="a sheet that feeds every household"), apply=True)
        self.assertEqual(res["verdicts"], {"1": "accept", "2": "draft", "3": "draft"})
        self.assertEqual(res["proposal"]["status"], "applied")
        self.assertIn("dropped from op 2", " ".join(res["notes"]))
        src = res["answer"]["src"]
        onto = self.onto()
        self.assertEqual(onto.sources[src]["kind"], "interview")
        self.assertEqual(sources.read(self.repo, src), TEXT + "\n")
        node = onto.node("deliverable:weekly-harvest-sheet")
        self.assertEqual((node["status"], node["trust"]), ("confirmed", "user"))
        self.assertEqual(node["prov"], [{"src": src, "loc": "Q:q.frame.deliverable", "by": "user",
                                         "quote": "a weekly harvest sheet for member households"}])
        serves = [e for e in onto.edges.values() if e["rel"] == "serves" and e["src"] == node["id"]][0]
        self.assertEqual((serves["status"], serves["trust"]), ("proposed", "agent"))  # quote not found: a draft
        self.assertEqual(serves["prov"], [{"src": src, "loc": "Q:q.frame.deliverable", "by": "user"}])
        owns = [e for e in onto.edges.values() if e["rel"] == "owns" and e["dst"] == node["id"]][0]
        self.assertEqual((owns["status"], owns["trust"]), ("proposed", "agent"))  # inferred: a draft
        row = self.log()[-1]
        self.assertEqual((row["q"], row["status"], row["src"], row["proposal"], row["node"]),
                         ("q.frame.deliverable", "answered", src, res["proposal"]["id"], None))
        self.assertEqual(records.check(row, "answer"), [])
        change = ledger.read_changes(self.repo)[-1]
        self.assertEqual((change["type"], change["by"], change["source"], change["proposal"]),
                         ("answer", "user", src, res["proposal"]["id"]))
        self.assertEqual(pipeline.load(self.repo, res["proposal"]["id"])["review"]["by"], "user")
        self.assertEqual(res["applied"]["change"], change["id"])
        self.assert_valid()

    def test_an_applied_answer_names_the_nodes_it_added(self):
        text = "I run the seed swap for the Riverside allotment society."
        ops = [{"op": "add_node", "ref": "$swap", "basis": "stated", "node": {"kind": "process", "name": "Seed swap"},
                "prov": [{"quote": "I run the seed swap"}]},
               {"op": "add_node", "ref": "$club", "node": {"kind": "org", "name": "Riverside allotment society"}},
               {"op": "add_edge", "basis": "stated", "edge": {"src": "role:plot-coordinator", "rel": "owns",
                                                              "dst": "$swap"},
                "prov": [{"quote": "I run the seed swap"}]}]
        path = os.path.join(self.tmp, "ops.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(ops, fh)
        code, out, err = _support.run_cli(["answer", "q.deepen.more", text, "--ops", "@" + path, "--apply"],
                                          self.root)
        self.assertEqual(code, 0, err)
        self.assertIn("added: Seed swap, Riverside allotment society (draft)", out.splitlines())
        self.assertNotIn("not in the ontology yet", out)
        added = [{"n": 1, "id": "process:seed-swap", "name": "Seed swap", "status": "confirmed"},
                 {"n": 2, "id": "org:riverside-allotment-society", "name": "Riverside allotment society",
                  "status": "proposed"}]
        again = interview.answer(self.repo, "q.deepen.more", text, ops=ops, apply=True)  # the retried call
        self.assertIn("already recorded", again["notes"][-1])
        self.assertEqual(again["applied"]["added"], added)
        self.assertEqual(again["proposal"]["new_terms"], [])
        self.assertEqual(pipeline.load(self.repo, again["proposal"]["id"])["new_terms"],
                         ["Seed swap", "Riverside allotment society"])  # the proposal record is left as prepared
        for item in added:
            self.assertEqual(self.onto().node(item["id"])["status"], item["status"])
        # without apply nothing is added yet, so the new names are still not in the ontology
        code, out, err = _support.run_cli(["answer", "q.vocab.terms", "A tilth is fine, crumbly topsoil.", "--ops",
                                           json.dumps([{"op": "add_node", "node": {"kind": "term", "name": "Tilth"},
                                                        "prov": [{"quote": "fine, crumbly topsoil"}]}])], self.root)
        self.assertEqual(code, 0, err)
        self.assertIn('not in the ontology yet: Tilth', out)
        self.assertNotIn("added:", out)
        self.assert_valid()

    def test_found_quote_on_a_stated_edge_is_confirmed(self):
        res = interview.answer(self.repo, "q.frame.deliverable", TEXT, ops=sheet_ops(), apply=True)
        self.assertEqual(res["verdicts"], {"1": "accept", "2": "accept", "3": "draft"})
        onto = self.onto()
        serves = [e for e in onto.edges.values() if e["rel"] == "serves"
                  and e["src"] == "deliverable:weekly-harvest-sheet"][0]
        self.assertEqual((serves["status"], serves["trust"]), ("confirmed", "user"))
        clear()
        self.assertNotIn("q.frame.deliverable", self.next_ids())

    def test_without_apply_the_ops_wait_as_a_pending_proposal(self):
        res = interview.answer(self.repo, "q.frame.deliverable", TEXT, ops=sheet_ops())
        self.assertIsNone(res["applied"])
        self.assertIsNone(res["verdicts"])
        prop = pipeline.load(self.repo, res["proposal"]["id"])
        self.assertEqual((prop["status"], prop["source"], prop["by"]), ("pending", res["answer"]["src"], "agent"))
        self.assertNotIn("deliverable:weekly-harvest-sheet", self.onto().nodes)
        change = ledger.read_changes(self.repo)[-1]
        self.assertEqual((change["type"], change["proposal"], change["ids"]),
                         ("answer", prop["id"], [res["answer"]["src"]]))
        self.assert_valid()

    def test_other_statuses_append_only_a_log_line(self):
        before = _support.snapshot(self.root, skip=[".onto"])
        for status in ("skipped", "later", "na", "skip_stage"):
            res = interview.answer(self.repo, "q.process.main", "not now, sorry", status=status)
            self.assertIsNone(res["answer"]["src"])
            self.assertIn("not stored", res["notes"][0])
        after = _support.snapshot(self.root, skip=[".onto"])
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        self.assertEqual(changed, ["interview/log.jsonl"])
        rows = self.log()[-4:]
        self.assertEqual([r["status"] for r in rows], ["skipped", "later", "na", "skip_stage"])
        self.assertEqual(len({r["id"] for r in rows}), 4)  # one clock second, four ids
        with self.assertRaises(UsageError):
            interview.answer(self.repo, "q.process.main", None, status="skipped", ops=sheet_ops())
        self.assert_valid()

    def test_refused_ops_keep_nothing(self):
        before = _support.snapshot(self.root, skip=[".onto"])
        unknown = [{"op": "add_node", "basis": "stated", "node": {"kind": "no-such-kind", "name": "Compost"},
                    "prov": [{"quote": "Compost is rotted garden waste."}]}]
        fabricated = [{"op": "add_node", "node": {"kind": "term", "name": "Compost"},
                       "prov": [{"quote": "Compost is turned every Sunday."}]}]
        with self.assertRaises(Refused):  # prepare refuses the kind after the source was stored: it is removed
            interview.answer(self.repo, "q.vocab.terms", "Compost is rotted garden waste.", ops=unknown, apply=True)
        with self.assertRaises(Refused):  # without apply a quote the answer does not hold refuses the answer
            interview.answer(self.repo, "q.vocab.terms", "Compost is rotted garden waste.", ops=fabricated)
        self.assertEqual(before, _support.snapshot(self.root, skip=[".onto"]))

    def test_ops_citing_only_another_source_are_refused(self):
        """An answer's op must cite the answer: content from an ingested source keeps its untrusted mark."""
        before = _support.snapshot(self.root, skip=[".onto"])
        mulch = [{"op": "add_node", "node": {"kind": "term", "name": "Mulch"},
                  "prov": [{"src": NOTE, "loc": "L11-L11", "quote": "Mulch keeps the soil moist", "by": "agent"}]}]
        with self.assertRaises(Refused) as caught:
            interview.answer(self.repo, "q.vocab.terms", "Okay.", ops=mulch, apply=True)
        self.assertIn("op 1 cites another source and not the answer", caught.exception.message)
        self.assertIn(NOTE, caught.exception.message)
        self.assertIn("onto propose", caught.exception.message)
        self.assertEqual(caught.exception.problems[0]["n"], 1)
        with self.assertRaises(Refused):
            interview.answer(self.repo, "q.vocab.terms", "Okay.", ops=mulch)
        self.assertEqual(before, _support.snapshot(self.root, skip=[".onto"]))
        self.assertNotIn("term:mulch", self.onto().nodes)
        # citing the answer as well is an answer's op: it goes in (a draft, since it is not stated)
        text = "Mulch keeps the soil moist, as the handbook says."
        both = [dict(mulch[0], prov=mulch[0]["prov"] + [{"quote": "Mulch keeps the soil moist"}])]
        res = interview.answer(self.repo, "q.vocab.terms", text, ops=both, apply=True)
        self.assertEqual(res["verdicts"], {"1": "draft"})
        node = self.onto().node("term:mulch")
        self.assertEqual(node["status"], "proposed")
        self.assert_valid()

    def test_credentials_are_refused_before_anything_is_written(self):
        before = _support.snapshot(self.root, skip=[".onto"])
        with self.assertRaises(Refused):
            interview.answer(self.repo, "q.data.where", "The key is %s" % _support.fake_secret("github"))
        self.assertEqual(before, _support.snapshot(self.root, skip=[".onto"]))

    def test_text_is_needed_and_question_ids_are_checked(self):
        with self.assertRaises(UsageError):
            interview.answer(self.repo, "q.data.where", "  ")
        with self.assertRaises(UsageError):
            interview.answer(self.repo, "q.no.such.question", "text")
        with self.assertRaises(UsageError):
            interview.answer(self.repo, "not a question", "text")
        with self.assertRaises(UsageError):
            interview.answer(self.repo, "q.gap.orphan", "text")  # a gap question needs its node
        with self.assertRaises(UsageError):
            interview.answer(self.repo, "q.data.where@dataset:harvest-log", "text")  # a bank question takes none
        with self.assertRaises(NotFound):
            interview.answer(self.repo, "q.gap.orphan@term:nothing-like-it", "text")
        with self.assertRaises(UsageError):
            interview.answer(self.repo, "q.data.where", "text", status="maybe")

    def test_gap_question_answered_on_its_node(self):
        gap_id = "q.gap.missing_relation.measures.in@goal:shared-harvest"
        text = "We count the households that get a share each week."
        ops = [{"op": "add_node", "ref": "$m", "basis": "stated",
                "node": {"kind": "metric", "name": "Households served", "summary": "Households that got a share.",
                         "attrs": {"unit": "households"}},
                "prov": [{"quote": "the households that get a share each week"}]},
               {"op": "add_edge", "basis": "stated", "edge": {"src": "$m", "rel": "measures",
                                                              "dst": "goal:shared-harvest"},
                "prov": [{"quote": "count the households"}]}]
        res = interview.answer(self.repo, "q.gap.missing_relation.measures.in@shared-harvest", text, ops=ops,
                               apply=True)
        self.assertEqual(res["resolved"]["id"], "goal:shared-harvest")
        self.assertEqual(res["question"]["id"], gap_id)
        self.assertEqual(res["verdicts"], {"1": "accept", "2": "accept"})
        row = self.log()[-1]
        self.assertEqual((row["q"], row["node"]), ("q.gap.missing_relation.measures.in", "goal:shared-harvest"))
        onto = self.onto()
        self.assertEqual(onto.node("metric:households-served")["prov"][0]["loc"],
                         "Q:q.gap.missing_relation.measures.in")
        self.assertIn("Answer to q.gap.missing_relation.measures.in on goal:shared-harvest",
                      onto.sources[res["answer"]["src"]]["title"])
        clear()
        self.assertNotIn(gap_id, self.next_ids())
        self.assert_valid()

    def test_repeatable_question_comes_back(self):
        interview.answer(self.repo, "q.vocab.terms", "Mulch is straw laid over the soil.")
        clear()
        self.assertIn("q.vocab.terms", self.next_ids(40))
        base = self.scores()["q.vocab.terms"]
        with clock("2026-10-01T12:00:00Z"):
            clear()
            self.assertEqual(self.scores()["q.vocab.terms"], base + 300)
            second = interview.answer(self.repo, "q.vocab.terms", "A bed is one raised plot.")
        titles = [e["title"] for e in sources.index(self.repo).values() if e["title"].startswith("Answer to q.vocab")]
        self.assertEqual(len(titles), 2)
        self.assertIsNone(sources.index(self.repo)[second["answer"]["src"]]["supersedes"])

    def test_a_retried_answer_writes_nothing(self):
        first = interview.answer(self.repo, "q.frame.deliverable", TEXT, ops=sheet_ops(), apply=True)
        before = _support.snapshot(self.root, skip=[".onto"])
        again = interview.answer(self.repo, "q.frame.deliverable", TEXT, ops=sheet_ops(), apply=True)
        self.assertEqual(before, _support.snapshot(self.root, skip=[".onto"]))
        self.assertEqual(again["answer"]["id"], first["answer"]["id"])
        self.assertEqual(again["proposal"]["id"], first["proposal"]["id"])
        self.assertEqual(again["applied"]["ids"], first["applied"]["ids"])
        self.assertEqual(again["verdicts"], first["verdicts"])
        self.assertIn("already recorded", again["notes"][-1])
        # other ops for the same text are a new answer
        changed = sheet_ops()[:1]
        changed[0]["node"]["name"] = "Weekly pick list"
        third = interview.answer(self.repo, "q.frame.deliverable", TEXT, ops=changed, apply=True)
        self.assertNotEqual(third["proposal"]["id"], first["proposal"]["id"])
        self.assertIn("deliverable:weekly-pick-list", third["applied"]["ids"])
        self.assertNotIn("deliverable:weekly-harvest-sheet-2", self.onto().nodes)
        self.assert_valid()

    def test_a_retried_pending_answer_writes_nothing(self):
        first = interview.answer(self.repo, "q.frame.deliverable", TEXT, ops=sheet_ops())
        before = _support.snapshot(self.root, skip=[".onto"])
        again = interview.answer(self.repo, "q.frame.deliverable", TEXT, ops=sheet_ops())
        self.assertEqual(before, _support.snapshot(self.root, skip=[".onto"]))
        self.assertEqual(again["proposal"]["id"], first["proposal"]["id"])
        # applying it later goes through: the open proposal is reused
        applied = interview.answer(self.repo, "q.frame.deliverable", TEXT, ops=sheet_ops(), apply=True)
        self.assertEqual(applied["proposal"]["status"], "applied")
        self.assertEqual(len([p for p in pipeline.pending(self.repo) if p["id"] == first["proposal"]["id"]]), 0)
        self.assert_valid()

    def test_same_text_twice_reuses_the_source(self):
        one = interview.answer(self.repo, "q.frame.scope", "Only the garden, not the orchard.")
        two = interview.answer(self.repo, "q.deepen.more", "Only the garden, not the orchard.")
        self.assertEqual(one["answer"]["src"], two["answer"]["src"])
        self.assertTrue(two["answer"]["duplicate"])
        self.assertNotEqual(one["answer"]["id"], two["answer"]["id"])
        self.assertEqual([r["q"] for r in self.log()[-2:]], ["q.frame.scope", "q.deepen.more"])
        self.assert_valid()

    def test_a_retried_answer_without_ops_writes_nothing(self):
        first = interview.answer(self.repo, "q.frame.scope", "Only the garden, not the orchard.")
        before = _support.snapshot(self.root, skip=[".onto"])
        again = interview.answer(self.repo, "q.frame.scope", "Only the garden, not the orchard.")
        self.assertEqual(before, _support.snapshot(self.root, skip=[".onto"]))  # no log line, no change entry
        self.assertEqual(again["answer"], {"id": first["answer"]["id"], "src": first["answer"]["src"],
                                           "duplicate": True})
        self.assertIn("already recorded as %s with the same text;" % first["answer"]["id"], again["notes"][-1])
        # a different text is a new answer
        third = interview.answer(self.repo, "q.frame.scope", "The garden and the orchard.")
        self.assertFalse(third["answer"]["duplicate"])
        self.assertEqual([r["q"] for r in self.log()].count("q.frame.scope"), 2)
        self.assert_valid()


class ConfirmTest(Base):
    TEXT = "The harvest log is filled in every week now."

    def ops(self):
        return [{"op": "update_node", "id": "dataset:harvest-log", "set": {"attrs.cadence": "weekly"},
                 "reason": "the answer says the log is filled in every week", "basis": "stated",
                 "prov": [{"quote": "filled in every week"}]},
                {"op": "archive", "id": "term:companion-planting",
                 "archived": {"reason": "the answer folds it into the pest notes for this test",
                              "decision": DECISION, "superseded_by": []}}]

    def test_destructive_ops_need_confirm_on_the_cli(self):
        path = os.path.join(self.tmp, "ops.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.ops(), fh)
        before = _support.snapshot(self.root, skip=[".onto"])
        code, out, err = _support.run_cli(["answer", "q.deepen.more", self.TEXT, "--ops", "@" + path, "--apply"],
                                          self.root)
        self.assertEqual(code, 1, err)
        self.assertIn("destructive: 1 (update_node), 2 (archive) need confirm=true", out)
        self.assertIn("Preview only; nothing written", out)
        self.assertEqual(before, _support.snapshot(self.root, skip=[".onto"]))  # not the source, not the log
        code, out, err = _support.run_cli(["answer", "q.deepen.more", self.TEXT, "--ops", "@" + path, "--apply",
                                           "--confirm"], self.root)
        self.assertEqual(code, 0, err)
        clear()
        onto = self.onto()
        log = onto.node("dataset:harvest-log")
        self.assertEqual((log["attrs"]["cadence"], log["trust"]), ("weekly", "user"))
        self.assertEqual(onto.node("term:companion-planting")["status"], "archived")
        self.assert_valid()

    def test_destructive_ops_need_confirm_over_mcp(self):
        cmd = commands.get("onto_answer")
        args = {"q": "q.deepen.more", "text": self.TEXT, "ops": self.ops(), "apply": True}
        before = _support.snapshot(self.root, skip=[".onto"])
        text, is_error, obj = commands.dispatch(cmd, args, commands.Context(repo=self.repo, mcp=True, profile="full"))
        self.assertTrue(is_error)
        self.assertTrue(obj["preview"])
        self.assertIn("Call again with confirm=true", text)
        self.assertEqual(before, _support.snapshot(self.root, skip=[".onto"]))
        text, is_error, obj = commands.dispatch(cmd, dict(args, confirm=True),
                                                commands.Context(repo=self.repo, mcp=True, profile="full"))
        self.assertFalse(is_error, text)
        self.assertEqual(obj["verdicts"], {"1": "accept", "2": "accept"})
        self.assertEqual(obj["proposal"]["status"], "applied")

    def test_no_confirm_needed_without_destructive_ops(self):
        cmd = commands.get("onto_answer")
        ctx = commands.Context(repo=self.repo, mcp=True, profile="full")
        text, is_error, obj = commands.dispatch(cmd, {"q": "q.frame.deliverable", "text": TEXT, "ops": sheet_ops(),
                                                      "apply": True}, ctx)
        self.assertFalse(is_error, text)
        self.assertEqual(obj["proposal"]["status"], "applied")
        self.assertFalse(ctx.preview)
        text, is_error, obj = commands.dispatch(cmd, {"q": "q.process.main", "status": "later"}, ctx)
        self.assertFalse(is_error, text)
        self.assertIn("log only", text)
        # without apply a destructive op only waits in a pending proposal: no gate
        text, is_error, obj = commands.dispatch(cmd, {"q": "q.deepen.more", "text": self.TEXT, "ops": self.ops()}, ctx)
        self.assertFalse(is_error, text)
        self.assertEqual(obj["proposal"]["status"], "pending")

    def test_pack_changes_in_an_apply_answer_need_confirm(self):
        ops = [{"op": "add_kind", "name": "plot", "kind": {"label": "Plot", "plural": "plots", "dimension": "data"}},
               {"op": "add_question", "question": {"id": "q.local.plots", "ask": "Which plots does {topic} have?",
                                                   "stage": 1, "priority": 20}}]
        self.assertEqual(interview.confirm_ops(self.onto(), ops), [1, 2])
        self.assertEqual(interview.destructive_ops(self.onto(), ops), [])
        cmd = commands.get("onto_answer")
        args = {"q": "q.deepen.more", "text": "We also have plots.", "ops": ops, "apply": True}
        before = _support.snapshot(self.root, skip=[".onto"])
        text, is_error, obj = commands.dispatch(cmd, args, commands.Context(repo=self.repo, mcp=True, profile="full"))
        self.assertTrue(is_error)
        self.assertIn("pack or question changes: 1 (add_kind), 2 (add_question) need confirm=true", text)
        self.assertNotIn("destructive:", text)
        path = os.path.join(self.tmp, "pack-ops.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(ops, fh)
        code, out, err = _support.run_cli(["answer", "q.deepen.more", "We also have plots.", "--ops", "@" + path,
                                           "--apply"], self.root)
        self.assertEqual(code, 1, err)
        self.assertIn("Preview only; nothing written", out)
        self.assertEqual(before, _support.snapshot(self.root, skip=[".onto"]))
        self.assertIsNone(self.onto().registry.kind("plot"))
        text, is_error, obj = commands.dispatch(cmd, dict(args, confirm=True),
                                                commands.Context(repo=self.repo, mcp=True, profile="full"))
        self.assertFalse(is_error, text)
        self.assertEqual(obj["verdicts"], {"1": "accept", "2": "accept"})
        clear()
        self.assertIsNotNone(self.onto().registry.kind("plot"))
        self.assertIn("q.local.plots", [q["id"] for q in self.onto().registry.questions()])
        self.assert_valid()

    def test_preview_function_writes_nothing(self):
        before = _support.snapshot(self.root, skip=[".onto"])
        res = interview.answer(self.repo, "q.deepen.more", self.TEXT, ops=self.ops(), apply=True, preview=True)
        self.assertEqual(res["preview"]["destructive"], [1, 2])
        self.assertEqual(res["preview"]["confirm"], [1, 2])
        self.assertEqual(res["verdicts"], {"1": "accept", "2": "accept"})
        _clean, src = interview._clean_text(self.repo, self.TEXT)
        self.assertEqual(res["preview"]["source"], src)
        self.assertEqual(before, _support.snapshot(self.root, skip=[".onto"]))
        self.assertEqual(interview.destructive_ops(self.onto(), self.ops()), [1, 2])
        self.assertEqual(res["preview"]["problems"], [])  # the answer is not stored yet, and that is no problem

    # answer-preview-skips-checks
    def bad_sets(self):
        return {
            "short reason and unknown relation": [
                {"op": "update_node", "id": "dataset:harvest-log", "set": {"attrs.cadence": "weekly"},
                 "reason": "short"},
                {"op": "add_edge",
                 "edge": {"src": "dataset:harvest-log", "rel": "flies", "dst": "goal:shared-harvest"}}],
            "archive without a decision": [
                {"op": "archive", "id": "term:companion-planting",
                 "archived": {"reason": "the answer folds it into the pest notes for this test"}}],
            "a location past the end of the answer": [
                {"op": "update_node", "id": "dataset:harvest-log", "set": {"attrs.cadence": "weekly"},
                 "reason": "the answer says the log is filled in every week",
                 "prov": [{"loc": "L7-L9", "quote": "filled in every week"}]}],
        }

    def test_the_preview_runs_the_checks_the_confirmed_call_runs(self):
        before = _support.snapshot(self.root, skip=[".onto"])
        for label, ops in self.bad_sets().items():
            res = interview.answer(self.repo, "q.deepen.more", self.TEXT, ops=ops, apply=True, preview=True)
            problems = res["preview"]["problems"]
            self.assertTrue(problems, label)
            self.assertEqual(before, _support.snapshot(self.root, skip=[".onto"]), label)
            with self.assertRaises(Refused) as caught:
                interview.answer(self.repo, "q.deepen.more", self.TEXT, ops=ops, apply=True)
            self.assertEqual(problems, caught.exception.problems, label)  # every one, in the same words
            self.assertEqual(before, _support.snapshot(self.root, skip=[".onto"]), label)
        codes = [(p["n"], p["code"]) for p in interview.answer(
            self.repo, "q.deepen.more", self.TEXT, ops=self.bad_sets()["short reason and unknown relation"],
            apply=True, preview=True)["preview"]["problems"]]
        self.assertEqual(codes, [(1, "reason"), (2, "P09")])
        # without apply a quote the answer does not hold is a problem, as the real call refuses it
        fabricated = [{"op": "add_node", "node": {"kind": "term", "name": "Weekly log"},
                       "prov": [{"quote": "filled in every Sunday"}]}]
        res = interview.answer(self.repo, "q.deepen.more", self.TEXT, ops=fabricated, preview=True)
        self.assertEqual([(p["n"], p["code"]) for p in res["preview"]["problems"]], [(1, "quote")])
        res = interview.answer(self.repo, "q.deepen.more", self.TEXT, ops=fabricated, apply=True, preview=True)
        self.assertEqual(res["preview"]["problems"], [])  # apply drops it, and the op goes in as a draft
        # a refusal of the whole call (a credential in the ops) is raised by the preview as by the real call
        secret = self.ops()
        secret[0]["set"] = {"summary": "The key is %s" % _support.fake_secret("github")}
        for preview in (True, False):
            with self.assertRaises(Refused):
                interview.answer(self.repo, "q.deepen.more", self.TEXT, ops=secret, apply=True, preview=preview)
        self.assertEqual(before, _support.snapshot(self.root, skip=[".onto"]))

    def test_a_refused_preview_says_so_before_the_confirm(self):
        cmd = commands.get("onto_answer")
        args = {"q": "q.deepen.more", "text": self.TEXT, "ops": self.bad_sets()["short reason and unknown relation"],
                "apply": True}
        before = _support.snapshot(self.root, skip=[".onto"])
        text, is_error, obj = commands.dispatch(cmd, args, commands.Context(repo=self.repo, mcp=True, profile="full"))
        self.assertTrue(is_error)
        self.assertIn("refused: 2 problems; nothing was written, and confirm=true would be refused the same way", text)
        self.assertIn("problem: op 1 reason dataset:harvest-log is confirmed", text)
        self.assertIn("problem: op 2 P09 unknown relation 'flies'", text)
        self.assertNotIn("would store the answer", text)
        self.assertNotIn("Call again with confirm=true", text)
        self.assertEqual((obj["error"], obj["exit_code"], len(obj["problems"])), ("refused", 1, 2))
        self.assertEqual(before, _support.snapshot(self.root, skip=[".onto"]))
        # an archive without a decision is refused at the preview too
        archive = dict(args, ops=self.bad_sets()["archive without a decision"])
        text, is_error, obj = commands.dispatch(cmd, archive, commands.Context(repo=self.repo, mcp=True,
                                                                               profile="full"))
        self.assertTrue(is_error)
        self.assertIn("problem: op 1 P12 an archive needs an active decision", text)
        self.assertEqual(before, _support.snapshot(self.root, skip=[".onto"]))

    def test_a_refused_answer_lists_every_problem(self):
        cmd = commands.get("onto_answer")
        args = {"q": "q.deepen.more", "text": self.TEXT, "ops": self.bad_sets()["short reason and unknown relation"],
                "apply": True, "confirm": True}
        before = _support.snapshot(self.root, skip=[".onto"])
        text, is_error, obj = commands.dispatch(cmd, args, commands.Context(repo=self.repo, mcp=True, profile="full"))
        self.assertTrue(is_error)
        self.assertIn("refused: 2 problems; nothing was written. Fix every problem below and answer again.", text)
        self.assertIn("problem: op 1 reason", text)
        self.assertIn("problem: op 2 P09", text)
        self.assertEqual([(p["n"], p["code"]) for p in obj["problems"]], [(1, "reason"), (2, "P09")])
        path = os.path.join(self.tmp, "bad-ops.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(args["ops"], fh)
        for extra in ([], ["--confirm"]):
            code, out, err = _support.run_cli(["answer", "q.deepen.more", self.TEXT, "--ops", "@" + path, "--apply"]
                                              + extra, self.root)
            self.assertEqual(code, 1, out)
            self.assertIn("problem: op 1 reason", err)
            self.assertIn("problem: op 2 P09", err)
            code, out, err = _support.run_cli(["answer", "q.deepen.more", self.TEXT, "--ops", "@" + path, "--apply",
                                               "--json"] + extra, self.root)
            self.assertEqual((code, json.loads(out)["error"], len(json.loads(out)["problems"])), (1, "refused", 2))
        self.assertEqual(before, _support.snapshot(self.root, skip=[".onto"]))
        # ops citing another source list their problem the same way
        mulch = [{"op": "add_node", "node": {"kind": "term", "name": "Mulch"},
                  "prov": [{"src": NOTE, "loc": "L11-L11", "quote": "Mulch keeps the soil moist", "by": "agent"}]}]
        text, is_error, obj = commands.dispatch(cmd, {"q": "q.vocab.terms", "text": "Okay.", "ops": mulch,
                                                      "apply": True}, commands.Context(repo=self.repo, mcp=True,
                                                                                       profile="full"))
        self.assertTrue(is_error)
        self.assertIn("problem: op 1 foreign_source cites %s and not the answer" % NOTE, text)
        self.assertEqual(before, _support.snapshot(self.root, skip=[".onto"]))


# progress and the commands -------------------------------------------------------------------------------------
class ProgressTest(Base):
    def test_resume_derived_from_the_log(self):
        interview.answer(self.repo, "q.frame.deliverable", TEXT, ops=sheet_ops(), apply=True)
        interview.answer(self.repo, "q.vocab.terms", None, status="later")
        clear()
        first = interview.progress(self.onto())
        clear()
        graph.Ontology.load(self.repo)  # a new session: nothing is kept but the files
        again = interview.progress(graph.Ontology.load(store.Repo.open(self.root)))
        self.assertEqual(first, again)
        self.assertEqual(again["answered"], 5)
        self.assertEqual(again["quick"]["handled"], 5)
        self.assertEqual(again["last_session"]["lines"], 6)
        self.assertEqual(again["last_session"]["last"][-2:], ["q.frame.deliverable", "q.vocab.terms"])
        self.assertEqual(again["last_session"]["by_status"], {"answered": 5, "later": 1})
        self.assertNotIn("q.frame.deliverable", self.next_ids(40))
        for row in self.log():
            self.assertEqual(records.check(row, "answer"), [])

    def test_sessions_split_on_a_long_pause(self):
        with clock("2026-09-29T09:00:00Z"):
            interview.answer(self.repo, "q.process.main", None, status="later")
        with clock("2026-09-29T09:20:00Z"):
            interview.answer(self.repo, "q.constraints.rules", None, status="skipped")
            clear()
            session = interview.progress(self.onto())["last_session"]
        self.assertEqual((session["started"], session["ended"], session["lines"]),
                         ("2026-09-29T09:00:00Z", "2026-09-29T09:20:00Z", 2))

    def test_status_uses_progress_and_next(self):
        code, out, err = _support.run_cli(["status"], self.root)
        self.assertEqual(code, 0, err)
        self.assertIn("stage 0 frame", out)
        self.assertIn("stages: 0 80%, 1 done, 2 done, 3 20%", out)
        self.assertIn("Next: onto answer q.frame.deliverable", out)
        code, out, err = _support.run_cli(["status", "--json"], self.root)
        obj = json.loads(out)
        self.assertEqual(obj["stage"], 0)
        self.assertEqual(obj["next"]["question"], "q.frame.deliverable")

    def test_cli_next_and_answer(self):
        code, out, err = _support.run_cli(["next"], self.root)
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("mini unreleased"))
        self.assertEqual(lines[1], "stage 0 frame | quick start 4/5 | answered 4, skipped 0")
        self.assertTrue(lines[2].startswith("1. q.frame.deliverable  \"What is the first thing"))
        self.assertIn("quick start, stage 0, score 270.0", lines[2])
        # review round 4: the CLI reads the user's words from a file, never from a shell line
        self.assertIn("Answer: onto answer q.frame.deliverable --text-file .onto/answer.txt --apply (with the user's "
                      "words saved in .onto/answer.txt first)", out)
        code, out, err = _support.run_cli(["next", "--json", "--n", "2"], self.root)
        obj = json.loads(out)
        self.assertEqual([q["id"] for q in obj["questions"]], MINI_GOLDEN[:2])
        self.assertEqual(obj["stage"], 0)
        for key in ("id", "ask", "why", "fills", "score", "stage"):
            self.assertIn(key, obj["questions"][0])
        code, out, err = _support.run_cli(["next", "--text", "--stage", "4", "--n", "1"], self.root)
        self.assertIn("why: Processes connect the people to the data.", out)
        code, out, err = _support.run_cli(["answer", "q.frame.scope", "Only", "the", "garden."], self.root)
        self.assertEqual(code, 0, err)
        self.assertIn("to q.frame.scope (answered) stored as src-", out)
        self.assertEqual(self.log()[-1]["q"], "q.frame.scope")
        code, out, err = _support.run_cli(["answer", "q.nope", "text"], self.root)
        self.assertEqual(code, 2)
        self.assertIn("unknown question q.nope", err)

    def test_mcp_next_call_forms(self):
        ctx = commands.Context(repo=self.repo, mcp=True, profile="full")
        text, is_error, _obj = commands.dispatch(commands.get("onto_next"), {"n": 1}, ctx)
        self.assertFalse(is_error, text)
        self.assertIn('onto_answer q=q.frame.deliverable text="<the user\'s words>" apply=true', text)
        self.assertIn("+", text)  # more questions are available and the line names the call


if __name__ == "__main__":
    unittest.main()
