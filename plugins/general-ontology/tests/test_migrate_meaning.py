"""Migrate folds a clashing local name into the core pack only when both mean the same (review round 7).

A 0.1.0 topic made with the real 0.1.0 kit (``test_migrate_020.old_kit``) holds a local question ``q.vocab.kinds``
that asks which file formats to import, answered in the interview, cited as ``Q:q.vocab.kinds`` by a record and by a
pending proposal, and named by another local question's ``when``. Kit 0.2.0 ships a core question of that id about
the topic's main kinds. Dropping the local one would attach the answer to the core question, which then is never
asked. So ``migrate`` refuses and writes nothing, and ``onto migrate --rename-question OLD=NEW`` moves the local
question and every reference to it to a new id, then migrates. A question of the same meaning still folds.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests import _support
from tests.test_migrate_020 import SPECIAL, old_kit
from ontokit import __version__, graph, interview, migrate, store, validate

GAP_OLD = "q.gap.archived_premise"
OLD = "q.vocab.kinds"
NEW = "q.local.formats"
FORMATS = {"ask": "Which file formats should the garden log import?", "dimension": "vocabulary",
           "fills": {"kinds": ["term"]}, "follow_ups": ["q.local.formats.detail"], "id": OLD, "options": None,
           "priority": 30, "quick": False, "repeatable": True, "stage": 3, "until": [], "when": [],
           "why": "Each format becomes a term."}
DETAIL = {"ask": "Which of those formats come from the weather station?", "dimension": "vocabulary",
          "fills": {"kinds": ["term"]}, "follow_ups": [], "id": "q.local.formats.detail", "options": None,
          "priority": 20, "quick": False, "repeatable": False, "stage": 3, "until": [],
          "when": [{"answered": OLD}], "why": "The station's formats change on their own schedule."}


def read_jsonl(path):
    rows, bad = store.read_jsonl(path)
    assert not bad, bad
    return rows


def clear():
    graph.clear_cache()
    store.clear_cache()
    validate.clear_cache()


def core_question(qid):
    with open(os.path.join(_support.PLUGIN_DIR, "ontokit", "packs", "core.questions.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            if row["id"] == qid:
                return row
    raise AssertionError(qid)


class _OldTopic(_support.TempCase):
    """A topic the 0.1.0 kit wrote, in a folder whose path has a space and a curly apostrophe."""

    def setUp(self):
        super().setUp()
        place = os.path.join(self.tmp, SPECIAL)
        self.onto010 = old_kit(os.path.join(place, "kit 0.1.0"))
        self.root = os.path.join(place, "garden")
        _support.git_init(self.root)
        self.old("init", "--name", "garden", "--ns", "garden", "--title", "Garden")

    def old(self, *args):
        env = dict(os.environ, **_support.GIT_ENV)
        proc = subprocess.run([sys.executable, self.onto010] + list(args), cwd=self.root, env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        self.assertEqual(proc.returncode, 0, (args, proc.stdout, proc.stderr))
        return proc.stdout.decode("utf-8")

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def write_questions(self, rows):
        store.write_bytes(self.path("packs", "local.questions.jsonl"), store.jsonl_bytes(rows, "id"))

    def ops_file(self, name, ops):
        path = self.path(".onto", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(ops, fh)
        return "@" + path

    def snapshot(self):
        return _support.snapshot(self.root, skip=[".git"])


class _FormatsTopic(_OldTopic):
    """The local ``q.vocab.kinds`` about file formats, answered twice (one answer applied, one pending)."""

    def setUp(self):
        super().setUp()
        self.write_questions([FORMATS, DETAIL])
        self.assertNotIn("P20", self.old("validate"))  # the 0.1.0 kit has no core q.vocab.kinds
        text = "Seed sheets come as CSV files."
        ops = [{"op": "add_node", "ref": "$t", "basis": "stated", "node": {"kind": "term", "name": "CSV"},
                "prov": [{"quote": "CSV files"}]},
               {"op": "add_edge", "basis": "stated", "edge": {"src": "$t", "rel": "part_of", "dst": "topic:garden"},
                "prov": [{"quote": "CSV files"}]}]
        self.old("answer", OLD, text, "--ops", self.ops_file("ops.json", ops), "--apply")
        # a second answer (the question is repeatable) leaves a pending proposal that cites the question too
        ops = [{"op": "add_node", "ref": "$t", "basis": "stated", "node": {"kind": "term", "name": "Plain text"},
                "prov": [{"quote": "plain text"}]}]
        self.old("answer", OLD, "Harvest notes are plain text.", "--ops", self.ops_file("ops2.json", ops))


class QuestionMeaningTest(_FormatsTopic):
    def test_a_different_meaning_is_refused_and_nothing_is_written(self):
        before = self.snapshot()
        for args in (["migrate", "--check"], ["migrate"]):
            code, out, err = _support.run_cli(args, self.root)
            self.assertEqual(code, 1, out + err)
            text = " ".join((out + err).split())
            self.assertIn("nothing was written", text)
            self.assertIn("the local question %s" % OLD, text)
            self.assertIn("Which file formats should the garden log import?", text)
            self.assertIn("onto migrate --rename-question %s=q.local.vocab.kinds" % OLD, text)
            self.assertNotIn("update_node", text)  # the record advice does not apply to a question
        self.assertEqual(self.snapshot(), before)
        with open(self.path("ontology.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["kit"], "0.1.0")

    def test_the_rename_moves_every_reference_and_migrate_passes(self):
        nodes_before = read_jsonl(self.path("graph", "nodes.jsonl"))
        log_before = read_jsonl(self.path("interview", "log.jsonl"))
        self.assertEqual(sum(1 for r in log_before if r["q"] == OLD), 2)
        before = self.snapshot()
        code, out, err = _support.run_cli(["migrate", "--check", "--rename-question", "%s=%s" % (OLD, NEW)], self.root)
        self.assertEqual(code, 0, out + err)
        self.assertIn("rename the local question %s to %s" % (OLD, NEW), out)
        self.assertEqual(self.snapshot(), before)  # --check writes nothing
        code, out, err = _support.run_cli(["migrate", "--rename-question", "%s=%s" % (OLD, NEW)], self.root)
        self.assertEqual(code, 0, out + err)
        self.assertIn("rename the local question %s to %s" % (OLD, NEW), out)
        self.assertIn("stamp kit %s (was 0.1.0)" % __version__, out)
        clear()
        # the question and its follow-up's predicate
        questions = {q["id"]: q for q in read_jsonl(self.path("packs", "local.questions.jsonl"))}
        self.assertEqual(sorted(questions), [NEW, "q.local.formats.detail"])
        self.assertEqual(questions[NEW]["ask"], FORMATS["ask"])
        self.assertEqual(questions["q.local.formats.detail"]["when"], [{"answered": NEW}])
        # the interview log: the same lines, ids and order, under the new id
        log = read_jsonl(self.path("interview", "log.jsonl"))
        self.assertEqual([r["id"] for r in log], [r["id"] for r in log_before])
        self.assertEqual([r["q"] for r in log], [NEW if r["q"] == OLD else r["q"] for r in log_before])
        # provenance on the records, in the proposals and the answer sources' titles
        nodes = read_jsonl(self.path("graph", "nodes.jsonl"))
        self.assertEqual([n["id"] for n in nodes], [n["id"] for n in nodes_before])
        locs = [p.get("loc") for n in nodes for p in n.get("prov") or []]
        self.assertIn("Q:" + NEW, locs)
        self.assertNotIn("Q:" + OLD, locs)
        for folder in ("pending", "done"):
            for name in sorted(os.listdir(self.path("proposals", folder))):
                with open(self.path("proposals", folder, name), encoding="utf-8") as fh:
                    text = fh.read()
                self.assertNotIn(OLD, text, name)
        pending = os.listdir(self.path("proposals", "pending"))
        self.assertEqual(len(pending), 1)
        with open(self.path("proposals", "pending", pending[0]), encoding="utf-8") as fh:
            self.assertIn("Q:" + NEW, fh.read())
        titles = [s.get("title") for s in read_jsonl(self.path("sources", "index.jsonl"))
                  if s.get("kind") == "interview"]
        self.assertTrue(any(t.startswith("Answer to %s" % NEW) for t in titles), titles)
        self.assertFalse(any(OLD in t for t in titles), titles)
        # the old answer stays attached to the renamed question; the core question is open
        onto = graph.Ontology.load(store.Repo.open(self.root))
        state = interview._State(onto)
        self.assertEqual(state.log.status(NEW), "answered")
        self.assertIsNone(state.log.status(OLD))
        bank = {q["id"]: q for q in onto.registry.questions()}
        self.assertEqual(bank[OLD]["ask"], core_question(OLD)["ask"])
        self.assertEqual(bank[NEW]["ask"], FORMATS["ask"])
        self.assertIn(NEW, interview.origin_questions(onto, "term:csv"))
        # the change log records the rename with the migration
        change = read_jsonl(self.path("ledger", "changes.jsonl"))[-1]
        self.assertEqual(change["type"], "migrate")
        self.assertIn("rename the local question %s to %s" % (OLD, NEW), change["summary"])
        code, out, err = _support.run_cli(["validate"], self.root)
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("P20", out)
        code, out, _err = _support.run_cli(["migrate", "--check"], self.root)
        self.assertEqual((code, out.strip().splitlines()[-1]), (0, "up to date: nothing to migrate"))

    def test_a_rename_that_cannot_hold_is_refused_and_nothing_is_written(self):
        before = self.snapshot()
        cases = (("%s=q.frame.you" % OLD, "already a question"),  # a core id
                 ("%s=q.local.formats.detail" % OLD, "already a question"),  # a local id
                 ("q.frame.you=q.local.you", "not a local question"),
                 ("%s=Local formats" % OLD, "not a question id"),
                 ("%s" % OLD, "OLD=NEW"),
                 ("%s=%s" % (OLD, OLD), "NEW must be a new id"))
        for spec, needle in cases:
            code, out, err = _support.run_cli(["migrate", "--rename-question", spec], self.root)
            self.assertNotEqual(code, 0, (spec, out, err))
            self.assertIn(needle, out + err, spec)
        self.assertEqual(self.snapshot(), before)

    def test_a_new_id_the_log_already_holds_is_refused(self):
        with open(self.path("interview", "log.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": "2026-01-02T03:04:05Z", "id": "ans-20260102-aaaaaa", "node": None,
                                 "proposal": None, "q": NEW, "src": None, "status": "skipped"}) + "\n")
        before = self.snapshot()
        code, out, err = _support.run_cli(["migrate", "--rename-question", "%s=%s" % (OLD, NEW)], self.root)
        self.assertNotEqual(code, 0, out + err)
        self.assertIn("the interview log already holds %s" % NEW, out + err)
        self.assertEqual(self.snapshot(), before)

    def test_a_write_that_fails_half_way_puts_every_file_back(self):
        before = self.snapshot()
        real = store.write_bytes
        written = []

        def flaky(path, data):
            rel = os.path.relpath(path, self.root).replace(os.sep, "/")
            if not rel.startswith(".onto/") and not rel.startswith(".."):  # a topic file, not the intent or a backup
                written.append(rel)
                if len(written) == 2:
                    raise OSError("disk full")
            return real(path, data)

        with mock.patch.object(store, "write_bytes", flaky):
            code, out, err = _support.run_cli(["migrate", "--rename-question", "%s=%s" % (OLD, NEW)], self.root)
        self.assertNotEqual(code, 0, out + err)
        self.assertIn("disk full", out + err)
        self.assertGreater(len(written), 2, written)  # the first file was written, then put back
        clear()
        self.assertFalse(os.path.exists(self.path(".onto", "intent.json")))
        skip = ".onto/lock"  # the lock file holds the pid of the last writer
        self.assertEqual({k: v for k, v in self.snapshot().items() if k != skip},
                         {k: v for k, v in before.items() if k != skip})
        code, out, err = _support.run_cli(["migrate", "--rename-question", "%s=%s" % (OLD, NEW)], self.root)
        self.assertEqual(code, 0, out + err)  # and the same call goes through once the disk is fine


class RenameUnitTest(unittest.TestCase):
    def test_the_question_id_rule_is_the_interview_rule(self):
        self.assertEqual(migrate.QID_RE.pattern, interview.QID_RE.pattern)

    def test_the_suggested_id_is_free_and_stable(self):
        self.assertEqual(migrate.suggest_id(OLD, set()), "q.local.vocab.kinds")
        self.assertEqual(migrate.suggest_id(OLD, {"q.local.vocab.kinds"}), "q.local.vocab.kinds-2")

    def test_meaning_is_blind_to_spacing_case_and_order_only(self):
        core = core_question(OLD)
        self.assertEqual(migrate.question_differs(dict(core, ask=" " + core["ask"].upper(), why="x", priority=1),
                                                  core), [])
        self.assertEqual(migrate.question_differs(dict(core, ask=core["ask"] + " Or two."), core), ["the ask"])
        self.assertEqual(migrate.question_differs(dict(core, options=[]), dict(core, options=None)), [])
        self.assertEqual(migrate.question_differs(dict(core, for_gap="orphan"), core), ["the gap it asks about"])
        fills = {"kinds": ["b", "a"]}
        self.assertEqual(migrate.question_differs(dict(core, fills=fills), dict(core, fills={"kinds": ["a", "b"]})),
                         [])

    def test_a_gap_template_moves_its_gap_questions_too(self):
        mapper = migrate._Mapper([("q.gap.orphan", "q.local.orphan")], {"q.gap.orphan"}, {"q.gap.orphan.kept"})
        self.assertEqual(mapper.qid("q.gap.orphan"), "q.local.orphan")
        self.assertEqual(mapper.qid("q.gap.orphan.owner"), "q.local.orphan.owner")
        self.assertIsNone(mapper.qid("q.gap.orphan.kept"))  # a question of its own
        self.assertIsNone(mapper.qid("q.gap.orphaned"))
        self.assertEqual(mapper.loc("Q:q.gap.orphan.owner@term:mulch"), "Q:q.local.orphan.owner@term:mulch")
        self.assertIsNone(mapper.loc("L1-L2"))
        plain = migrate._Mapper([(OLD, NEW)], set(), set())
        self.assertIsNone(plain.qid(OLD + ".detail"))  # only a gap template has gap questions
        # a gap question of a longer template id belongs to that template, not to the renamed one
        longer = migrate._Mapper([("q.gap.orphan", "q.local.orphan")], {"q.gap.orphan"}, {"q.gap.orphan.bed"},
                                 {"q.gap.orphan.bed"})
        self.assertIsNone(longer.qid("q.gap.orphan.bed.owner"))
        self.assertEqual(longer.qid("q.gap.orphan.owner"), "q.local.orphan.owner")

    def test_a_gap_template_id_is_suggested_below_the_next_template(self):
        self.assertEqual(migrate.suggest_id(GAP_OLD, set(), "q.gap.missing_field"), "q.gap.local.archived_premise")
        self.assertEqual(migrate.suggest_id(GAP_OLD, {"q.gap.local.archived_premise"}, "q.gap.missing_field"),
                         "q.gap.local.archived_premise-2")
        self.assertEqual(migrate.suggest_id(GAP_OLD, set(), "q.gap.conflict"), "q.0.local.archived_premise")
        self.assertEqual(migrate.suggest_id(GAP_OLD, set(), "q.0"), "")

    def test_a_malformed_rename_is_refused(self):
        from ontokit.errors import UsageError

        for value in (["q.a"], ["q.a=q.b", "q.a=q.c"], ["q.a=q.c", "q.b=q.c"], ["q.a=q.b", "q.b=q.c"], ["x=q.b"]):
            with self.assertRaises(UsageError, msg=value):
                migrate.parse_renames(value)
        self.assertEqual(migrate.parse_renames([" q.a = q.b "]), [("q.a", "q.b")])


class EquivalentQuestionTest(_OldTopic):
    def test_a_question_that_means_the_same_folds(self):
        core = core_question(OLD)
        same = dict(core, ask="  " + core["ask"].upper().replace(" ", "  ") + " ", priority=10, why="Our words.")
        self.write_questions([same])
        self.old("answer", OLD, "Beds, crops and tools.")
        code, out, err = _support.run_cli(["migrate"], self.root)
        self.assertEqual(code, 0, out + err)
        self.assertIn("drop the local question %s: the core question %s takes its place" % (OLD, OLD), out)
        # the core priority orders the interview from now on, so the fold line says so
        self.assertIn("takes its place (its priority 10 gives way to the core priority %d, which changes where the "
                      "interview asks it)" % core["priority"], " ".join(out.split()))
        clear()
        self.assertEqual(read_jsonl(self.path("packs", "local.questions.jsonl")), [])
        onto = graph.Ontology.load(store.Repo.open(self.root))
        self.assertEqual(interview._State(onto).log.status(OLD), "answered")
        self.assertEqual(_support.run_cli(["validate"], self.root)[0], 0)

    def test_other_fills_or_options_are_a_different_meaning(self):
        core = core_question(OLD)
        for change in ({"fills": {"kinds": ["term"]}}, {"options": ["beds", "crops"]}):
            self.write_questions([dict(core, **change)])
            clear()
            before = self.snapshot()
            code, out, err = _support.run_cli(["migrate", "--check"], self.root)
            self.assertEqual(code, 1, (change, out, err))
            self.assertIn("--rename-question", out + err)
            self.assertEqual(self.snapshot(), before)


class GapTemplateTest(_OldTopic):
    """A 0.1.0 local gap template under the id 0.2.0 gives the core archived_premise template, with an answered
    missing_field gap question ``q.gap.archived_premise.owner@bed:north-bed`` that left a pending proposal."""

    GAP = "q.gap.archived_premise.owner"

    def setUp(self):
        super().setUp()
        self.old("answer", "q.deepen.more", "We keep beds; each has an owner.", "--ops", self.ops_file("k.json", [
            {"op": "add_kind", "name": "bed", "kind": {"label": "Bed", "plural": "beds", "expected": ["owner"],
                                                       "fields": {"owner": {"type": "string"}}}}]),
                 "--apply", "--confirm")
        self.write_questions([{"ask": "Who tends {name}, the {field} of it?", "for_gap": "missing_field",
                               "id": GAP_OLD, "priority": 35, "why": "Each bed has a keeper."}])
        self.old("answer", "q.deepen.more", "We have the north bed.", "--ops", self.ops_file("n.json", [
            {"op": "add_node", "ref": "$b", "basis": "stated", "node": {"kind": "bed", "name": "North bed"},
             "prov": [{"quote": "the north bed", "by": "user"}]},
            {"op": "add_edge", "basis": "stated", "edge": {"src": "$b", "rel": "part_of", "dst": "topic:garden"},
             "prov": [{"quote": "the north bed", "by": "user"}]}]), "--apply", "--confirm")
        self.assertIn(self.GAP + "@bed:north-bed", self.old("next", "--n", "30"))
        self.old("answer", self.GAP + "@bed:north-bed", "Sam tends the north bed.", "--ops", self.ops_file("a.json", [
            {"op": "update_node", "id": "bed:north-bed", "set": {"attrs.owner": "Sam"},
             "reason": "The user named who tends the north bed.",
             "prov": [{"quote": "Sam tends the north bed.", "by": "user"}]}]))

    def test_an_id_that_sorts_after_the_core_template_is_refused(self):
        code, out, err = _support.run_cli(["migrate"], self.root)
        self.assertEqual(code, 1, out + err)
        text = " ".join((out + err).split())
        self.assertIn("--rename-question %s=q.gap.local.archived_premise" % GAP_OLD, text)
        before = self.snapshot()
        code, out, err = _support.run_cli(["migrate", "--rename-question", GAP_OLD + "=q.local.gap.archived_premise"],
                                          self.root)
        self.assertEqual(code, 2, out + err)
        text = " ".join((out + err).split())
        self.assertIn("sorts after the template q.gap.missing_field", text)
        self.assertIn("for example q.gap.local.archived_premise", text)
        self.assertEqual(self.snapshot(), before)

    def test_the_rename_moves_the_gap_questions_and_the_gap_stays_answered(self):
        new = "q.gap.local.archived_premise"
        code, out, err = _support.run_cli(["migrate", "--rename-question", "%s=%s" % (GAP_OLD, new)], self.root)
        self.assertEqual(code, 0, out + err)
        self.assertIn("rename the local question %s to %s (1 interview line, 1 proposal, 1 source)" % (GAP_OLD, new),
                      out)
        clear()
        moved = new + ".owner"
        self.assertEqual(read_jsonl(self.path("interview", "log.jsonl"))[-1]["q"], moved)
        titles = [s["title"] for s in read_jsonl(self.path("sources", "index.jsonl")) if s.get("kind") == "interview"]
        self.assertIn("Answer to %s on bed:north-bed" % moved, titles)
        self.assertFalse(any(self.GAP in t for t in titles), titles)
        pending = sorted(os.listdir(self.path("proposals", "pending")))
        self.assertEqual(len(pending), 1)
        with open(self.path("proposals", "pending", pending[0]), encoding="utf-8") as fh:
            prop = json.load(fh)
        self.assertEqual(prop["summary"], "answer to %s@bed:north-bed: 1 op" % moved)
        self.assertEqual(prop["ops"][0]["prov"][0]["loc"], "Q:" + moved)
        onto = graph.Ontology.load(store.Repo.open(self.root))
        self.assertEqual(interview._State(onto).templates()["missing_field"]["id"], new)
        code, out, err = _support.run_cli(["next", "--n", "30"], self.root)
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("owner@bed:north-bed", out)  # the answered gap is not asked again
        self.assertEqual(_support.run_cli(["validate"], self.root)[0], 0)


class KindMeaningTest(_OldTopic):
    """The kind and relation folds: a record or edge in use keeps its id, so the fold also needs the same meaning
    where the declaration states one (a kind's label and dimension, a relation's direction)."""

    def add(self, ops, text):
        self.old("answer", "q.deepen.more", text, "--ops", self.ops_file("ops.json", ops), "--apply", "--confirm")

    def test_a_kind_in_use_with_another_dimension_is_refused(self):
        self.add([{"op": "add_kind", "name": "premise",
                   "kind": {"label": "Premise", "plural": "premises", "dimension": "vocabulary"}},
                  {"op": "add_node", "ref": "$p", "basis": "stated", "node": {"kind": "premise", "name": "North lot"},
                   "prov": [{"quote": "the north lot", "by": "user"}]},
                  {"op": "add_edge", "basis": "stated", "edge": {"src": "$p", "rel": "part_of", "dst": "topic:garden"},
                   "prov": [{"quote": "the north lot", "by": "user"}]}], "The garden sits on the north lot.")
        before = self.snapshot()
        code, out, err = _support.run_cli(["migrate", "--check"], self.root)
        self.assertEqual(code, 1, out + err)
        text = " ".join((out + err).split())
        self.assertIn("premise:north-lot", text)
        self.assertIn("dimension vocabulary", text)
        self.assertIn("archive the record", text)
        self.assertEqual(self.snapshot(), before)

    def test_a_kind_with_no_active_record_still_folds(self):
        self.add([{"op": "add_kind", "name": "premise",
                   "kind": {"label": "Grounds", "plural": "grounds", "dimension": "vocabulary"}}],
                 "We call the grounds premises.")
        code, out, err = _support.run_cli(["migrate"], self.root)
        self.assertEqual(code, 0, out + err)
        self.assertIn("fold the local kind premise into the core kind premise (0 records keep their ids)", out)

    def test_a_relation_in_use_with_another_direction_is_refused(self):
        # the edge fits the core rests_on (anything to a premise) and the local premise kind means the core one, so
        # only the direction rule tells the two relations apart
        self.add([{"op": "add_kind", "name": "premise",
                   "kind": {"label": "Premise", "plural": "premises", "dimension": "constraints"}},
                  {"op": "add_kind", "name": "bed", "kind": {"label": "Bed", "plural": "beds"}},
                  {"op": "add_relation", "name": "rests_on",
                   "relation": {"from": "*", "to": ["premise"], "symmetric": True}},
                  {"op": "add_node", "ref": "$a", "basis": "stated", "node": {"kind": "bed", "name": "South bed"},
                   "prov": [{"quote": "south bed", "by": "user"}]},
                  {"op": "add_node", "ref": "$b", "basis": "stated",
                   "node": {"kind": "premise", "name": "Rain is enough"},
                   "prov": [{"quote": "rain is enough", "by": "user"}]},
                  {"op": "add_edge", "basis": "stated", "edge": {"src": "$a", "rel": "rests_on", "dst": "$b"},
                   "prov": [{"quote": "The south bed needs no hose: rain is enough.", "by": "user"}]}],
                 "The south bed needs no hose: rain is enough.")
        before = self.snapshot()
        code, out, err = _support.run_cli(["migrate", "--check"], self.root)
        self.assertEqual(code, 1, out + err)
        text = " ".join((out + err).split())
        self.assertIn("the local relation rests_on is symmetric and the core relation rests_on is not", text)
        self.assertNotIn("P09", text)  # the edge fits the core relation: only its direction differs
        self.assertNotIn("the local kind premise", text)  # the kind means the core one and folds
        self.assertEqual(self.snapshot(), before)


def another_writer_first(write):
    """A ``store.write_lock`` stand-in: the first time migrate asks for the lock, another command takes it first and
    runs ``write`` (then lets go), as when an answer is recorded after migrate planned and before it locked."""
    real = store.write_lock
    done = []

    @contextlib.contextmanager
    def lock(repo, *args, **kwargs):
        if not done:
            done.append(True)
            with real(repo):
                write()
        with real(repo, *args, **kwargs):
            yield

    return mock.patch.object(store, "write_lock", lock), done


class ConcurrentWriteTest(_FormatsTopic):
    """Review finding: migrate planned its rewrites before it took the write lock, so a write another command made
    in between was overwritten by the stale planned bytes. The plan that is written is now made under the lock."""

    LINE = {"at": "2026-09-29T12:00:00Z", "id": "ans-20260929-bbbbbb", "node": None, "proposal": None, "q": OLD,
            "src": None, "status": "skipped"}

    def test_an_answer_recorded_while_migrate_plans_a_rename_is_kept_and_moved(self):
        log = self.path("interview", "log.jsonl")
        patch, done = another_writer_first(lambda: store.append_jsonl(log, self.LINE))
        with patch:
            code, out, err = _support.run_cli(["migrate", "--rename-question", "%s=%s" % (OLD, NEW)], self.root)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(done, [True])
        clear()
        rows = read_jsonl(log)
        mine = [r for r in rows if r["id"] == self.LINE["id"]]
        self.assertEqual(len(mine), 1, rows)
        self.assertEqual(mine[0]["q"], NEW)  # the newer answer survives and moves with its question
        self.assertFalse(any(r["q"] == OLD for r in rows), rows)
        self.assertIn("3 interview lines", out)
        code, out, err = _support.run_cli(["validate"], self.root)
        self.assertEqual(code, 0, out + err)

    def test_a_question_added_while_migrate_plans_a_fold_is_kept(self):
        core = core_question(OLD)
        self.write_questions([dict(core, why="Our words.")])
        clear()
        added = {"ask": "Which beds get the morning sun?", "dimension": "vocabulary", "fills": {"kinds": ["term"]},
                 "id": "q.local.sun", "why": "Sun decides what grows."}
        bank = self.path("packs", "local.questions.jsonl")
        patch, done = another_writer_first(lambda: store.append_jsonl(bank, added))
        with patch:
            code, out, err = _support.run_cli(["migrate"], self.root)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(done, [True])
        self.assertIn("drop the local question %s" % OLD, out)
        clear()
        self.assertEqual([q["id"] for q in read_jsonl(bank)], ["q.local.sun"])

    def test_a_check_or_a_refusal_takes_no_lock(self):
        patch, done = another_writer_first(lambda: None)
        with patch:
            self.assertEqual(_support.run_cli(["migrate", "--check", "--rename-question", "%s=%s" % (OLD, NEW)],
                                              self.root)[0], 0)
            self.assertEqual(_support.run_cli(["migrate"], self.root)[0], 1)
        self.assertEqual(done, [])


class PrefixTemplateTest(_support.TempCase):
    """Review finding: two renamed gap templates whose ids share a prefix (``q.gap.zz`` and ``q.gap.zz.bed``) sent
    the longer one's gap questions through the shorter one."""

    PAIRS = [("q.gap.zz", "q.local.zz"), ("q.gap.zz.bed", "q.local.bed")]

    def test_the_mapper_picks_the_longest_renamed_template(self):
        for pairs in (self.PAIRS, list(reversed(self.PAIRS))):
            mapper = migrate._Mapper(pairs, {"q.gap.zz", "q.gap.zz.bed"}, set(), {"q.gap.zz", "q.gap.zz.bed"})
            self.assertEqual(mapper.qid("q.gap.zz.bed.owner"), "q.local.bed.owner")
            self.assertEqual(mapper.hit, {"q.gap.zz.bed"})
            mapper.tally("line")
            self.assertEqual(mapper.qid("q.gap.zz.owner"), "q.local.zz.owner")
            self.assertEqual(mapper.qid("q.gap.zz.bed"), "q.local.bed")
            self.assertEqual(mapper.qid("q.gap.zz"), "q.local.zz")
            self.assertEqual(mapper.loc("Q:q.gap.zz.bed.owner@bed:north-bed"), "Q:q.local.bed.owner@bed:north-bed")
        # a longer template that is not renamed still keeps its gap questions
        kept = migrate._Mapper(self.PAIRS[:1], {"q.gap.zz"}, {"q.gap.zz.bed"}, {"q.gap.zz", "q.gap.zz.bed"})
        self.assertIsNone(kept.qid("q.gap.zz.bed.owner"))

    def test_two_prefix_related_renames_in_one_call(self):
        root = _support.bare_topic(os.path.join(self.tmp, SPECIAL))
        repo = store.Repo.open(root)
        rows = [{"ask": "What does {name} miss?", "for_gap": "thin", "id": "q.gap.zz", "why": "Thin records."},
                {"ask": "Who owns {name}?", "for_gap": "orphan", "id": "q.gap.zz.bed", "why": "Lone beds."}]
        store.write_bytes(repo.path(migrate.LOCAL_QUESTIONS), store.jsonl_bytes(rows, "id"))
        log = [{"at": "2026-01-02T03:04:05Z", "id": "ans-20260102-aaaaa%d" % n, "node": None, "proposal": None,
                "q": q, "src": None, "status": "skipped"}
               for n, q in enumerate(["q.gap.zz.bed.owner", "q.gap.zz.owner", "q.gap.zz.bed.owner"])]
        store.write_bytes(repo.path(migrate.INTERVIEW_LOG), store.jsonl_bytes(log, None))
        lines, files = migrate.rename_plan(repo, self.PAIRS)
        self.assertEqual(lines, ["rename the local question q.gap.zz to q.local.zz (1 interview line)",
                                 "rename the local question q.gap.zz.bed to q.local.bed (2 interview lines)"])
        moved = [json.loads(x)["q"] for x in files[migrate.INTERVIEW_LOG].decode("utf-8").splitlines() if x]
        self.assertEqual(moved, ["q.local.bed.owner", "q.local.zz.owner", "q.local.bed.owner"])


