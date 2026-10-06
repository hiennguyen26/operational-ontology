"""Regression tests for the fourth core review round: crash recovery after a merge, erased sources that stayed
citable, stale proposals that could not be proposed again, the trust of mixed-source ops, out-of-range numbers in
data lines, finding a name before an erase, personal data in the init title, conflicts settled by unrelated
decisions or by answers that left no trace, node gaps that never settled, and the answer confirm text."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from tests import _support
from tests.test_graph import clear
from ontokit import commands, graph, ledger, mutate, needs, pipeline, sources, store, util, validate
from ontokit.errors import Conflict, Refused

NOTE = "src-000a61a61d03"  # the handbook excerpt of the mini fixture (12 lines, untrusted note)
MULCH = {"src": NOTE, "loc": "L11-L11", "quote": "Mulch keeps the soil moist between waterings.", "by": "agent"}


def read(root, rel):
    with open(os.path.join(root, *rel.split("/")), "rb") as fh:
        return fh.read()


def write(root, rel, data):
    with open(os.path.join(root, *rel.split("/")), "wb") as fh:
        fh.write(data if isinstance(data, bytes) else data.encode("utf-8"))


def lines(root, rel):
    return [line for line in read(root, rel).split(b"\n") if line.strip()]


class Base(_support.TempCase):
    def setUp(self):
        super().setUp()
        clear()
        self.root = _support.make_topic(self.tmp)
        self.repo = store.Repo.open(self.root)

    def onto(self):
        clear()
        return graph.Ontology.load(self.repo)

    def propose(self, ops, source=NOTE, **extra):
        draft = {"source": source, "summary": "test", "ops": ops}
        draft.update(extra)
        return pipeline.prepare(self.repo, draft)

    def accept(self, prop, verdict="accept"):
        pipeline.review(self.repo, prop["id"], {str(op["n"]): verdict for op in prop["ops"]}, by="user")
        return pipeline.commit(self.repo, prop["id"])

    def ingest(self, text, title="Note", kind="note"):
        from ontokit import ingest

        out = ingest.ingest_text(self.repo, text, title=title, kind=kind)
        clear()
        return out["source"]["id"] if "source" in out else out["items"][0]["source"]["id"]

    def problems(self, fix=False):
        clear()
        return [p.text() for p in validate.validate(self.repo, fix=fix).problems]


# recover-truncates-merged-lines ---------------------------------------------------------------------------------
class RecoverKeepsMergedLinesTest(Base):
    LOG = ledger.CHANGES

    def foreign(self, chg="chg-20261001-abcdef"):
        return util.canonical_line({"id": chg, "type": "ingest", "summary": "from another machine"}).encode() + b"\n"

    def test_a_line_merged_in_after_a_kill_before_the_append_survives_recovery(self):
        old = read(self.root, self.LOG)
        store.begin_write(self.repo, [], [(self.LOG, old)])
        store._ACTIVE.clear()  # killed before anything was appended
        write(self.root, self.LOG, old + self.foreign())  # a merge or a pull brings another line
        store.recover(self.repo)
        self.assertEqual(read(self.root, self.LOG), old + self.foreign())
        self.assertIsNone(store.pending_intent(self.repo))
        self.assertIsNone(store.recovery_note(self.repo))

    def test_the_interrupted_writes_own_line_is_still_taken_back(self):
        old = read(self.root, self.LOG)
        store.begin_write(self.repo, [], [(self.LOG, old)])
        store.append_jsonl(self.repo.path(self.LOG), {"id": "chg-20261001-111111", "type": "apply"})
        store._ACTIVE.clear()
        store.recover(self.repo)
        self.assertEqual(read(self.root, self.LOG), old)

    def test_a_planned_line_that_never_landed_takes_nothing_back(self):
        old = read(self.root, self.LOG)
        store.begin_write(self.repo, [], [(self.LOG, old)])
        intent = store._ACTIVE[os.path.realpath(self.root)]
        intent["appends"][0]["add"] = ['{"id":"chg-planned"}\n']  # recorded, then killed before the write
        store.write_bytes(self.repo.path(store.INTENT_REL), util.canonical_bytes(intent))
        store._ACTIVE.clear()
        write(self.root, self.LOG, old + self.foreign())
        store.recover(self.repo)
        self.assertEqual(read(self.root, self.LOG), old + self.foreign())
        self.assertIsNone(store.recovery_note(self.repo))

    def test_own_line_among_merged_lines_is_left_and_reported(self):
        old = read(self.root, self.LOG)
        store.begin_write(self.repo, [], [(self.LOG, old)])
        store.append_jsonl(self.repo.path(self.LOG), {"id": "chg-20261001-111111", "type": "apply"})
        store._ACTIVE.clear()
        mixed = read(self.root, self.LOG) + self.foreign()
        write(self.root, self.LOG, mixed)
        store.recover(self.repo)
        self.assertEqual(read(self.root, self.LOG), mixed)  # nothing cut: a person decides
        note = store.recovery_note(self.repo)
        self.assertEqual([i["path"] for i in note["left"]], [self.LOG])
        clear()
        report = validate.validate(self.repo)
        self.assertIn("W08", [w.code for w in report.warnings])
        code, out, _err = _support.run_cli(["status"], repo=self.root)
        self.assertIn("recovery left ledger/changes.jsonl as it is", out)
        validate.validate(self.repo, fix=True)  # reports it once more, then clears the note
        self.assertIsNone(store.recovery_note(self.repo))

    def test_a_new_file_is_removed_only_when_it_holds_just_the_planned_line(self):
        rel = "metrics/history.jsonl"
        if os.path.exists(self.repo.path(rel)):
            os.unlink(self.repo.path(rel))
        store.begin_write(self.repo, [], [(rel, None)])
        store.append_jsonl(self.repo.path(rel), {"at": "2026-10-01T00:00:00Z", "kind": "apply"})
        store._ACTIVE.clear()
        store.recover(self.repo)
        self.assertFalse(os.path.exists(self.repo.path(rel)))  # it held only the interrupted write's line
        store.begin_write(self.repo, [], [(rel, None)])
        store._ACTIVE.clear()
        write(self.root, rel, b'{"at":"2026-10-01T00:00:00Z","merged":true}\n')  # a merge created it
        store.recover(self.repo)
        self.assertTrue(os.path.exists(self.repo.path(rel)))

    def test_status_names_a_pending_intent(self):
        old = read(self.root, self.LOG)
        store.begin_write(self.repo, [], [(self.LOG, old)])
        store._ACTIVE.clear()
        code, out, _err = _support.run_cli(["status"], repo=self.root)
        self.assertIn("a write stopped half way", out)
        self.assertIn("Next: onto validate --fix", out)
        self.assertEqual(store.interrupted_lines(self.repo)[0].split(" (")[0], "a write stopped half way")
        store.recover(self.repo)
        self.assertEqual(store.interrupted_lines(self.repo), [])


class RecoverAfterMergeKillTest(_support.TempCase):
    """The reviewer's flow: a hard kill inside apply, then a fast-forward merge of a branch that ingested a
    source, then ``validate --fix``: the merged change line and history point stay."""

    def test_merged_ingest_survives_the_roll_back(self):
        from ontokit import ingest

        clear()
        root = _support.make_topic(self.tmp)
        _support.git_init(root)
        repo = store.Repo.open(root)
        _support.commit_all(root, "base")
        _support.git(root, "checkout", "-q", "-b", "other")
        ingest.ingest_text(repo, "Cold frames keep seedlings warm in March.\n", title="Cold frames", kind="note")
        _support.commit_all(root, "other")
        _support.git(root, "checkout", "-q", "main")
        clear()
        prop = pipeline.prepare(repo, {"source": NOTE, "summary": "t", "ops": [
            {"op": "add_node", "node": {"kind": "term", "name": "Mulch layer", "summary": "Keeps soil moist."},
             "prov": [MULCH]}]})
        _support.commit_all(root, "proposal")
        script = (
            "import os, sys\n"
            "sys.path.insert(0, %r)\n"
            "from ontokit import pipeline, store\n"
            "real = store.write_bytes\n"
            "def kill(path, data):\n"
            "    if path.endswith('nodes.jsonl'):\n"
            "        os._exit(9)\n"
            "    return real(path, data)\n"
            "store.write_bytes = kill\n"
            "pipeline.commit(store.Repo.open(%r), %r, verdicts={'1': 'accept'}, by='user')\n"
            % (_support.PLUGIN_DIR, root, prop["id"]))
        env = dict(os.environ, ONTO_FIXED_NOW=_support.FIXED_NOW)
        proc = subprocess.run([sys.executable, "-c", script], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(proc.returncode, 9, proc.stderr.decode("utf-8", "replace"))
        self.assertIsNotNone(store.pending_intent(repo))
        _support.git(root, "merge", "-q", "--no-edit", "other")
        merged = {rel: read(root, rel) for rel in (ledger.CHANGES, "metrics/history.jsonl")}
        clear()
        report = validate.validate(repo, fix=True)
        self.assertEqual([p.text() for p in report.problems], [])
        for rel, data in merged.items():
            self.assertEqual(read(root, rel), data, rel)  # nothing cut
        self.assertEqual(_support.git(root, "status", "--porcelain", "--", "ledger", "metrics"), "")


# overflow-float-crashes-validate ---------------------------------------------------------------------------------
class OverflowNumberTest(Base):
    def test_1e400_and_minus_1e999_are_p01_not_a_crash(self):
        for literal in ("1e400", "-1e999"):
            with self.subTest(literal=literal):
                clear()
                root = _support.make_topic(self.tmp, ns="ovf%s" % ("a" if literal == "1e400" else "b"))
                repo = store.Repo.open(root)
                rows = lines(root, "graph/nodes.jsonl")
                first = json.loads(rows[0])
                rows[0] = rows[0].replace(b'"attrs":{', b'"attrs":{"zz":%s,' % literal.encode(), 1) \
                    if b'"attrs":{}' not in rows[0] else rows[0].replace(b'"attrs":{}', b'"attrs":{"zz":%s}'
                                                                         % literal.encode(), 1)
                write(root, "graph/nodes.jsonl", b"\n".join(rows) + b"\n")
                clear()
                report = validate.validate(repo)
                found = [p for p in report.problems if p.code == "P01" and p.file == "graph/nodes.jsonl"]
                self.assertTrue(found, [p.text() for p in report.problems])
                self.assertIn("out of range", found[0].message)
                clear()
                validate.validate(repo, fix=True)  # no crash either
                with self.assertRaises(ValueError):
                    util.loads_record('{"a":%s}' % literal)
                with self.assertRaises(ValueError):
                    util.loads_strict('{"a":%s}' % literal)
                self.assertTrue(first)


# erased-source-still-citable -------------------------------------------------------------------------------------
class ErasedSourceTest(Base):
    GATE = "Gate note: the side gate code is kept by the plot lead.\nThe gate is painted blue.\n"

    def gate_op(self, sid, name="Side gate code kept by plot lead", quote=True):
        prov = {"src": sid, "loc": "L1-L1", "by": "agent"}
        if quote:
            prov["quote"] = "the side gate code is kept by the plot lead"
        return {"op": "add_node", "node": {"kind": "term", "name": name,
                                           "summary": "The side gate code is kept by the plot lead."},
                "prov": [prov]}

    def erase(self, sid):
        from ontokit import ingest

        dec = ledger.decide(self.repo, "Should the gate note be forgotten?", ["yes=Yes", "no=No"], "yes",
                            scope=[sid])
        clear()
        out = ingest.erase(self.repo, sid, dec["id"])
        clear()
        return out

    def test_a_pending_proposal_from_an_erased_source_is_closed_and_scrubbed(self):
        sid = self.ingest(self.GATE, title="Gate note")
        prop = self.propose([self.gate_op(sid)], source=sid)
        out = self.erase(sid)
        self.assertIn(prop["id"], out["names_scrubbed_in"])
        self.assertFalse(os.path.exists(self.repo.path("proposals/pending/%s.json" % prop["id"])))
        done = read(self.root, "proposals/done/%s.json" % prop["id"]).decode("utf-8")
        self.assertNotIn("gate code", done)
        self.assertEqual(pipeline.load(self.repo, prop["id"])["status"], "superseded")
        with self.assertRaises(Refused):
            pipeline.commit(self.repo, prop["id"], verdicts={"1": "accept"}, by="user")
        self.assertNotIn("term:side-gate-code-kept-by-plot-lead", self.onto().nodes)
        self.assertEqual(self.problems(), [])

    def test_a_new_proposal_citing_an_erased_source_is_refused(self):
        sid = self.ingest(self.GATE, title="Gate note")
        self.erase(sid)
        for source in (NOTE, sid):
            with self.subTest(source=source):
                with self.assertRaises(Refused) as ctx:
                    self.propose([self.gate_op(sid, name="Blue side gate", quote=False)], source=source)
                self.assertIn("P11", [p["code"] for p in ctx.exception.extra["problems"]])

    def test_a_reviewed_proposal_from_another_source_citing_it_cannot_apply(self):
        sid = self.ingest(self.GATE, title="Gate note")
        prop = self.propose([self.gate_op(sid)], source=NOTE)
        pipeline.review(self.repo, prop["id"], {"1": "accept"}, by="user")
        self.erase(sid)
        self.assertEqual(pipeline.load(self.repo, prop["id"])["status"], "superseded")
        with self.assertRaises(Refused):
            pipeline.commit(self.repo, prop["id"])

    def test_a_rejected_proposal_loses_the_text_it_drafted(self):
        sid = self.ingest("The hose is kept in shed two; Priya Raman has the key.\n", title="Hose")
        prop = self.propose([{"op": "add_node", "node": {
            "kind": "term", "name": "Hose key", "summary": "The hose is kept in shed two; Priya Raman has the key."},
            "prov": [{"src": sid, "loc": "L1-L1", "quote": "Priya Raman has the key", "by": "agent"}]}], source=sid)
        pipeline.review(self.repo, prop["id"], {"1": "reject"}, by="user")
        out = self.erase(sid)
        self.assertIn(prop["id"], out["names_scrubbed_in"])
        self.assertNotIn("Priya", read(self.root, "proposals/done/%s.json" % prop["id"]).decode("utf-8"))

    def test_an_applied_op_keeps_its_text_and_validate_flags_a_later_citation(self):
        sid = self.ingest(self.GATE, title="Gate note")
        prop = self.propose([self.gate_op(sid)], source=NOTE)
        self.accept(prop)
        self.erase(sid)
        self.assertIn("gate code", read(self.root, "proposals/done/%s.json" % prop["id"]).decode("utf-8"))
        self.assertEqual(self.problems(), [])
        # a merge brings back a quote of the erased source: validate names it
        rows = [json.loads(line) for line in lines(self.root, "graph/nodes.jsonl")]
        for row in rows:
            for p in row.get("prov") or []:
                if p.get("src") == sid:
                    p["quote"] = "the side gate code is kept by the plot lead"
        write(self.root, "graph/nodes.jsonl", store.jsonl_bytes(rows, "id"))
        self.assertTrue(any("keeps a quote of an erased source" in p for p in self.problems()))

    def test_validate_flags_text_applied_from_a_proposal_after_its_source_was_erased(self):
        sid = self.ingest(self.GATE, title="Gate note")
        prop = self.propose([self.gate_op(sid)], source=sid)
        self.erase(sid)
        self.assertEqual(self.problems(), [])
        # what a merge of another machine's apply (or an older kit) leaves: the op applied after the erase
        mutate.apply_ops(self.repo, [{"n": 1, "op": "add_node", "node": {
            "id": "term:side-gate-code", "kind": "term", "name": "Side gate code", "summary": ""},
            "status": "proposed", "trust": "untrusted", "conf": 0.7,
            "prov": [{"src": sid, "loc": "L1-L1", "by": "agent"}]}],
            by="agent", change_type="apply", summary="late", proposal_id=prop["id"], source=sid)
        found = [p for p in self.problems() if "after it was erased" in p]
        self.assertTrue(found and "term:side-gate-code" in found[0], self.problems())


# stale-proposal-cannot-repropose ---------------------------------------------------------------------------------
class StaleReproposeTest(Base):
    REPORT = "deliverable:harvest-report"

    def update(self, summary):
        return [{"op": "update_node", "id": self.REPORT, "set": {"summary": summary},
                 "reason": "the notes say how the report is shared now",
                 "prov": [{"src": NOTE, "loc": "L12-L12", "quote": "The harvest report goes to member households "
                                                                   "each month.", "by": "agent"}]}]

    def test_a_stale_proposal_is_replaced_when_proposed_again(self):
        first = self.propose(self.update("Summary A from the notes."))
        second = self.propose(self.update("Summary B from the notes."))
        self.accept(second)
        with self.assertRaises(Conflict) as ctx:
            pipeline.commit(self.repo, first["id"], verdicts={"1": "accept"}, by="user")
        self.assertIn(first["id"], ctx.exception.message)
        self.assertIn("--all reject", ctx.exception.message)
        again = self.propose(self.update("Summary A from the notes."))
        self.assertNotEqual(again["id"], first["id"])
        self.assertNotEqual(again.get("note"), "already proposed")
        self.assertEqual(again["supersedes"], first["id"])
        self.assertEqual(pipeline.load(self.repo, first["id"])["status"], "superseded")
        self.accept(again)
        self.assertEqual(self.onto().node(self.REPORT)["summary"], "Summary A from the notes.")
        # an open copy that is not stale is still "already proposed"
        third = self.propose(self.update("Summary C from the notes."))
        self.assertEqual(self.propose(self.update("Summary C from the notes."))["note"], "already proposed")
        self.assertEqual(third["status"], "pending")

    def test_the_same_draft_can_be_proposed_and_rejected_many_times_a_day(self):
        op = [{"op": "add_node", "node": {"kind": "term", "name": "Mulch", "summary": "Keeps soil moist."},
               "prov": [MULCH]}]
        seen = set()
        for _round in range(6):
            prop = self.propose(op)
            self.assertNotIn(prop["id"], seen)
            seen.add(prop["id"])
            pipeline.review(self.repo, prop["id"], {"1": "reject"}, by="user")
        self.assertEqual(self.problems(), [])


# mixed-source-draft-trust ----------------------------------------------------------------------------------------
class MixedSourceTrustTest(Base):
    ANSWER = "src-e08998852112"  # the interview answers of the mini fixture
    GARDEN = {"src": ANSWER, "loc": "L2-L2", "quote": "garden", "by": "user"}

    def node_op(self, name, prov, basis="stated"):
        return {"op": "add_node", "node": {"kind": "term", "name": name, "summary": "Mulch keeps the soil moist."},
                "prov": prov, "basis": basis}

    def apply(self, ops, verdict, change_type="answer"):
        prop = self.propose(ops, source=self.ANSWER)
        pipeline.commit(self.repo, prop["id"], verdicts={str(n): verdict for n in range(1, len(ops) + 1)},
                        by="user", change_type=change_type)
        return self.onto()

    def test_a_draft_citing_an_answer_and_a_file_stays_untrusted(self):
        onto = self.apply([self.node_op("Mixed mulch", [self.GARDEN, MULCH]), self.node_op("Answer mulch", [
            self.GARDEN]), self.node_op("File mulch", [MULCH])], "draft")
        self.assertEqual(onto.node("term:mixed-mulch")["trust"], "untrusted")
        self.assertEqual(onto.node("term:answer-mulch")["trust"], "agent")
        self.assertEqual(onto.node("term:file-mulch")["trust"], "untrusted")

    def test_an_answer_accept_earns_user_trust_only_from_the_answer_alone(self):
        onto = self.apply([self.node_op("Mixed mulch", [self.GARDEN, MULCH]),
                           self.node_op("Answer mulch", [self.GARDEN])], "accept")
        self.assertEqual(onto.node("term:mixed-mulch")["trust"], "untrusted")  # nobody reviewed the file's text
        self.assertEqual(onto.node("term:answer-mulch")["trust"], "user")
        self.assertEqual(onto.node("term:mixed-mulch")["status"], "confirmed")

    def test_a_user_review_of_a_mixed_op_gives_reviewed(self):
        onto = self.apply([self.node_op("Mixed mulch", [self.GARDEN, MULCH])], "accept", change_type="apply")
        self.assertEqual(onto.node("term:mixed-mulch")["trust"], "reviewed")


# unrelated-decision-settles-conflict / conflict-answer-leaves-no-trace -------------------------------------------
class ConflictSettleTest(_support.TempCase):
    def setUp(self):
        from tests.test_graph import mk_edge, write_topic

        super().setUp()
        clear()
        same = mk_edge("pa/process:repair", "same_as", "pb/process:repair", symmetric=True)
        self.repo = write_topic(self.tmp, [_node("topic:t")], [same])
        self.pin({"cadence": "monthly"}, {"cadence": "weekly"})

    def pin(self, pa, pb):
        from tests.test_graph import add_import, make_export

        for ns, attrs in (("pa", pa), ("pb", pb)):
            add_import(self.repo, ns, make_export(ns, [_node("process:repair", attrs=attrs)]))
        clear()

    def conflicts(self):
        clear()
        onto = graph.Ontology.load(self.repo)
        return {g["field"]: g for g in needs.needs(onto, "pa/process:repair")["gaps"] if g["type"] == "conflict"}

    def decide(self, question, scope):
        clear()
        return ledger.decide(self.repo, question, ["a=Monthly", "b=Weekly"], "b", scope=scope)

    def test_a_decision_about_something_else_settles_nothing(self):
        self.decide("Should the open day show a repair demo?", ["pb/process:repair"])
        self.assertEqual(sorted(self.conflicts()), ["attrs.cadence"])

    def test_a_decision_for_the_field_settles_it_until_a_new_value_or_field_arrives(self):
        scope = self.conflicts()["attrs.cadence"]["decide_scope"]
        self.assertEqual(scope, "pa/process:repair#attrs.cadence")
        dec = self.decide("Which cadence holds for Repair?", [scope])
        self.assertEqual(dec["settles"][0]["field"], "attrs.cadence")
        self.assertEqual(self.conflicts(), {})
        clear()
        settled = needs.settled_conflicts(graph.Ontology.load(self.repo), "pb/process:repair")
        self.assertEqual([(c["field"], c["by"]) for c in settled], [("attrs.cadence", dec["id"])])
        # an import update brings a new field disagreement: it opens, the settled field stays settled
        self.pin({"cadence": "monthly", "trigger": "the monthly check"},
                 {"cadence": "weekly", "trigger": "a broken part"})
        self.assertEqual(sorted(self.conflicts()), ["attrs.trigger"])
        # a new value of the settled field opens it again
        self.pin({"cadence": "monthly", "trigger": "x"}, {"cadence": "daily", "trigger": "x"})
        self.assertEqual(sorted(self.conflicts()), ["attrs.cadence"])

    def test_the_cli_scope_spelling_resolves(self):
        code, out, err = _support.run_cli(["decide", "--question", "Which cadence holds for Repair?", "--chosen",
                                           "weekly", "--scope", "pa/process:repair#cadence"], repo=self.repo.root)
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("not in the ontology", out)
        self.assertEqual(self.conflicts(), {})

    def test_an_answer_without_a_decision_keeps_the_conflict_marked_answered(self):
        row = {"id": "ans-20260928-abcdef", "q": "q.gap.conflict.cadence", "at": _support.FIXED_NOW,
               "status": "answered", "src": None, "proposal": None, "node": "pa/process:repair"}
        store.append_jsonl(self.repo.path("interview/log.jsonl"), row)
        gap = self.conflicts()["attrs.cadence"]
        self.assertIn("answered", gap)
        self.assertIn("record which value holds", gap["ask"])
        self.assertIn("pa/process:repair#attrs.cadence", gap["ask"])
        store.append_jsonl(self.repo.path("interview/log.jsonl"), dict(row, id="ans-20260928-abcd01", status="na"))
        self.assertEqual(self.conflicts(), {})
        clear()
        settled = needs.settled_conflicts(graph.Ontology.load(self.repo), "pa/process:repair")
        self.assertEqual([(c["field"], c["by"]) for c in settled], [("attrs.cadence", "n/a")])


def _node(nid, attrs=None):
    from tests.test_graph import mk_node

    return mk_node(nid, attrs=attrs)


# gaps-never-settle -----------------------------------------------------------------------------------------------
class GapSettleTest(Base):
    LOG = "dataset:harvest-log"  # the fixture records the gap {field: attrs.cadence, note: not asked yet}

    def gap_types(self, nid):
        clear()
        onto = graph.Ontology.load(self.repo)
        return [(g["type"], g.get("field") or g.get("rel")) for g in needs.needs(onto, nid)["gaps"]]

    def answer(self, qid, node, status="answered"):
        n = len(lines(self.root, "interview/log.jsonl"))
        store.append_jsonl(self.repo.path("interview/log.jsonl"), {
            "id": "ans-20260928-%06x" % (0xabc000 + n), "q": qid, "at": _support.FIXED_NOW, "status": status,
            "src": None, "proposal": None, "node": node})

    def test_a_recorded_gap_settles_when_its_field_is_filled(self):
        self.assertIn(("open_question", "attrs.cadence"), self.gap_types(self.LOG))
        prop = self.propose([{"op": "update_node", "id": self.LOG, "set": {"attrs.cadence": "weekly"},
                              "reason": "the steward fills the notebook every week",
                              "prov": [{"src": NOTE, "loc": "L3-L3", "by": "agent"}]}])
        self.accept(prop)
        self.assertNotIn(("open_question", "attrs.cadence"), self.gap_types(self.LOG))

    def test_a_recorded_gap_settles_when_its_question_is_answered_or_na(self):
        gap = {"type": "open_question", "field": "attrs.cadence"}
        self.assertEqual(needs.gap_question_id(self.onto(), gap), "q.gap.open_question.cadence")
        self.answer("q.gap.open_question.cadence", self.LOG)
        self.assertNotIn(("open_question", "attrs.cadence"), self.gap_types(self.LOG))

    def test_a_recorded_gap_can_be_closed_with_an_unset(self):
        prop = self.propose([{"op": "update_node", "id": self.LOG, "unset": ["gaps.attrs.cadence"],
                              "reason": "the cadence no longer matters for this notebook",
                              "prov": [{"src": NOTE, "loc": "L3-L3", "by": "agent"}]}])
        self.accept(prop)
        self.assertEqual(self.onto().node(self.LOG)["gaps"], [])
        with self.assertRaises(Refused):  # no such recorded gap
            self.propose([{"op": "update_node", "id": self.LOG, "unset": ["gaps.owner"],
                           "reason": "the owner no longer matters for this notebook",
                           "prov": [{"src": NOTE, "loc": "L3-L3", "by": "agent"}]}])
        self.assertEqual(self.problems(), [])

    def test_a_gap_whose_question_was_set_na_leaves_gaps_and_w01(self):
        onto = self.onto()
        missing = [(nid, g) for nid, need in needs.all_needs(onto).items() for g in need["gaps"]
                   if g["type"] in ("missing_field", "missing_relation")]
        self.assertTrue(missing)
        nid, gap = missing[0]
        self.answer(needs.gap_question_id(onto, gap), nid, status="na")
        clear()
        after = [(g["type"], g.get("field"), g.get("rel")) for g in needs.needs(self.onto(), nid)["gaps"]]
        self.assertNotIn((gap["type"], gap.get("field"), gap.get("rel")), after)
        if gap["type"] == "missing_field":
            clear()
            self.assertFalse([w for w in validate.validate(self.repo).warnings
                              if w.code == "W01" and gap["field"] in w.message and nid in w.message])

    def test_a_later_skip_does_not_reopen_an_answer(self):
        self.answer("q.gap.open_question.cadence", self.LOG)
        self.answer("q.gap.open_question.cadence", self.LOG, status="later")
        self.assertNotIn(("open_question", "attrs.cadence"), self.gap_types(self.LOG))


# answer-confirm-text-promises-partial-write ------------------------------------------------------------------------
class AnswerConfirmTextTest(_support.TempCase):
    def test_the_confirm_text_says_nothing_is_written(self):
        text = commands.ANSWER_CONFIRM["description"]
        self.assertIn("writes nothing", text)
        self.assertNotIn("the rest is written", text)
        code, out, _err = _support.run_cli(["answer", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("writes nothing", " ".join(out.split()))


# erase-cannot-find-name --------------------------------------------------------------------------------------------
class EraseFindTest(Base):
    ROTA = "Watering rota.\nPriya Raman waters the north bed every Tuesday morning.\nThe hose is in shed two.\n"

    def test_find_lists_quotes_source_texts_and_proposals_and_writes_nothing(self):
        sid = self.ingest(self.ROTA, title="Rota two")
        prop = self.propose([{"op": "add_node", "node": {"kind": "process", "name": "Tuesday watering",
                                                         "summary": "The north bed is watered on Tuesdays."},
                              "prov": [{"src": sid, "loc": "L2-L2", "by": "agent",
                                        "quote": "Priya Raman waters the north bed every Tuesday morning"}]}],
                            source=sid)
        self.accept(prop)
        before = _support.snapshot(self.root)
        code, out, err = _support.run_cli(["erase", "--find", "priya  RAMAN", "--json"], repo=self.root)
        self.assertEqual(code, 0, out + err)
        found = json.loads(out)["find"]
        files = {h["file"]: h for h in found["hits"]}
        self.assertIn("sources/%s.txt" % sid, files)
        self.assertEqual(files["sources/%s.txt" % sid]["where"], ["L2"])
        self.assertIn("graph/nodes.jsonl", files)
        self.assertEqual(files["graph/nodes.jsonl"]["id"], "process:tuesday-watering")
        self.assertEqual(files["graph/nodes.jsonl"]["quotes"], [sid])
        self.assertIn("proposals/done/%s.json" % prop["id"], files)
        self.assertEqual(found["erase"], [sid])
        self.assertEqual(_support.snapshot(self.root), before)
        code, out, _err = _support.run_cli(["erase", "--find", "Priya"], repo=self.root)
        self.assertIn("nothing was written", out)
        code, out, _err = _support.run_cli(["erase", "--find", "Nobody Here"], repo=self.root)
        self.assertIn("0 places hold it", out)

    def test_erasing_a_node_lists_an_uncited_source_that_names_it(self):
        named = self.ingest("I am Dana Whitfield, the garden coordinator.\n", title="About me")
        prop = self.propose([{"op": "add_node", "node": {"kind": "person", "name": "Dana Whitfield",
                                                         "summary": "Coordinates the garden."},
                              "prov": [MULCH]}])
        self.accept(prop)
        dec = ledger.decide(self.repo, "Erase the coordinator's data?", ["yes=Yes", "no=No"], "yes",
                            scope=["person:dana-whitfield"])
        code, out, err = _support.run_cli(["erase", "person:dana-whitfield", "--decision", dec["id"], "--json"],
                                          repo=self.root)
        self.assertEqual(code, 0, out + err)
        left = json.loads(out)["names_still_in"]
        self.assertEqual(left[0]["text"], "Dana Whitfield")
        self.assertIn("sources/%s.txt" % named, [h["file"] for h in left[0]["hits"]])
        self.assertIn(named, left[0]["erase"])
        code, out, _err = _support.run_cli(["erase", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("--find", out)
        code, out, _err = _support.run_cli(["erase"], repo=self.root)
        self.assertNotEqual(code, 0)


# init-title-skips-personal-policy ----------------------------------------------------------------------------------
class InitTitleTest(_support.TempCase):
    TITLE = "Plot of sam.patel@outlook.com, 07700 900123"

    def test_the_title_is_redacted_everywhere_init_writes_it(self):
        clear()
        repo = mutate.init_topic(os.path.join(self.tmp, "kit2"), "kit2", "kit2", self.TITLE)
        manifest = store.read_json(repo.path("ontology.json"))
        self.assertNotIn("sam.patel", manifest["title"])
        self.assertIn("[redacted:email]", manifest["title"])
        for rel in ("ontology.json", ledger.CHANGES, "graph/nodes.jsonl"):
            data = read(repo.root, rel).decode("utf-8")
            self.assertNotIn("sam.patel", data, rel)
            self.assertNotIn("900123", data, rel)
        clear()
        self.assertEqual([p.text() for p in validate.validate(repo).problems], [])

    def test_validate_names_a_title_holding_personal_data(self):
        clear()
        repo = mutate.init_topic(os.path.join(self.tmp, "kit3"), "kit3", "kit3", "Plot")
        manifest = store.read_json(repo.path("ontology.json"))
        manifest["title"] = self.TITLE
        store.write_json(repo.path("ontology.json"), manifest)
        clear()
        found = [p for p in validate.validate(store.Repo.open(repo.root)).problems if p.file == "ontology.json"]
        self.assertEqual([p.code for p in found], ["P18"])

    def test_a_credential_in_the_title_is_refused(self):
        clear()
        with self.assertRaises(Refused):
            mutate.init_topic(os.path.join(self.tmp, "kit4"), "kit4", "kit4", "Plot " + _support.fake_secret("github"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "kit4", "ontology.json")))
