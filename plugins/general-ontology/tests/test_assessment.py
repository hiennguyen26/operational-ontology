"""The opt-in assessment pack (SPEC C4): the built-in pack and its questions, ``onto pack list`` and ``onto pack add``
(one ``pack`` change, idempotent, refusals that write nothing, a path with a space and a curly apostrophe), and the
calibration checks ``validate`` runs when the pack is on (W09 for draft risks, P23 once reviewed or approved)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

from tests import _support
from tests.test_graph import archive_block, mk_edge
from tests.test_validate import Base, node
from ontokit import assessment, graph, ledger, mutate, packs, store, util, validate
from ontokit.errors import Refused

ONTO = os.path.join(_support.PLUGIN_DIR, "bin", "onto")
LEVELS = ["very_low", "low", "moderate", "high", "critical"]


def builtin(name):
    with open(os.path.join(packs.BUILTIN_DIR, "%s.pack.json" % name), encoding="utf-8") as fh:
        return json.load(fh)


def bank(name):
    with open(os.path.join(packs.BUILTIN_DIR, "%s.questions.jsonl" % name), encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def clear():
    graph.clear_cache()
    store.clear_cache()


# the pack ------------------------------------------------------------------------------------------------------
class AssessmentPackTest(unittest.TestCase):
    def test_content_matches_spec(self):
        pack = builtin("assessment")
        self.assertEqual(packs.check_pack(pack), [])
        self.assertEqual(pack["pack"], "assessment")
        self.assertEqual(set(pack["kinds"]), {"risk", "control"})
        self.assertEqual({k: (v["from"], v["to"]) for k, v in pack["relations"].items()},
                         {"mitigates": (["control"], ["risk"]), "threatens": (["risk"], "*")})
        risk = pack["kinds"]["risk"]["fields"]
        self.assertEqual(set(risk), {"statement", "ratings", "status", "owner"})
        self.assertEqual(risk["status"]["enum"], ["draft", "reviewed", "approved"])
        self.assertEqual(pack["kinds"]["risk"]["expected"], ["statement", "owner"])
        control = pack["kinds"]["control"]["fields"]
        self.assertEqual(control["type"]["enum"], ["preventive", "detective", "corrective"])
        self.assertEqual(control["nature"]["enum"], ["manual", "automated"])
        self.assertEqual(control["status"]["enum"], ["implemented", "partial", "missing", "planned"])
        self.assertEqual(control["covers"]["type"], "array")
        asks = {(e["rel"], e["dir"]): e["ask"] for e in pack["kinds"]["risk"]["expects"]}
        self.assertEqual(asks[("mitigates", "in")], "What already reduces {name}?")
        self.assertEqual({k: (v["stage"], v["target"]) for k, v in pack["dimensions"].items()}, {"risks": (5, 2)})
        self.assertEqual(tuple(LEVELS), assessment.LEVELS)

    def test_ratings_field_accepts_only_rating_items(self):
        fields = builtin("assessment")["kinds"]["risk"]["fields"]
        good = {"ratings": ["financial.impact=high", "customer-trust.residual=very_low", "regulatory.inherent=low"]}
        self.assertEqual(packs.field_errors(good, fields), [])
        for bad in ("financial.impact=huge", "financial.likelihood=low", "Financial.impact=low", "impact=low",
                    "financial.impact = low"):
            self.assertTrue(packs.field_errors({"ratings": [bad]}, fields), bad)
        self.assertTrue(packs.field_errors({"ratings": ["a.impact=low", "a.impact=low"]}, fields))  # unique items
        self.assertTrue(packs.field_errors({"status": "signed"}, fields))

    def test_loads_clean_with_and_without_discovery(self):
        for names in (["core", "discovery", "assessment"], ["core", "assessment"], ["core", "assessment", "local"]):
            reg = packs.load(None, {"packs": names}, local_pack=packs.empty_local_pack())
            self.assertEqual([p.text() for p in reg.problems()], [], names)
            self.assertEqual(reg.kind_pack("risk"), "assessment")
            self.assertEqual(reg.inverse("mitigates"), "mitigated_by")
            self.assertEqual(reg.dimension_of("control"), "risks")
            self.assertTrue(reg.allowed("threatens", "risk", "dataset" if "discovery" in names else "term"))
            self.assertFalse(reg.allowed("mitigates", "risk", "control"))
        self.assertNotIn("risk", packs.load(None, {"packs": ["core", "discovery"]}).kinds())

    def test_questions(self):
        rows = bank("assessment")
        text = read(packs.BUILTIN_DIR, "assessment.questions.jsonl")
        self.assertEqual(text, "".join(util.canonical_line(r) + "\n" for r in rows))
        ids = [r["id"] for r in rows]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(set(ids), {"q.risks.what", "q.risks.dimensions", "q.risks.ratings", "q.risks.owner",
                                    "q.risks.controls"})
        by_id = {r["id"]: r for r in rows}
        self.assertIn("What could go wrong in {topic}?", by_id["q.risks.what"]["ask"])
        self.assertIn("Who owns each risk", by_id["q.risks.owner"]["ask"])
        self.assertIn("What already reduces each risk", by_id["q.risks.controls"]["ask"])
        self.assertEqual(by_id["q.risks.dimensions"]["options"], list(assessment.DIMENSIONS_SUGGESTED))
        for row in rows:
            self.assertEqual(packs.check_question(row), [], row["id"])
            self.assertEqual((row["stage"], row["dimension"], row["quick"]), (5, "risks", False), row["id"])
        self.assertFalse(any(r.get("quick") for r in rows))  # the quick start stays five questions

    def test_rating_pattern_matches_the_pack(self):
        pattern = re.compile(builtin("assessment")["kinds"]["risk"]["fields"]["ratings"]["items"]["pattern"])
        samples = ["financial.impact=high", "a.residual=very_low", "x-y_1.inherent=critical", "Financial.impact=high",
                   "financial.impact=severe", "financial.likelihood=high", "financial.impact=high ", ".impact=low",
                   "%s.impact=low" % ("a" * 40), "%s.impact=low" % ("a" * 41), "1abc.impact=low", "fin.impact="]
        for item in samples:
            self.assertEqual(bool(pattern.match(item)), bool(assessment.RATING_RE.match(item)), item)

    def test_parse_ratings(self):
        rated, notes = assessment.parse_ratings(["financial.impact=high", "financial.inherent=moderate",
                                                 "customer.residual=low", "financial.impact=low", "junk", 3])
        self.assertEqual(rated, {"financial": {"impact": "high", "inherent": "moderate"},
                                 "customer": {"residual": "low"}})
        self.assertEqual(notes, ["financial: impact is rated twice (high and low); keep one"])
        self.assertEqual(assessment.parse_ratings(None), ({}, []))


# onto pack -----------------------------------------------------------------------------------------------------
class PackCommandTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        parent = os.path.join(self.tmp, "My topic’s folder")
        os.makedirs(parent)
        self.root = _support.init_topic(parent, "garden", "Planning the community garden")
        self.repo = store.Repo.open(self.root)

    def cli(self, *args):
        clear()
        return _support.run_cli(list(args), repo=self.root)

    def manifest(self):
        return store.read_json(os.path.join(self.root, "ontology.json"))

    def test_list_shows_every_builtin_pack(self):
        code, out, err = self.cli("pack", "list", "--json")
        self.assertEqual(code, 0, err)
        found = {p["name"]: p["enabled"] for p in json.loads(out)["packs"]}
        self.assertEqual(found, {"assessment": False, "core": True, "discovery": True})
        code, out, _err = self.cli("pack")  # list is the default action
        self.assertEqual(code, 0)
        self.assertIn("assessment (off)", out)
        self.assertIn("Next: onto pack add assessment", out)

    def test_add_writes_one_pack_change_and_is_idempotent(self):
        code, out, err = self.cli("pack", "add", "assessment")
        self.assertEqual(code, 0, err)
        self.assertIn("added pack assessment", out)
        self.assertEqual(self.manifest()["packs"], ["core", "discovery", "assessment", "local"])
        changes, _ = store.read_jsonl(os.path.join(self.root, ledger.CHANGES))
        self.assertEqual([c["type"] for c in changes], ["init", "pack"])
        self.assertEqual(changes[-1]["summary"], "add pack assessment")
        self.assertEqual(changes[-1]["by"], "user")
        self.assertNotEqual(changes[-1]["before"], changes[-1]["after"])
        points, _ = store.read_jsonl(os.path.join(self.root, "metrics", "history.jsonl"))
        self.assertEqual(points[-1]["kind"], "pack")
        self.assertFalse(os.path.exists(os.path.join(self.root, store.INTENT_REL)))
        clear()
        self.assertEqual([p.text() for p in validate.validate(store.Repo.open(self.root)).problems], [])
        before = _support.snapshot(self.root)
        code, out, _err = self.cli("pack", "add", "assessment")
        self.assertEqual(code, 0)
        self.assertIn("already on", out)
        self.assertEqual(_support.snapshot(self.root), before)
        code, out, _err = self.cli("pack", "list", "--json")
        self.assertTrue({p["name"]: p["enabled"] for p in json.loads(out)["packs"]}["assessment"])
        code, out, _err = self.cli("log", "--json")
        self.assertIn("pack", [c["type"] for c in json.loads(out)["changes"]])

    def test_the_new_questions_join_the_interview(self):
        self.cli("pack", "add", "assessment")
        clear()
        reg = graph.Ontology.load(store.Repo.open(self.root)).registry
        self.assertIn("q.risks.what", [q["id"] for q in reg.questions()])

    def test_refusals_write_nothing(self):
        before = _support.snapshot(self.root)
        code, out, err = self.cli("pack", "add", "nope")
        self.assertNotEqual(code, 0)
        self.assertIn("unknown pack 'nope'", out + err)
        self.assertIn("assessment, core, discovery", out + err)
        code, out, err = self.cli("pack", "add", "local")
        self.assertNotEqual(code, 0)
        code, out, err = self.cli("pack", "add")
        self.assertNotEqual(code, 0)
        self.assertIn("pack add needs the name", out + err)
        code, out, err = self.cli("pack", "list", "assessment")
        self.assertNotEqual(code, 0)
        self.assertEqual(_support.snapshot(self.root), before)

    def test_a_clashing_local_kind_refuses(self):
        local = packs.empty_local_pack()
        local["kinds"]["risk"] = {"label": "Risk", "dimension": "constraints"}
        store.write_json(os.path.join(self.root, packs.LOCAL_PACK), local)
        before = _support.snapshot(self.root)
        clear()
        with self.assertRaises(Refused) as caught:
            mutate.add_pack(store.Repo.open(self.root), "assessment")
        self.assertIn("duplicate kind 'risk'", caught.exception.message)
        self.assertEqual(_support.snapshot(self.root), before)

    def test_list_works_outside_a_topic_and_add_needs_one(self):
        empty = os.path.join(self.tmp, "empty dir’s")
        os.makedirs(empty)
        env = dict(os.environ)
        env.pop("ONTO_REPO", None)
        for args, code_ok in ((["pack", "list", "--json"], True), (["pack", "list"], True),
                              (["pack", "add", "assessment"], False)):
            proc = subprocess.run([sys.executable, ONTO] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  stdin=subprocess.DEVNULL, env=env, cwd=empty)
            out = proc.stdout.decode("utf-8") + proc.stderr.decode("utf-8")
            self.assertEqual(proc.returncode == 0, code_ok, out)
            if args[-1] == "--json":
                body = json.loads(proc.stdout.decode("utf-8"))
                self.assertFalse(body["topic"])
                self.assertEqual({p["name"]: p["enabled"] for p in body["packs"]},
                                 {"assessment": False, "core": False, "discovery": False})
            elif code_ok:
                self.assertIn("No topic here", out)
            else:
                self.assertIn("no topic", out.lower())
        self.assertEqual(os.listdir(empty), [])

    def test_a_failure_while_adding_puts_everything_back(self):
        before = _support.snapshot(self.root)
        with mock.patch.object(mutate.history, "append_point", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                mutate.add_pack(store.Repo.open(self.root), "assessment")
        clear()
        self.assertEqual(_support.snapshot(self.root), before)
        self.assertFalse(os.path.exists(os.path.join(self.root, store.INTENT_REL)))
        self.assertNotIn("assessment", self.manifest()["packs"])

    def test_the_launcher_adds_a_pack_from_a_path_with_a_space(self):
        env = dict(os.environ, ONTO_REPO=self.root)
        proc = subprocess.run([sys.executable, ONTO, "pack", "add", "assessment"], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env=env, cwd=self.root)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertIn("assessment", self.manifest()["packs"])


# calibration ---------------------------------------------------------------------------------------------------
class CalibrationBase(Base):
    def setUp(self):
        super().setUp()
        mutate.add_pack(store.Repo.open(self.root), "assessment")
        clear()

    def risk(self, slug="flood", status=None, owner="Bed steward", statement="Heavy rain floods the beds.",
             ratings=()):
        attrs = {"ratings": list(ratings)}
        for key, value in (("status", status), ("owner", owner), ("statement", statement)):
            if value is not None:
                attrs[key] = value
        return node("risk:%s" % slug, attrs=attrs)

    def control(self, slug, **attrs):
        return node("control:%s" % slug, attrs=attrs)

    def mitigates(self, control, risk="risk:flood"):
        return mk_edge("control:%s" % control, "mitigates", risk)

    def found(self):
        clear()
        report = validate.validate(store.Repo.open(self.root))
        problems = [p.message for p in report.problems if p.code == "P23"]
        warnings = [p.message for p in report.warnings if p.code == "W09"]
        others = [p.text() for p in report.problems if p.code != "P23"]
        self.assertEqual(others, [])
        return problems, warnings

    def apply(self, ops):
        clear()
        return mutate.apply_ops(store.Repo.open(self.root), ops, by="user", change_type="apply",
                                summary="assessment write")


class CalibrationTest(CalibrationBase):
    def test_clean_ratings_pass(self):
        self.add_nodes(self.risk(status="approved", ratings=["financial.impact=high", "financial.inherent=high",
                                                             "financial.residual=moderate"]),
                       self.control("raised-beds", type="preventive", nature="manual", status="partial"))
        self.add_edges(self.mitigates("raised-beds"))
        self.assertEqual(self.found(), ([], []))

    def test_order_rules(self):
        self.add_nodes(self.risk(status="reviewed", ratings=["financial.impact=low", "financial.inherent=moderate",
                                                             "financial.residual=high"]))
        problems, warnings = self.found()
        self.assertEqual(warnings, [])
        self.assertEqual(len(problems), 2, problems)
        self.assertIn("inherent moderate is above impact low", problems[0] + problems[1])
        self.assertIn("residual high is above inherent moderate", problems[0] + problems[1])

    def test_draft_findings_are_warnings(self):
        self.add_nodes(self.risk(status=None, ratings=["customer.inherent=critical", "customer.impact=high",
                                                       "customer.residual=critical"]))
        problems, warnings = self.found()
        self.assertEqual(problems, [])
        self.assertEqual(len(warnings), 1)
        self.assertTrue(warnings[0].startswith("risk:flood: customer: inherent critical is above impact high"))
        self.assertTrue(warnings[0].endswith("(draft)"))
        clear()
        self.assertTrue(validate.validate(store.Repo.open(self.root)).ok)  # drafts never block a build

    def test_reviewed_needs_a_named_owner_and_a_statement(self):
        self.add_nodes(self.risk(status="reviewed", owner="  ", statement=None,
                                 ratings=["financial.impact=low", "financial.inherent=low", "financial.residual=low"]))
        problems, _warnings = self.found()
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(all("ratings stay draft until a named owner reviews them" in p for p in problems))
        self.assertIn("no owner", problems[0] + problems[1])
        self.assertIn("no statement", problems[0] + problems[1])

    def test_a_lower_residual_needs_a_control_in_place(self):
        rated = ["financial.impact=high", "financial.inherent=high", "financial.residual=moderate"]
        self.add_nodes(self.risk(status="approved", ratings=rated),
                       self.control("sandbags", type="corrective", nature="manual", status="planned"))
        self.add_edges(self.mitigates("sandbags"))
        problems, _w = self.found()
        self.assertEqual(len(problems), 1)
        self.assertIn("no implemented or partial control mitigates it", problems[0])
        self.edit("graph/nodes.jsonl", "control:sandbags",
                  attrs={"type": "corrective", "nature": "manual", "status": "implemented"})
        self.assertEqual(self.found(), ([], []))

    def test_covers_limits_a_control_to_its_dimensions(self):
        rated = ["financial.impact=high", "financial.inherent=high", "financial.residual=moderate"]
        self.add_nodes(self.risk(status="approved", ratings=rated),
                       self.control("drain", type="preventive", nature="manual", status="implemented",
                                    covers=["customer"]))
        self.add_edges(self.mitigates("drain"))
        problems, _w = self.found()
        self.assertEqual(len(problems), 1)
        self.edit("graph/nodes.jsonl", "control:drain", attrs={"type": "preventive", "nature": "manual",
                                                               "status": "implemented",
                                                               "covers": ["customer", "financial"]})
        self.assertEqual(self.found(), ([], []))

    def test_a_drop_of_two_levels_needs_an_implemented_automated_control(self):
        rated = ["regulatory.impact=critical", "regulatory.inherent=critical", "regulatory.residual=moderate"]
        self.add_nodes(self.risk(status="approved", ratings=rated),
                       self.control("rota", type="detective", nature="manual", status="implemented"))
        self.add_edges(self.mitigates("rota"))
        problems, _w = self.found()
        self.assertEqual(len(problems), 1)
        self.assertIn("2 levels below inherent critical", problems[0])
        self.assertIn("implemented, automated control", problems[0])
        self.add_nodes(self.control("sensor", type="detective", nature="automated", status="partial"))
        self.add_edges(self.mitigates("sensor"))
        self.assertEqual(len(self.found()[0]), 1)  # partial is not enough for a two-level drop
        self.edit("graph/nodes.jsonl", "control:sensor",
                  attrs={"type": "detective", "nature": "automated", "status": "implemented"})
        self.assertEqual(self.found(), ([], []))

    def test_a_measure_rated_twice(self):
        self.add_nodes(self.risk(status="draft", ratings=["financial.impact=high", "financial.impact=low",
                                                          "financial.inherent=high", "financial.residual=high"]))
        _problems, warnings = self.found()
        self.assertEqual(warnings, ["risk:flood: financial: impact is rated twice (high and low); keep one (draft)"])

    def test_a_bad_rating_item_is_a_field_problem(self):
        self.add_nodes(self.risk(ratings=["financial.impact=severe"]))
        clear()
        report = validate.validate(store.Repo.open(self.root))
        self.assertIn("P08", [p.code for p in report.problems])

    def test_a_draft_risk_without_a_statement_is_a_warning(self):
        self.add_nodes(self.risk(statement=None))
        problems, warnings = self.found()
        self.assertEqual(problems, [])
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("risk:flood: the risk has no statement", warnings[0])

    def test_draft_risk_and_narrowing_warnings_keep_distinct_codes(self):
        # W09 is the assessment pack's draft-risk warning; a narrower of a superseded decision is W10
        self.add_nodes(self.risk(statement=None))
        repo = store.Repo.open(self.root)
        broad = ledger.decide(repo, "Which beds grow vegetables?", [], "The south beds")
        narrow = ledger.decide(repo, "Which vegetables in the south beds?", [], "Tomatoes", narrows=broad["id"])
        ledger.decide(repo, "Which beds grow vegetables?", [], "The north beds", supersedes=broad["id"])
        clear()
        report = validate.validate(store.Repo.open(self.root))
        by_code = {}
        for p in report.warnings:
            by_code.setdefault(p.code, []).append(p)
        self.assertEqual(len(by_code.get("W09", [])), 1)
        self.assertTrue(by_code["W09"][0].message.startswith("risk:flood: the risk has no statement"))
        self.assertEqual([p.file for p in by_code.get("W10", [])], ["ledger/decisions/%s.json" % narrow["id"]])
        self.assertEqual(report.problems, [])

    def test_archived_controls_and_edges_do_not_count(self):
        rated = ["financial.impact=high", "financial.inherent=high", "financial.residual=moderate"]
        self.add_nodes(self.risk(status="approved", ratings=rated),
                       self.control("raised-beds", type="preventive", nature="manual", status="implemented"))
        self.add_edges(self.mitigates("raised-beds"))
        self.assertEqual(self.found(), ([], []))
        eid = self.mitigates("raised-beds")["id"]
        self.edit("graph/edges.jsonl", eid, status="archived", archived=archive_block(decision=self.decision))
        problems, _w = self.found()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("no implemented or partial control mitigates it", problems[0])
        self.edit("graph/nodes.jsonl", "control:raised-beds", status="archived",
                  archived=archive_block(decision=self.decision))
        self.edit("graph/edges.jsonl", eid, status="confirmed", archived=None)
        clear()
        report = validate.validate(store.Repo.open(self.root))
        self.assertIn("P13", [p.code for p in report.problems])  # the edge touches an archived node
        problems = [p.message for p in report.problems if p.code == "P23"]
        self.assertEqual(len(problems), 1, problems)

    def test_threatens_reaches_a_kind_of_another_pack(self):
        self.add_nodes(self.risk(status="approved", ratings=["customer.impact=high", "customer.inherent=high",
                                                             "customer.residual=high"]))
        self.add_edges(mk_edge("risk:flood", "threatens", "constraint:no-pesticides"))
        self.assertEqual(self.found(), ([], []))

    def test_a_write_that_adds_a_p23_is_refused(self):
        bad = ["financial.impact=low", "financial.inherent=high"]
        self.add_nodes(self.risk(status="draft", ratings=bad))
        before = _support.snapshot(self.root)
        with self.assertRaises(Refused) as caught:
            self.apply([{"n": 1, "op": "update_node", "id": "risk:flood",
                         "set": {"attrs.status": "reviewed"},
                         "unset": [], "prov": []}])
        self.assertIn("P23", caught.exception.message)
        self.assertIn("inherent high is above impact low", caught.exception.message)
        clear()
        self.assertEqual(_support.snapshot(self.root), before)
        dry = mutate.apply_ops(store.Repo.open(self.root), [{"n": 1, "op": "update_node", "id": "risk:flood",
                                                             "set": {"attrs.status": "approved"},
                                                             "unset": [], "prov": []}],
                               by="user", change_type="apply", summary="dry", dry_run=True)
        self.assertIn("P23", [p.code for p in dry["problems"]])

    def test_archiving_a_control_a_reviewed_risk_relies_on_is_refused(self):
        rated = ["financial.impact=high", "financial.inherent=high", "financial.residual=moderate"]
        self.add_nodes(self.risk(status="approved", ratings=rated),
                       self.control("raised-beds", type="preventive", nature="manual", status="implemented"))
        self.add_edges(self.mitigates("raised-beds"))
        self.assertEqual(self.found(), ([], []))
        before = _support.snapshot(self.root)
        archive = {"n": 1, "op": "archive", "id": "control:raised-beds",
                   "archived": {"reason": "the raised beds were taken down", "decision": self.decision,
                                "superseded_by": []}}
        with self.assertRaises(Refused) as caught:
            self.apply([archive])
        self.assertIn("P23 ", caught.exception.message)
        clear()
        self.assertEqual(_support.snapshot(self.root), before)
        # raising the residual first makes the archive fine
        self.apply([{"n": 1, "op": "update_node", "id": "risk:flood",
                     "set": {"attrs.ratings": ["financial.impact=high", "financial.inherent=high",
                                               "financial.residual=high"]},
                     "unset": [], "prov": []}])
        self.apply([archive])
        self.assertEqual(self.found(), ([], []))

    def test_an_existing_p23_does_not_block_other_writes(self):
        self.add_nodes(self.risk(status="reviewed", ratings=["financial.impact=low", "financial.inherent=high",
                                                                 "financial.residual=high"]))
        self.assertEqual(len(self.found()[0]), 1)
        self.apply([{"n": 1, "op": "update_node", "id": "constraint:no-pesticides",
                     "set": {"summary": "No pesticides are used anywhere in the garden, ever."},
                     "unset": [], "prov": []}])
        self.assertEqual(len(self.found()[0]), 1)

    def test_nothing_runs_without_the_pack(self):
        self.add_nodes(self.risk(status="reviewed", ratings=["financial.impact=low", "financial.inherent=high",
                                                                 "financial.residual=high"]))
        clear()
        onto = graph.Ontology.load(store.Repo.open(self.root))
        self.assertEqual(len(assessment.calibration(onto, lambda rid: ("graph/nodes.jsonl", 0))[0]), 1)
        manifest = store.read_json(os.path.join(self.root, "ontology.json"))
        manifest["packs"] = [n for n in manifest["packs"] if n != "assessment"]
        off = graph.Ontology.from_rows(None, manifest, [onto.nodes[i] for i in onto.local_ids], [])
        self.assertEqual(assessment.calibration(off, lambda rid: ("graph/nodes.jsonl", 0)), ([], []))


class IncompleteRatingsTest(CalibrationBase):
    """A reviewed or approved risk must rate impact, inherent and residual on every dimension it rates, and rate at
    least one dimension; a draft one gets the same findings as W09 warnings (an unrated draft is left alone)."""

    FULL = ["financial.impact=high", "financial.inherent=high", "financial.residual=high"]

    def test_an_approved_risk_rated_on_impact_only_is_a_problem(self):
        self.add_nodes(self.risk(status="approved", statement="Flooding", ratings=["financial.impact=high"]))
        problems, warnings = self.found()
        self.assertEqual(warnings, [])
        self.assertEqual(problems, ["risk:flood: financial: inherent and residual are not rated (every rated "
                                    "dimension needs impact, inherent and residual)"])

    def test_each_missing_measure_is_named(self):
        cases = ((["financial.inherent=high"], "impact and residual are"),
                 (["financial.residual=low"], "impact and inherent are"),
                 (["financial.impact=high", "financial.inherent=high"], "residual is"),
                 (["financial.impact=high", "financial.residual=high"], "inherent is"),
                 (["financial.inherent=high", "financial.residual=high"], "impact is"))
        self.add_nodes(*[self.risk(slug="flood-%d" % i, status="reviewed", ratings=rated)
                         for i, (rated, _m) in enumerate(cases)])
        problems, _w = self.found()
        for i, (rated, missing) in enumerate(cases):
            with self.subTest(rated=rated):
                self.assertIn("risk:flood-%d: financial: %s not rated (every rated dimension needs impact, inherent "
                              "and residual)" % (i, missing), problems)

    def test_an_approved_risk_with_no_ratings_is_a_problem(self):
        empty = self.risk(slug="flood-empty", status="approved")
        absent = self.risk(slug="flood-absent", status="approved")
        del absent["attrs"]["ratings"]
        self.add_nodes(empty, absent)
        problems, warnings = self.found()
        self.assertEqual(warnings, [])
        self.assertEqual(sorted(problems), ["risk:flood-%s: ratings are approved but the risk has no ratings; rate "
                                            "impact, inherent and residual on at least one dimension "
                                            "(financial.impact=high)" % slug for slug in ("absent", "empty")])

    def test_only_the_incomplete_dimension_is_named(self):
        self.add_nodes(self.risk(status="approved", ratings=self.FULL + ["customer.impact=low"]))
        problems, _w = self.found()
        self.assertEqual(len(problems), 1, problems)
        self.assertTrue(problems[0].startswith("risk:flood: customer: inherent and residual are not rated"))

    def test_a_complete_risk_passes(self):
        self.add_nodes(self.risk(status="approved", ratings=self.FULL))
        self.assertEqual(self.found(), ([], []))

    def test_an_incomplete_draft_is_a_warning_that_never_blocks(self):
        self.add_nodes(self.risk(status="draft", ratings=["financial.impact=high"]))
        problems, warnings = self.found()
        self.assertEqual(problems, [])
        self.assertEqual(warnings, ["risk:flood: financial: inherent and residual are not rated (every rated "
                                    "dimension needs impact, inherent and residual) (draft)"])
        clear()
        self.assertTrue(validate.validate(store.Repo.open(self.root)).ok)

    def test_an_unrated_draft_is_left_alone(self):
        self.add_nodes(self.risk(status=None))
        self.assertEqual(self.found(), ([], []))

    def test_a_measure_rated_twice_is_not_missing(self):
        self.add_nodes(self.risk(status="approved", ratings=self.FULL + ["financial.impact=low"]))
        problems, _w = self.found()
        self.assertEqual(problems, ["risk:flood: financial: impact is rated twice (high and low); keep one"])

    def test_approving_an_incomplete_risk_is_refused_and_names_the_missing_measures(self):
        self.add_nodes(self.risk(status="draft", statement="Flooding", ratings=["financial.impact=high"]))
        self.assertEqual(len(self.found()[1]), 1)
        before = _support.snapshot(self.root)
        op = {"n": 1, "op": "update_node", "id": "risk:flood", "set": {"attrs.status": "approved"}, "unset": [],
              "prov": []}
        with self.assertRaises(Refused) as caught:
            self.apply([op])
        self.assertIn("P23", caught.exception.message)
        self.assertIn("financial: inherent and residual are not rated", caught.exception.message)
        clear()
        self.assertEqual(_support.snapshot(self.root), before)
        dry = mutate.apply_ops(store.Repo.open(self.root), [op], by="user", change_type="apply", summary="dry",
                               dry_run=True)
        self.assertIn("P23", [p.code for p in dry["problems"]])
        # rating the two missing measures in the same write makes it fine
        self.apply([dict(op, set={"attrs.status": "approved", "attrs.ratings": self.FULL})])
        self.assertEqual(self.found(), ([], []))

    def test_approving_an_unrated_risk_is_refused(self):
        self.add_nodes(self.risk(status="draft"))
        before = _support.snapshot(self.root)
        with self.assertRaises(Refused) as caught:
            self.apply([{"n": 1, "op": "update_node", "id": "risk:flood", "set": {"attrs.status": "reviewed"},
                         "unset": [], "prov": []}])
        self.assertIn("the risk has no ratings", caught.exception.message)
        clear()
        self.assertEqual(_support.snapshot(self.root), before)

    def test_dropping_a_measure_or_every_rating_of_an_approved_risk_is_refused(self):
        self.add_nodes(self.risk(status="approved", ratings=self.FULL))
        before = _support.snapshot(self.root)
        for change, says in (({"set": {"attrs.ratings": self.FULL[:2]}, "unset": []}, "residual is not rated"),
                             ({"set": {"attrs.ratings": []}, "unset": []}, "the risk has no ratings"),
                             ({"set": {}, "unset": ["attrs.ratings"]}, "the risk has no ratings")):
            with self.subTest(change=change):
                with self.assertRaises(Refused) as caught:
                    self.apply([dict({"n": 1, "op": "update_node", "id": "risk:flood", "prov": []}, **change)])
                self.assertIn(says, caught.exception.message)
                clear()
                self.assertEqual(_support.snapshot(self.root), before)


class DocsTest(unittest.TestCase):
    def test_readme_describes_the_pack(self):
        text = read(_support.PLUGIN_DIR, "README.md")
        section = text[text.index("## Packs"):]
        section = section[:section.index("\n## ", 4)]
        for phrase in ("onto pack add assessment", "financial.impact=high", "W09", "P23", "covers"):
            self.assertIn(phrase, section)
        self.assertIsNone(re.search("\u2014", section))

    def test_docs_name_the_complete_ratings_rule(self):
        section = read(_support.PLUGIN_DIR, "README.md")
        section = section[section.index("**Calibration.**"):section.index("**Rating chips.**")]
        self.assertIn("all three measures", section)
        self.assertIn("rates at least one dimension", section)
        review = read(_support.PLUGIN_DIR, "skills", "onto-review", "SKILL.md")
        self.assertIn("rate the missing measures", review)
        self.assertIn("lacks its impact, inherent or", review)


if __name__ == "__main__":
    unittest.main()