class QuestionRulesTest(_OldTopic):
    """Review finding: a local question folded into the core one when only its interview rules differed (repeat,
    quick start, stage), so the interview asked it another way after the migration."""

    def test_each_interview_rule_is_part_of_the_meaning(self):
        core = core_question(OLD)
        cases = ((dict(core, repeatable=True), "the repeat rule"), (dict(core, quick=True), "the quick start flag"),
                 (dict(core, stage=5), "the stage"), (dict(core, dimension="frame"), "the dimension"),
                 (dict(core, when=[{"answered": "q.frame.you"}]), "the when rule"),
                 (dict(core, until=[{"answered": "q.frame.you"}]), "the until rule"),
                 (dict(core, follow_ups=["q.local.more"]), "the follow-ups"))
        for local, part in cases:
            self.assertEqual(migrate.question_differs(local, core), [part], part)
        # a rule left out reads as its default (not repeatable, not quick, no follow-ups, when or until); the order
        # of when and until predicates says nothing
        bare = {k: v for k, v in core.items() if k not in ("repeatable", "quick", "when", "until", "follow_ups")}
        self.assertEqual(migrate.question_differs(bare, core), [])
        both = [{"answered": "q.frame.you"}, {"not_answered": "q.frame.goal"}]
        self.assertEqual(migrate.question_differs(dict(core, when=both), dict(core, when=both[::-1])), [])
        # a stage the core question takes from its dimension is the same stage
        self.assertEqual(migrate.question_differs(dict(core, stage=3), {k: v for k, v in core.items() if k != "stage"},
                                                  {"vocabulary": 3}), [])

    def test_a_stage_or_dimension_left_out_is_the_one_the_interview_uses(self):
        """Review finding: a local question with no ``stage`` or ``dimension`` folded, though the interview asks it
        in the deepening stage and in no dimension, while one that wrote that stage down was refused."""
        self.assertEqual(migrate.DEEPEN, interview.DEEPEN)
        core = core_question(OLD)
        stages = {"vocabulary": core["stage"]}
        bare = {k: v for k, v in core.items() if k not in ("stage", "dimension")}
        self.assertEqual(migrate.question_differs(bare, core, stages), ["the stage", "the dimension"])
        self.assertEqual(migrate.question_differs(dict(bare, stage=interview.DEEPEN), core, stages),
                         ["the stage", "the dimension"])  # the same interview behaviour gets the same answer
        self.assertEqual(migrate.question_differs(dict(bare, stage=core["stage"]), core, stages), ["the dimension"])
        # with the core dimension and no stage, the question takes its dimension's stage, which is the core stage
        self.assertEqual(migrate.question_differs(dict(bare, dimension=core["dimension"]), core, stages), [])
        self.assertEqual(migrate.question_differs(dict(bare, dimension=core["dimension"]), core, {}), ["the stage"])
        # the dimension's stage comes from the topic: a local dimension of that name counts when the core has none
        local_dim = dict(bare, dimension="formats")
        self.assertEqual(migrate.question_differs(local_dim, dict(core, dimension="formats"), {"formats": 3}), [])
        for rows in ([bare], [dict(bare, stage=interview.DEEPEN)]):
            self.write_questions(rows)
            clear()
            before = self.snapshot()
            code, out, err = _support.run_cli(["migrate", "--check"], self.root)
            self.assertEqual(code, 1, (rows, out, err))
            text = " ".join((out + err).split())
            self.assertIn("the stage and the dimension differ", text)
            self.assertIn("--rename-question %s=q.local.vocab.kinds" % OLD, text)
            self.assertEqual(self.snapshot(), before)

    def test_the_same_priority_folds_with_no_note(self):
        self.write_questions([dict(core_question(OLD), why="Our words.")])
        clear()
        code, out, err = _support.run_cli(["migrate", "--check"], self.root)
        self.assertEqual(code, 0, out + err)
        self.assertIn("drop the local question %s: the core question %s takes its place" % (OLD, OLD), out)
        self.assertNotIn("gives way", out)

    def test_a_question_with_other_rules_is_refused(self):
        core = core_question(OLD)
        for change in ({"repeatable": True}, {"quick": True}, {"stage": 5}):
            self.write_questions([dict(core, **change)])
            clear()
            before = self.snapshot()
            for args in (["migrate", "--check"], ["migrate"]):
                code, out, err = _support.run_cli(args, self.root)
                self.assertEqual(code, 1, (change, out, err))
                self.assertIn("--rename-question %s=q.local.vocab.kinds" % OLD, " ".join((out + err).split()))
            self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
