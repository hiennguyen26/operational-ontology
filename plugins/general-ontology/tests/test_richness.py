"""richness: each measure on hand-computed topics, thin nodes and coverage, nulls with a reason, the score and its
bands, the topic gaps (missing_dimension, unbridged_import, uncited_source, duplicate, pending_backlog), the ranked
list and its filters, source suggestions, the Brier score on known verdicts, history changes, the history point
mutate writes, and the tools check."""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import stat
from unittest import mock

from tests import _support
from tests.test_graph import (GARDEN_PACK, SRC, add_import, archive_block, make_export, mk_edge, mk_node, source_row,
                              write_topic)
from ontokit import commands, entities, graph, history, mcp_server, mutate, records, richness, store, util

SRC2 = "src-" + "2" * 12
SRC3 = "src-" + "3" * 12
SRC4 = "src-" + "4" * 12
SRC5 = "src-" + "5" * 12
SRC6 = "src-" + "6" * 12
KITCHEN_PACK = {
    "pack": "local", "version": 1, "extends": ["core"],
    "kinds": {"ingredient": {"label": "Ingredient", "plural": "ingredients", "dimension": "data"}},
    "relations": {}, "dimensions": {}, "deliverables": {}, "kind_map": [],
}


def clear():
    graph.clear_cache()
    store.clear_cache()


def add_source(repo, sid, text, **extra):
    """Add a source line (and its text file) to the index of ``repo``."""
    rows, _problems = store.read_jsonl(repo.path("sources/index.jsonl"))
    rows = [r for r in rows if r.get("id") != sid] + [source_row(sid=sid, text=text, **extra)]
    store.write_jsonl(repo.path("sources/index.jsonl"), rows)
    store.write_bytes(repo.path("sources/%s.txt" % sid), text.encode("utf-8"))
    clear()


def write_rows(repo, rel, rows):
    store.write_jsonl(repo.path(rel), rows)
    clear()


def prov(*srcs):
    return [{"src": s, "loc": "L1-L1", "by": "user"} for s in srcs]


def base_topic(tmp):
    """The hand-computed topic (see ``MeasuresTest``)."""
    nodes = [
        mk_node("goal:harvest", "Harvest"),
        mk_node("metric:yield", "Yield", attrs={"unit": "kg"}),
        mk_node("dataset:harvest-log", "Harvest log", status="proposed", trust="agent", attrs={"location": "shed"}),
        mk_node("role:steward", "Steward"),
        mk_node("term:mulch", "Mulch", summary=""),
    ]
    edges = [
        mk_edge("metric:yield", "measures", "goal:harvest"),
        mk_edge("role:steward", "owns", "dataset:harvest-log", status="proposed", trust="agent",
                prov=prov(SRC, SRC2)),
        mk_edge("role:steward", "related_to", "term:mulch", background=True, symmetric=True),
    ]
    repo = write_topic(tmp, nodes, edges, ns="plot")
    add_source(repo, SRC2, "a second note\n")
    return repo


def load(repo):
    clear()
    return graph.Ontology.load(repo.root)


class MeasuresTest(_support.TempCase):
    """Five active nodes, two counted edges and one background edge:

    - completeness: expected fields metric.unit (filled), dataset.location (filled), dataset.format -> 2 / 3;
    - connectivity: goal measures(in), metric measures(out), dataset owns(in) all met -> 3 / 3; the mulch term has
      only a background edge, so it is an orphan -> 1 x (1 - 1/5) = 0.8;
    - evidence: five nodes on one source (0.5 each), the measures edge on one source (0.5), the owns edge on two
      (1) -> 4 / 7;
    - confirmation: nodes 4 confirmed and 1 proposed, edges 1 and 1 -> 5 / 7;
    - coverage: frame 1/2 (goal), people 1/3 (steward), data 1 (the dataset plus two cited sources), deliverables
      1/1 (metric), vocabulary 0 (mulch is thin), process, constraints and questions 0 -> (0.5 + 1/3 + 1 + 1) / 8;
    - depth: the evidence sum 4 (five nodes and the measures edge at 0.5, the owns edge at 1) against the target sum
      of the eight dimensions (2 + 3 + 3 + 1 + 5 + 2 + 2 + 2 = 20) -> 1 - 2^(-4 / 20).
    """

    def setUp(self):
        super().setUp()
        self.repo = base_topic(self.tmp)
        self.onto = load(self.repo)

    def test_each_measure(self):
        m = richness.measures(self.onto)
        v = m["values"]
        self.assertEqual(v["completeness"], round(2 / 3.0, 4))
        self.assertEqual(v["connectivity"], 0.8)
        self.assertEqual(v["evidence"], round(4 / 7.0, 4))
        self.assertEqual(v["confirmation"], round(5 / 7.0, 4))
        self.assertEqual(v["coverage"], round((0.5 + 1 / 3.0 + 1 + 1) / 8, 4))
        self.assertEqual(m["denominators"]["completeness"], 3)
        self.assertEqual(m["denominators"]["connectivity"], 3)
        self.assertEqual(m["denominators"]["evidence"], 7)
        self.assertEqual(m["denominators"]["confirmation"], 7)
        self.assertEqual(m["denominators"]["coverage"], 8)
        self.assertEqual(v["depth"], round(1 - 2 ** (-4 / 20.0), 4))
        self.assertEqual(m["denominators"]["depth"], 20)
        self.assertEqual(m["breakdowns"]["depth_mass"], 4.0)
        dims = m["breakdowns"]["dimensions"]
        self.assertEqual((dims["frame"], dims["data"], dims["vocabulary"]), (0.5, 1.0, 0.0))
        self.assertIsNone(v["brier"])
        self.assertIn("brier", m["missing"])

    def test_score_is_the_weighted_mean_with_quality_scaled_by_coverage_and_depth(self):
        v = richness.measures(self.onto)["values"]
        quality = 20 * v["completeness"] + 15 * v["connectivity"] + 20 * v["evidence"] + 15 * v["confirmation"]
        expected = int(30 * v["coverage"] + v["coverage"] * v["depth"] * quality + 0.5)
        self.assertEqual(v["richness"], expected)
        self.assertEqual(v["richness"], 13)  # 10.6 of coverage + 0.354 x 0.129 x 47.5
        s = richness.summary(self.onto)
        self.assertEqual((s["score"], s["band"]), (13, "seed"))
        self.assertEqual(set(s["parts"]), set(richness.PARTS))
        self.assertEqual(s["depth"], {"value": v["depth"], "mass": 4.0, "scale": 20, "scales": True})

    def test_breakdowns(self):
        b = richness.measures(self.onto)["breakdowns"]
        self.assertEqual(b["nodes_by_kind"], {"dataset": 1, "goal": 1, "metric": 1, "role": 1, "term": 1})
        self.assertEqual(b["edges_by_rel"], {"measures": 1, "owns": 1, "related_to": 1})
        self.assertEqual((b["sources"], b["pending"], b["open_questions"], b["bridges"]), (2, 0, 0, {}))

    def test_point_passes_its_schema_and_is_deterministic(self):
        p = richness.point(self.onto, "apply")
        self.assertEqual(records.check(p, "point"), [])
        self.assertEqual(p["kind"], "apply")
        self.assertEqual(richness.point(self.onto, "no-such-kind")["kind"], "apply")
        again = richness.point(load(self.repo), "apply")
        self.assertEqual(util.canonical_bytes(p), util.canonical_bytes(again))

    def test_partial_minimums_count_toward_connectivity(self):
        pack = json.loads(json.dumps(GARDEN_PACK))
        pack["kinds"]["plot"]["expects"] = [{"rel": "grown_in", "dir": "in", "min": 2, "ask": "What grows in {name}?"}]
        nodes = [mk_node("plot:north", "North"), mk_node("crop:bean", "Bean")]
        edges = [mk_edge("crop:bean", "grown_in", "plot:north")]
        repo = write_topic(os.path.join(self.tmp, "p"), nodes, edges, ns="beds", local_pack=pack)
        v = richness.measures(load(repo))["values"]
        self.assertEqual(v["connectivity"], 0.5)  # 1 of 2 required, no orphans

    def test_thin_nodes_do_not_raise_coverage(self):
        before = richness.measures(self.onto)["breakdowns"]["dimensions"]["vocabulary"]
        nodes, _p = store.read_jsonl(self.repo.path("graph/nodes.jsonl"))
        nodes.append(mk_node("term:compost", "Compost", summary=""))
        write_rows(self.repo, "graph/nodes.jsonl", nodes)
        self.assertEqual(richness.measures(load(self.repo))["breakdowns"]["dimensions"]["vocabulary"], before)
        nodes[-1] = mk_node("term:compost", "Compost", summary="Rotted plant matter spread on beds.")
        edges, _p = store.read_jsonl(self.repo.path("graph/edges.jsonl"))
        edges.append(mk_edge("term:compost", "about", "goal:harvest"))
        write_rows(self.repo, "graph/nodes.jsonl", nodes)
        write_rows(self.repo, "graph/edges.jsonl", edges)
        self.assertEqual(richness.measures(load(self.repo))["breakdowns"]["dimensions"]["vocabulary"], 0.2)

    def test_background_edges_never_count(self):
        edges, _p = store.read_jsonl(self.repo.path("graph/edges.jsonl"))
        write_rows(self.repo, "graph/edges.jsonl", [e for e in edges if not e.get("background")])
        v = richness.measures(load(self.repo))["values"]
        self.assertEqual((v["evidence"], v["confirmation"], v["connectivity"]),
                         (round(4 / 7.0, 4), round(5 / 7.0, 4), 0.8))


class NullTest(_support.TempCase):
    def test_empty_topic_has_null_evidence_with_a_reason(self):
        repo = write_topic(self.tmp, [], [], ns="empty")
        m = richness.measures(load(repo))
        self.assertIsNone(m["values"]["evidence"])
        self.assertEqual(m["missing"]["evidence"], "no active local records")
        self.assertEqual((m["values"]["completeness"], m["values"]["confirmation"], m["values"]["connectivity"]),
                         (1.0, 1.0, 1.0))
        self.assertEqual(m["values"]["coverage"], 0.0)
        self.assertEqual(m["values"]["richness"], 0)
        self.assertEqual(records.check(richness.point(load(repo), "init"), "point"), [])

    def test_fresh_topic_is_a_seed(self):
        root = _support.init_topic(self.tmp, "fresh")
        s = richness.summary(load(store.Repo.open(root)))
        self.assertEqual(s["band"], "seed")
        self.assertLess(s["score"], 20)

    def test_closed_dimensions_leave_coverage_and_every_closed_nulls_it(self):
        repo = base_topic(self.tmp)
        onto = load(repo)
        dims = sorted(onto.registry.dimensions())
        questions = [{"id": "q.wp5.%s" % d, "ask": "About %s?" % d, "dimension": d, "stage": 1, "priority": 10}
                     for d in dims]
        write_rows(repo, "packs/local.questions.jsonl", questions)
        onto = load(repo)
        self.assertEqual(richness.closed_dimensions(onto), [])
        log = [{"id": "ans-20260928-00000%d" % i, "q": "q.wp5.vocabulary", "at": "2026-09-28T12:00:00Z",
                "status": status, "src": None, "proposal": None, "node": None}
               for i, status in enumerate(("answered", "na"))]
        store.write_jsonl(repo.path("interview/log.jsonl"), log, key=None)
        # the graph files did not change, so the cached graph is reused: the log is part of the richness cache key
        self.assertIs(graph.Ontology.load(repo.root), onto)
        m = richness.measures(onto)
        self.assertEqual(richness.closed_dimensions(onto), ["vocabulary"])
        self.assertNotIn("vocabulary", m["breakdowns"]["dimensions"])
        self.assertEqual(m["values"]["coverage"], round((0.5 + 1 / 3.0 + 1 + 1) / 7, 4))
        log = [dict(log[0], id="ans-20260928-0000%02d" % i, q="q.wp5.%s" % d, status="na")
               for i, d in enumerate(dims)]
        store.write_jsonl(repo.path("interview/log.jsonl"), log, key=None)
        m = richness.measures(graph.Ontology.load(repo.root))
        self.assertIsNone(m["values"]["coverage"])
        self.assertIn("closed", m["missing"]["coverage"])
        v = m["values"]
        weighted = (20 * v["completeness"] + 15 * v["connectivity"] + 20 * v["evidence"] + 15 * v["confirmation"])
        self.assertEqual(v["richness"], int(weighted / 70 * 100 + 0.5))


class ScoreTest(_support.TempCase):
    def test_bands(self):
        cases = [(0, "seed"), (19, "seed"), (20, "sketch"), (39, "sketch"), (40, "working"), (59, "working"),
                 (60, "rich"), (79, "rich"), (80, "deep"), (100, "deep")]
        for value, name in cases:
            self.assertEqual(richness.band(value), name, value)
        self.assertIsNone(richness.band(None))

    def test_score_rules(self):
        full = {"coverage": 1.0, "completeness": 0.5, "connectivity": 0.5, "evidence": 0.5, "confirmation": 0.5}
        self.assertEqual(richness.score(full), (65, "rich"))  # coverage 1: exactly the weighted mean
        self.assertEqual(richness.score(dict(full, coverage=0.5)), (33, "sketch"))  # 15 + 0.5 x 35 = 32.5, half up
        self.assertEqual(richness.score(dict(full, evidence=None)), (69, "rich"))  # 55 / 80 = 68.75, half up
        self.assertEqual(richness.score(full, {"coverage": 0, "completeness": 1, "connectivity": 0, "evidence": 0,
                                                "confirmation": 0}), (50, "working"))
        self.assertEqual(richness.score({k: None for k in full}), (None, None))
        self.assertEqual(richness.score(dict(full, coverage=None)), (50, "working"))  # no coverage: no scaling

    def test_policy_weights_are_read(self):
        repo = base_topic(self.tmp)
        manifest = store.read_json(repo.path("ontology.json"))
        manifest.setdefault("policy", {})["weights"] = {"coverage": 0, "completeness": 0, "connectivity": 0,
                                                        "evidence": 0, "confirmation": 100}
        store.write_json(repo.path("ontology.json"), manifest)
        s = richness.summary(load(store.Repo.open(repo.root)))
        # a coverage weight of 0 leaves coverage out entirely: it scales nothing
        self.assertLess(s["parts"]["coverage"], 1)
        self.assertEqual(s["score"], int(s["parts"]["confirmation"] * 100 + 0.5))
        self.assertEqual(s["weights"]["confirmation"], 100)
        self.assertEqual(s["formula"], richness.PLAIN_FORMULA)

    def test_zero_coverage_weight_gives_the_plain_weighted_mean(self):
        parts = {"coverage": 0.5, "completeness": 1.0, "connectivity": 1.0, "evidence": 1.0, "confirmation": 1.0}
        self.assertEqual(richness.score(parts, {"coverage": 0}), (100, "deep"))
        self.assertEqual(richness.score(dict(parts, coverage=0.0), {"coverage": 0}), (100, "deep"))
        self.assertEqual(richness.score(dict(parts, coverage=0.0)), (0, "seed"))  # weighted coverage still scales

    def test_the_switch_restores_the_plain_weighted_mean(self):
        parts = {"coverage": 0.0, "completeness": 1.0, "connectivity": 1.0, "evidence": 1.0, "confirmation": 1.0}
        with mock.patch.object(richness, "SCALE_BY_COVERAGE", False):
            self.assertEqual(richness.score(parts), (70, "rich"))
            self.assertEqual(richness.score(dict(parts, coverage=0.5)), (85, "deep"))


class HistoryChangeTest(_support.TempCase):
    def test_change7_and_change30_against_the_history(self):
        repo = base_topic(self.tmp)
        points = [history.new_point("apply", {"richness": 5}, at="2026-08-25T12:00:00Z"),
                  history.new_point("apply", {"richness": 10}, at="2026-09-20T12:00:00Z"),
                  history.new_point("apply", {"richness": 20}, at="2026-09-26T12:00:00Z")]
        store.write_jsonl(repo.path("metrics/history.jsonl"), points, key=None)
        s = richness.summary(load(repo))
        self.assertEqual(s["change7"], {"delta": 13 - 10, "at": "2026-09-20T12:00:00Z", "text": "+3 since 09-20"})
        self.assertEqual(s["change30"]["delta"], 8)
        code, out, err = _support.run_cli(["richness"], repo.root)
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("plot unreleased | richness 13 seed (+3 since 09-20)"), out)

    def test_no_change_without_an_old_enough_point(self):
        repo = base_topic(self.tmp)
        s = richness.summary(load(repo))
        self.assertIsNone(s["change7"])
        self.assertIsNone(s["change30"])


TERMS = ("mulch", "compost", "tilth", "bolting", "hardening")


def first_pass_topic(tmp):
    """Every dimension just at its target, as a short interview leaves it: 20 confirmed, linked nodes and 16 edges,
    each on one source (``SRC``, a note, which is also the third data item), every expected field and link there.
    Evidence sum 36 x 0.5 = 18 against the target sum 20."""
    data = {"location": "shed", "format": "paper"}
    nodes = [
        mk_node("goal:harvest", "Harvest"), mk_node("goal:share", "Share"),
        mk_node("metric:yield", "Yield", attrs={"unit": "kg"}), mk_node("metric:shares", "Shares", attrs={"unit": "n"}),
        mk_node("role:steward", "Steward"), mk_node("role:waterer", "Waterer"), mk_node("org:council", "Council"),
        mk_node("dataset:harvest-log", "Harvest log", attrs=data), mk_node("dataset:rota", "Rota", attrs=data),
        mk_node("process:watering", "Watering"), mk_node("process:harvesting", "Harvesting"),
        mk_node("constraint:no-pesticides", "No pesticides"), mk_node("constraint:hose-ban", "Hose ban"),
        mk_node("question:second-plot", "Second plot?"), mk_node("question:rainwater", "Rainwater?"),
    ] + [mk_node("term:" + t, t.capitalize()) for t in TERMS]
    edges = [
        mk_edge("metric:yield", "measures", "goal:harvest"), mk_edge("metric:shares", "measures", "goal:share"),
        mk_edge("role:steward", "owns", "dataset:harvest-log"), mk_edge("role:waterer", "owns", "dataset:rota"),
        mk_edge("role:waterer", "owns", "process:watering"), mk_edge("role:steward", "owns", "process:harvesting"),
        mk_edge("org:council", "decides", "constraint:hose-ban"),
        mk_edge("constraint:no-pesticides", "constrains", "process:harvesting"),
        mk_edge("constraint:hose-ban", "constrains", "process:watering"),
        mk_edge("question:second-plot", "about", "goal:harvest"),
        mk_edge("question:rainwater", "about", "process:watering"),
    ] + [mk_edge("term:" + t, "about", "process:watering") for t in TERMS]
    repo = write_topic(tmp, nodes, edges, ns="plot")
    add_source(repo, SRC2, "notes from the second walk round\n")
    return repo


class GrowthTest(_support.TempCase):
    """The score keeps growing as reviewed, sourced material comes in (the plateau-and-drop regression): a topic
    whose small targets were met by a short interview is not "deep", and a reviewed batch that opens a few gaps
    still raises it."""

    def setUp(self):
        super().setUp()
        self.repo = first_pass_topic(self.tmp)

    def add(self, nodes=(), edges=(), replace=()):
        rows, _p = store.read_jsonl(self.repo.path("graph/nodes.jsonl"))
        swap = {n["id"]: n for n in replace}
        rows = [swap.pop(r["id"], r) for r in rows] + list(nodes)
        write_rows(self.repo, "graph/nodes.jsonl", rows)
        if edges:
            old, _p = store.read_jsonl(self.repo.path("graph/edges.jsonl"))
            write_rows(self.repo, "graph/edges.jsonl", old + list(edges))
        return richness.summary(load(self.repo))

    def test_meeting_every_small_target_is_not_deep(self):
        s = richness.summary(load(self.repo))
        p = s["parts"]
        self.assertEqual((p["coverage"], p["completeness"], p["connectivity"], p["evidence"], p["confirmation"]),
                         (1.0, 1.0, 1.0, 0.5, 1.0))
        self.assertEqual(s["depth"]["mass"], 18.0)
        self.assertEqual(s["depth"]["value"], round(1 - 2 ** (-18 / 20.0), 4))
        self.assertEqual(richness.score(p), (90, "deep"))  # the parts alone: what a short interview used to score
        self.assertEqual((s["score"], s["band"]), (58, "working"))  # 30 + 0.464 x 60

    def test_a_reviewed_batch_that_opens_gaps_still_raises_the_score(self):
        before = richness.summary(load(self.repo))
        doc = prov(SRC2)
        after = self.add(nodes=[
            mk_node("person:ana", "Ana", prov=doc),  # a person short of works_on
            mk_node("claim:mulch-holds-water", "Mulch holds water", prov=doc),
            mk_node("constraint:frost-date", "Frost date", prov=doc),
            mk_node("constraint:gate-hours", "Gate hours", status="proposed", prov=doc),  # a draft with no link
            mk_node("question:bees", "Bees?", status="proposed", prov=doc),
        ], edges=[
            mk_edge("person:ana", "member_of", "role:waterer", prov=doc),
            mk_edge("claim:mulch-holds-water", "about", "term:mulch", prov=doc),
            mk_edge("constraint:frost-date", "constrains", "process:watering", prov=doc),
            mk_edge("question:bees", "about", "goal:harvest", status="proposed", prov=doc),
        ], replace=[mk_node("dataset:rota", "Rota", attrs={"location": "shed", "format": "paper"},
                            prov=prov(SRC, SRC2))])  # a second source for a known record
        # every ratio the batch touches fell, and the parts alone would have lowered the score
        self.assertLess(after["parts"]["connectivity"], before["parts"]["connectivity"])
        self.assertLess(after["parts"]["confirmation"], before["parts"]["confirmation"])
        self.assertLess(richness.score(after["parts"])[0], richness.score(before["parts"])[0])
        # the material itself counts: 9 single-source records and one added source
        self.assertEqual(after["depth"]["mass"], before["depth"]["mass"] + 5.0)
        self.assertGreater(after["score"], before["score"])
        # answering the two gaps the batch raised raises it again
        closed = self.add(edges=[mk_edge("person:ana", "works_on", "process:watering", prov=prov(SRC2)),
                                 mk_edge("constraint:gate-hours", "constrains", "process:watering",
                                         prov=prov(SRC2))])
        self.assertGreater(closed["score"], after["score"])

    def test_the_score_keeps_growing_past_the_targets(self):
        scores = [richness.summary(load(self.repo))["score"]]
        for batch in range(8):
            names = ["word-%d-%d" % (batch, i) for i in range(5)]
            s = self.add(nodes=[mk_node("term:" + n, n.replace("-", " ")) for n in names],
                         edges=[mk_edge("term:" + n, "about", "process:harvesting") for n in names])
            scores.append(s["score"])
        self.assertEqual(scores, sorted(set(scores)), scores)  # strictly rising, batch after batch
        self.assertGreaterEqual(scores[-1] - scores[0], 20)
        self.assertEqual(s["band"], "deep")  # 40 more terms: the evidence sum is 58, nearly three times the targets
        # corroborating what is there counts too
        rows, _p = store.read_jsonl(self.repo.path("graph/nodes.jsonl"))
        again = self.add(replace=[dict(r, prov=prov(SRC, SRC2)) for r in rows])
        self.assertGreater(again["score"], scores[-1])
        self.assertLessEqual(again["score"], 100)

    def test_records_without_a_source_add_no_depth(self):
        before = richness.summary(load(self.repo))
        names = ("loam", "silt", "clay", "humus", "marl")
        after = self.add(nodes=[mk_node("term:" + n, n, prov=[]) for n in names],
                         edges=[mk_edge("term:" + n, "about", "process:watering", prov=[]) for n in names])
        self.assertEqual(after["depth"], before["depth"])
        self.assertLess(after["score"], before["score"])  # evidence fell and nothing was added to depth


class DepthTest(_support.TempCase):
    def test_depth_rules(self):
        self.assertIsNone(richness._depth(5, 0))
        self.assertEqual(richness._depth(0, 20), 0.0)
        self.assertEqual(richness._depth(20, 20), 0.5)
        self.assertEqual(richness._depth(40, 20), 0.75)

    def test_score_scales_the_quality_parts_by_coverage_and_depth(self):
        full = {"coverage": 1.0, "completeness": 0.5, "connectivity": 0.5, "evidence": 0.5, "confirmation": 0.5}
        self.assertEqual(richness.score(dict(full, depth=0.5)), (48, "working"))  # 30 + 0.5 x 35 = 47.5, half up
        self.assertEqual(richness.score(dict(full, coverage=0.5, depth=0.5)), (24, "sketch"))  # 15 + 0.25 x 35
        self.assertEqual(richness.score(dict(full, depth=None)), (65, "rich"))  # a null depth scales nothing
        self.assertEqual(richness.score(dict(full, depth=0.0), {"coverage": 0}), (50, "working"))
        with mock.patch.object(richness, "SCALE_BY_COVERAGE", False):
            self.assertEqual(richness.score(dict(full, depth=0.0)), (65, "rich"))

    def closed_log(self, repo, dims):
        questions = [{"id": "q.dp.%s" % d, "ask": "About %s?" % d, "dimension": d, "stage": 1, "priority": 10}
                     for d in dims]
        write_rows(repo, "packs/local.questions.jsonl", questions)
        store.write_jsonl(repo.path("interview/log.jsonl"),
                          [{"id": "ans-20260928-0000%02d" % i, "q": "q.dp.%s" % d, "at": "2026-09-28T12:00:00Z",
                            "status": "na", "src": None, "proposal": None, "node": None}
                           for i, d in enumerate(dims)], key=None)
        clear()

    def test_closed_dimensions_leave_the_target_sum(self):
        repo = base_topic(self.tmp)
        self.closed_log(repo, ["vocabulary"])
        m = richness.measures(load(repo))
        self.assertEqual(m["denominators"]["depth"], 15)  # 20 less the vocabulary target
        self.assertEqual(m["values"]["depth"], round(1 - 2 ** (-4 / 15.0), 4))

    def test_no_target_means_no_depth(self):
        repo = base_topic(self.tmp)
        self.closed_log(repo, sorted(load(repo).registry.dimensions()))
        m = richness.measures(load(repo))
        self.assertIsNone(m["values"]["depth"])
        self.assertEqual(m["missing"]["depth"], "no dimension target to scale it by")
        self.assertIsNone(m["denominators"]["depth"])
        s = richness.summary(load(repo))
        self.assertFalse(s["depth"]["scales"])
        self.assertNotIn("\ndepth ", "\n".join(richness._render_summary(s, "compact")))


class MutatePointTest(_support.TempCase):
    def test_apply_appends_a_measured_point(self):
        repo = base_topic(self.tmp)
        op = {"n": 1, "op": "add_node", "node": {"id": "term:compost", "kind": "term", "name": "Compost",
                                                  "summary": "Rotted plant matter."},
              "status": "confirmed", "trust": "reviewed", "conf": 0.7, "prov": prov(SRC)}
        mutate.apply_ops(repo, [op], by="user", change_type="apply", summary="add compost")
        last = history.read(repo)[-1]
        self.assertEqual(last["kind"], "apply")
        self.assertIsInstance(last["values"]["richness"], int)
        self.assertNotIn("richness", last["missing"])
        for key in ("nodes_by_kind", "edges_by_rel", "bridges", "dimensions", "sources", "pending",
                    "open_questions"):
            self.assertIn(key, last["breakdowns"])
        self.assertEqual(last["breakdowns"]["nodes_by_kind"]["term"], 2)


class TopicGapsTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        self.repo = base_topic(self.tmp)

    def types(self, gaps):
        return [g["type"] for g in gaps]

    def test_uncited_source_skips_cited_in_flight_erased_and_superseded(self):
        add_source(self.repo, SRC3, "in flight\n")
        add_source(self.repo, SRC4, "[erased by dec-20260928-x-0000]\n", erased=True)
        add_source(self.repo, SRC5, "old text\n", title="Rota")
        add_source(self.repo, SRC6, "new text\n", title="Rota", supersedes=SRC5)
        pending = {"id": "prop-20260928-abcdef", "status": "pending", "created": "2026-09-28T12:00:00Z",
                   "priority": 10, "source": SRC3, "ops": [{"n": 1, "op": "add_gap", "id": "goal:harvest",
                                                           "gap": {"field": "attrs.x", "note": "later"}}]}
        store.write_json(self.repo.path("proposals/pending/prop-20260928-abcdef.json"), pending)
        gaps = richness.topic_gaps(load(self.repo))
        uncited = [g["node"] for g in gaps if g["type"] == "uncited_source"]
        self.assertEqual(uncited, [SRC6])
        item = [g for g in gaps if g["type"] == "uncited_source"][0]
        self.assertEqual((item["severity"], item["ask"], item["kind"]), (3, "Extract from %s?" % SRC6, "source"))
        self.assertTrue(item["untrusted"])
        backlog = [g for g in gaps if g["type"] == "pending_backlog"]
        self.assertEqual(len(backlog), 1)
        self.assertEqual((backlog[0]["severity"], backlog[0]["count"]), (2, 1))
        self.assertIn("1 open proposal, oldest 0 days", backlog[0]["why"])

    def test_no_backlog_without_open_proposals(self):
        self.assertNotIn("pending_backlog", self.types(richness.topic_gaps(load(self.repo))))

    def test_missing_dimensions_use_stage_done_at(self):
        gaps = [g for g in richness.topic_gaps(load(self.repo)) if g["type"] == "missing_dimension"]
        dims = [g["dimension"] for g in gaps]
        # frame 0.5 and people 0.33 are under 0.6; data and deliverables are done
        self.assertIn("frame", dims)
        self.assertIn("people", dims)
        self.assertNotIn("data", dims)
        self.assertNotIn("deliverables", dims)
        people = [g for g in gaps if g["dimension"] == "people"][0]
        self.assertEqual(people["severity"], 9)
        self.assertIn("people: 1 of 3 substantive nodes", people["why"])
        self.assertTrue(people["ask"])
        manifest = store.read_json(self.repo.path("ontology.json"))
        manifest.setdefault("policy", {})["stage_done_at"] = 0.3
        store.write_json(self.repo.path("ontology.json"), manifest)
        dims = [g["dimension"] for g in richness.topic_gaps(load(store.Repo.open(self.repo.root)))
                if g["type"] == "missing_dimension"]
        self.assertNotIn("frame", dims)
        self.assertNotIn("people", dims)

    def test_duplicate_pairs(self):
        nodes, _p = store.read_jsonl(self.repo.path("graph/nodes.jsonl"))
        nodes.append(mk_node("term:mulch-2", "mulch"))
        write_rows(self.repo, "graph/nodes.jsonl", nodes)
        dup = [g for g in richness.topic_gaps(load(self.repo)) if g["type"] == "duplicate"]
        self.assertEqual([(g["node"], g["other"]) for g in dup], [("term:mulch", "term:mulch-2")])
        self.assertEqual(dup[0]["severity"], 3)
        self.assertIn("term:mulch-2", dup[0]["ask"])

    def test_duplicates_are_listed_only_up_to_a_size_unless_asked(self):
        nodes, _p = store.read_jsonl(self.repo.path("graph/nodes.jsonl"))
        nodes.append(mk_node("term:mulch-2", "mulch"))
        write_rows(self.repo, "graph/nodes.jsonl", nodes)
        onto = load(self.repo)
        with mock.patch.object(richness, "DUPLICATE_MAX_NODES", 3):
            self.assertNotIn("duplicate", self.types(richness.ranked_gaps(onto)))
            self.assertEqual(len(richness.ranked_gaps(onto, type="duplicate")), 1)
            code, out, err = _support.run_cli(["gaps"], self.repo.root)
        self.assertEqual(code, 0, err)
        self.assertIn("note: duplicate pairs are listed only up to 3 nodes here: onto gaps --type duplicate", out)


class ImportGapsTest(_support.TempCase):
    def garden(self):
        nodes = [mk_node("goal:shared-harvest", "Shared harvest"),
                 mk_node("role:bed-steward", "Bed steward"),
                 mk_node("process:watering", "Watering"),
                 mk_node("crop:tomato", "Tomato"),
                 mk_node("plot:north-bed", "North bed")]
        edges = [mk_edge("role:bed-steward", "works_on", "goal:shared-harvest"),
                 mk_edge("process:watering", "serves", "goal:shared-harvest"),
                 mk_edge("crop:tomato", "grown_in", "plot:north-bed")]
        return make_export("garden", nodes, edges, local_pack=GARDEN_PACK)

    def kitchen(self):
        return make_export("kitchen", [mk_node("ingredient:tomato", "Tomato")], local_pack=KITCHEN_PACK)

    def unbridged(self, repo):
        return [(g["ns"], g["kind"]) for g in richness.topic_gaps(load(repo)) if g["type"] == "unbridged_import"]

    def test_unbridged_kinds_near_a_goal_until_bridged(self):
        repo = write_topic(self.tmp, [mk_node("role:cook", "Cook")], [], ns="g2t")
        add_import(repo, "garden", self.garden())
        clear()
        self.assertEqual(self.unbridged(repo), [("garden", "goal"), ("garden", "process"), ("garden", "role")])
        edge = mk_edge("garden/role:bed-steward", "related_to", "role:cook", symmetric=True)
        write_rows(repo, "graph/edges.jsonl", [edge])
        self.assertEqual(self.unbridged(repo), [("garden", "goal"), ("garden", "process")])
        gap = [g for g in richness.topic_gaps(load(repo)) if g["type"] == "unbridged_import"][0]
        self.assertEqual((gap["severity"], gap["stage"], gap["example"]), (6, 8, "garden/goal:shared-harvest"))
        self.assertIn("garden/goal:shared-harvest", gap["ask"])

    def test_without_any_goal_every_imported_kind_counts(self):
        repo = write_topic(self.tmp, [], [], ns="g2t")
        export = self.kitchen()
        add_import(repo, "kitchen", export)
        clear()
        self.assertEqual(self.unbridged(repo), [("kitchen", "kitchen/ingredient")])

    def test_bridges_dimension_over_import_pairs(self):
        repo = write_topic(self.tmp, [mk_node("goal:menu", "Menu")], [], ns="g2t")
        add_import(repo, "garden", self.garden())
        add_import(repo, "kitchen", self.kitchen())
        clear()
        onto = load(repo)
        self.assertEqual(richness.measures(onto)["breakdowns"]["dimensions"]["bridges"], 0.0)
        edges = [mk_edge("garden/crop:tomato", "same_as", "kitchen/ingredient:tomato", symmetric=True),
                 mk_edge("goal:menu", "related_to", "kitchen/ingredient:tomato", symmetric=True)]
        write_rows(repo, "graph/edges.jsonl", edges)
        m = richness.measures(load(repo))
        self.assertEqual(m["breakdowns"]["bridges"], {"garden|kitchen": 1, "kitchen|self": 1})
        self.assertEqual(m["breakdowns"]["dimensions"]["bridges"], 0.5)  # only garden|kitchen is an import pair
        gap = [g for g in richness.topic_gaps(load(repo))
               if g["type"] == "missing_dimension" and g["dimension"] == "bridges"][0]
        self.assertEqual((gap["severity"], gap["stage"]), (9, 8))
        self.assertIn("garden|kitchen 1 of 2", gap["why"])

    def test_one_direct_import_pairs_with_this_topic_and_bundles_join_their_parent(self):
        repo = write_topic(self.tmp, [mk_node("role:cook", "Cook")], [], ns="market")
        add_import(repo, "g2t", make_export("g2t", [mk_node("goal:menu", "Menu")]))
        add_import(repo, "garden", self.garden(), via="g2t")
        clear()
        edges = [mk_edge("garden/role:bed-steward", "related_to", "role:cook", symmetric=True)]
        write_rows(repo, "graph/edges.jsonl", edges)
        m = richness.measures(load(repo))
        self.assertEqual(m["breakdowns"]["bridges"], {"garden|self": 1})
        self.assertEqual(m["breakdowns"]["dimensions"]["bridges"], 0.5)  # counted for the pair g2t|self


class RankedGapsTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        self.repo = base_topic(self.tmp)
        self.onto = load(self.repo)

    def test_ranking_and_shape(self):
        gaps = richness.ranked_gaps(self.onto)
        sev = [g["severity"] for g in gaps]
        self.assertEqual(sev, sorted(sev, reverse=True))
        for g in gaps:
            for key in ("type", "severity", "why", "ask", "action"):
                self.assertIn(key, g)
            self.assertFalse([k for k in g if k.startswith("_")])
        first_six = [g for g in gaps if g["severity"] == 6]
        self.assertEqual(first_six[0]["type"], "missing_field")
        self.assertEqual(first_six[0]["node"], "dataset:harvest-log")
        self.assertEqual(first_six[0]["field"], "attrs.format")
        self.assertEqual(first_six[0]["why"], "no attrs.format")
        self.assertTrue(first_six[0]["draft"])
        self.assertEqual(richness.ranked_gaps(self.onto), gaps)

    def test_filters(self):
        orphans = richness.ranked_gaps(self.onto, type="orphan")
        self.assertEqual([g["node"] for g in orphans], ["term:mulch"])
        mine = richness.ranked_gaps(self.onto, node="dataset:harvest-log")
        self.assertTrue(mine)
        self.assertTrue(all(g["node"] == "dataset:harvest-log" for g in mine))
        by_kind = richness.ranked_gaps(self.onto, kind="term")
        self.assertTrue(all(g.get("kind") == "term" or g["type"] == "missing_dimension" for g in by_kind))
        self.assertIn("vocabulary", [g.get("dimension") for g in by_kind])
        self.assertEqual(richness.ranked_gaps(self.onto, type="pending_backlog"), [])


class SuggestTest(_support.TempCase):
    def test_lines_that_name_the_node_and_the_field(self):
        repo = base_topic(self.tmp)
        add_source(repo, SRC3, "The Harvest Log sits in the shed.\nThe harvest log format is a paper binder.\n"
                               "Nothing else here.\n")
        onto = load(repo)
        gaps = richness.ranked_gaps(onto, node="dataset:harvest-log")
        found = richness.suggest_sources(onto, gaps)
        by_type = {}
        for s in found:
            by_type.setdefault(s["type"], []).append(s)
        field = by_type["missing_field"][0]
        self.assertEqual((field["src"], field["loc"], field["matched"]), (SRC3, "L2-L2", "name and format"))
        self.assertEqual(field["gap"], "missing_field dataset:harvest-log attrs.format")
        self.assertEqual([(s["src"], s["loc"]) for s in by_type["draft"]], [(SRC3, "L1-L1")])
        self.assertNotIn("text", field)

    def test_single_source_skips_cited_sources_and_uncited_points_at_a_chunk(self):
        repo = base_topic(self.tmp)
        add_source(repo, SRC3, "Steward notes.\n\nThe steward waters daily.\n")
        onto = load(repo)
        gaps = richness.ranked_gaps(onto, node="role:steward", type="single_source")
        found = richness.suggest_sources(onto, gaps)
        self.assertEqual([(s["src"], s["loc"]) for s in found], [(SRC3, "L1-L1")])
        uncited = richness.ranked_gaps(onto, type="uncited_source")
        self.assertEqual([g["node"] for g in uncited], [SRC3])
        found = richness.suggest_sources(onto, uncited)
        self.assertEqual([(s["src"], s["loc"]) for s in found], [(SRC3, "L1-L3")])

    def test_cli_suggestions_carry_the_read_call(self):
        repo = base_topic(self.tmp)
        add_source(repo, SRC3, "The harvest log format is a paper binder.\n")
        code, out, err = _support.run_cli(["gaps", "--node", "harvest-log", "--suggest-sources", "--json"],
                                          repo.root)
        self.assertEqual(code, 0, err)
        obj = json.loads(out)
        self.assertEqual(obj["resolved"]["id"], "dataset:harvest-log")
        calls = {s["call"] for s in obj["suggestions"]}
        self.assertIn("onto get %s --lines 1-1" % SRC3, calls)
        code, out, err = _support.run_cli(["gaps", "--node", "harvest-log", "--suggest-sources"], repo.root)
        self.assertIn("   from: %s L1-L1 (name and format)" % SRC3, out)


class CalibrationTest(_support.TempCase):
    def proposal(self, pid, ops, verdicts, status="applied"):
        prop = {"id": pid, "status": status, "created": "2026-09-28T12:00:00Z", "priority": 0, "source": None,
                "ops": ops, "review": {"by": "user", "at": "2026-09-28T12:00:00Z", "verdicts": verdicts}
                if verdicts is not None else None}
        folder = "done" if status in ("applied", "rejected", "superseded") else "pending"
        store.write_json(self.repo.path("proposals/%s/%s.json" % (folder, pid)), prop)

    def setUp(self):
        super().setUp()
        self.repo = base_topic(self.tmp)
        os.makedirs(self.repo.path("proposals/done"), exist_ok=True)
        os.makedirs(self.repo.path("proposals/pending"), exist_ok=True)
        ops = [
            {"n": 1, "op": "add_node", "node": {"kind": "role", "name": "A"}, "conf": 0.9},
            {"n": 2, "op": "add_node", "node": {"kind": "crop", "name": "B"}, "conf": 0.6},
            {"n": 3, "op": "add_edge", "edge": {"src": "role:a", "rel": "owns", "dst": "dataset:b"}},
            {"n": 4, "op": "update_node", "id": "role:x", "set": {"summary": "y"}, "conf": 0.8},
            {"n": 5, "op": "archive", "id": "role:y", "archived": {"reason": "x" * 20, "decision": None,
                                                                    "superseded_by": []}},
        ]
        self.proposal("prop-20260928-aaaaaa", ops, {"1": "accept", "2": "reject", "3": "draft", "4": "edit",
                                                    "5": "accept"})
        self.proposal("prop-20260928-bbbbbb", ops[:1], None, status="pending")

    def test_brier_on_known_verdicts(self):
        cal = richness.calibration(self.repo)
        # (0.9-1)^2 + (0.6-0)^2 + (0.7-0.5)^2 + (0.8-0.5)^2 = 0.01 + 0.36 + 0.04 + 0.09 = 0.5 over 4 ops
        self.assertEqual((cal["n"], cal["brier"]), (4, 0.125))
        self.assertEqual(cal["by_kind"]["role"]["brier"], 0.05)
        self.assertEqual(cal["by_kind"]["crop"]["brier"], 0.36)
        self.assertEqual(cal["by_kind"]["edge"]["brier"], 0.04)
        self.assertEqual(cal["verdicts"], {"accept": 1, "edit": 1, "draft": 1, "reject": 1})
        self.assertEqual(richness.measures(load(self.repo))["values"]["brier"], 0.125)

    def test_calibration_section_and_cache_refresh(self):
        code, out, err = _support.run_cli(["gaps", "--section", "calibration"], self.repo.root)
        self.assertEqual(code, 0, err)
        self.assertIn("calibration: brier 0.1250 over 4 reviewed ops", out)
        self.proposal("prop-20260928-cccccc", [{"n": 1, "op": "add_node", "node": {"kind": "role", "name": "C"},
                                                "conf": 1.0}], {"1": "accept"})
        self.assertEqual(richness.calibration(self.repo)["n"], 5)
        self.assertEqual(richness.calibration(self.repo)["brier"], 0.1)


class CommandTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        self.repo = base_topic(self.tmp)

    def cli(self, *args):
        return _support.run_cli(list(args), self.repo.root)

    def test_gaps_json_and_paging(self):
        code, out, err = self.cli("gaps", "--json", "--limit", "3")
        self.assertEqual(code, 0, err)
        obj = json.loads(out)
        self.assertEqual(obj["section"], "gaps")
        self.assertEqual(len(obj["gaps"]), 3)
        self.assertGreater(obj["totals"]["gaps"], 3)
        self.assertEqual(obj["summary"]["score"], 13)
        self.assertEqual(obj["version"]["richness"]["score"], 13)
        code2, out2, _err = self.cli("gaps", "--json", "--limit", "3")
        self.assertEqual(out, out2)
        code, out, err = self.cli("gaps", "--limit", "3")
        self.assertIn("[page] gaps 1-3 of", out)
        self.assertIn("-> ask \"", out)

    def test_richness_alias_gives_the_summary(self):
        code, out, err = self.cli("richness")
        self.assertEqual(code, 0, err)
        self.assertIn("richness 13 seed: coverage 0.35", out)
        self.assertIn("\ndepth 0.13: evidence sum 4.00 of target sum 20 (it scales the other parts with coverage)\n",
                      out)
        self.assertIn(richness.HEURISTIC, out)
        code, out, err = self.cli("richness", "--text")
        self.assertIn("coverage counts in full", out)
        self.assertIn("in proportion to coverage x depth", out)
        self.assertIn(richness.DEPTH_RULE, out)
        self.assertIn("denominators:", out)
        self.assertIn("depth 20", out)

    def test_bad_filters_are_usage_errors(self):
        self.assertEqual(self.cli("gaps", "--kind", "nonsense")[0], 2)
        self.assertEqual(self.cli("gaps", "--type", "nonsense")[0], 2)
        self.assertEqual(self.cli("gaps", "--node", "no-such-thing-at-all")[0], 1)

    def test_kind_filter_accepts_plurals(self):
        code, out, err = self.cli("gaps", "--kind", "datasets", "--json")
        self.assertEqual(code, 0, err)
        obj = json.loads(out)
        self.assertEqual(obj["filters"], {"kind": "dataset"})
        self.assertTrue(all(g.get("kind") == "dataset" or g["type"] == "missing_dimension" for g in obj["gaps"]))

    def test_history_section(self):
        store.write_jsonl(self.repo.path("metrics/history.jsonl"),
                          [history.new_point("apply", {"richness": 10}, at="2026-09-20T12:00:00Z")], key=None)
        code, out, err = self.cli("gaps", "--section", "history", "--json")
        self.assertEqual(code, 0, err)
        obj = json.loads(out)
        self.assertEqual(obj["history"][0]["richness"], 10)
        self.assertEqual(obj["totals"]["history"], 1)
        code, out, err = self.cli("gaps", "--section", "history")
        self.assertIn("2026-09-20 12:00 apply richness 10", out)
        self.assertIn("conf n/a, depth n/a", out)  # a point written before depth was measured
        mutate.apply_ops(self.repo, [{"n": 1, "op": "add_node", "status": "confirmed", "trust": "reviewed",
                                      "node": {"id": "term:tilth", "kind": "term", "name": "Tilth",
                                               "summary": "The crumbly state of well worked soil."},
                                      "conf": 0.7, "prov": prov(SRC)}], by="user", change_type="apply", summary="t")
        obj = json.loads(self.cli("gaps", "--section", "history", "--json")[1])
        self.assertIsInstance(obj["history"][-1]["depth"], float)

    def test_validate_prints_the_score(self):
        code, out, err = self.cli("validate")
        self.assertIn("richness 13", out)


class ToolsCheckTest(_support.TempCase):
    def test_cli_tools_on_path_and_others_unchecked(self):
        nodes = [
            mk_node("tool:probe", "Probe", attrs={"interface": "cli", "invoke": "garden-probe --all"}),
            mk_node("tool:ghost", "Ghost", attrs={"interface": "cli", "invoke": "no-such-tool-zz9 run"}),
            mk_node("tool:feed", "Feed", attrs={"interface": "http", "invoke": "https://example.invalid/feed"}),
            mk_node("tool:odd", "Odd", attrs={"interface": "cli", "invoke": "/bin/sh -c true"}),
            mk_node("tool:bare", "Bare", status="proposed", trust="untrusted"),
        ]
        repo = write_topic(self.tmp, nodes, [], ns="tools")
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        script = os.path.join(bindir, "garden-probe")
        with open(script, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR)
        before = _support.snapshot(repo.root, skip=[".onto"])
        with mock.patch.dict(os.environ, {"PATH": bindir}):
            code, out, err = _support.run_cli(["tools", "check", "--json"], repo.root)
        self.assertEqual(code, 0, err)
        obj = json.loads(out)
        status = {t["id"]: t["status"] for t in obj["tools"]}
        self.assertEqual(status, {"tool:bare": "unchecked", "tool:feed": "unchecked", "tool:ghost": "missing",
                                  "tool:odd": "unchecked", "tool:probe": "ok"})
        self.assertEqual(obj["counts"], {"ok": 1, "missing": 1, "unchecked": 3})
        bare = [t for t in obj["tools"] if t["id"] == "tool:bare"][0]
        self.assertTrue(bare["untrusted"] and bare["draft"])
        self.assertEqual(before, _support.snapshot(repo.root, skip=[".onto"]))
        report = store.read_json(repo.path(".onto/tools-check.json"))
        self.assertEqual([t["id"] for t in report["tools"]], sorted(status))
        with mock.patch.dict(os.environ, {"PATH": bindir}):
            code, out, err = _support.run_cli(["tools", "check"], repo.root)
        self.assertIn("tools: 5 checked: 1 ok, 1 missing, 3 unchecked", out)
        self.assertIn("[untrusted] tool:bare (draft) - unchecked (no interface set)", out)


class DuplicateTotalsTest(_support.TempCase):
    """Every duplicate pair is listed (no silent cap), so totals, paging and the node filter are true."""

    def setUp(self):
        super().setUp()
        # letter suffixes: names that differ only in their numbers are not duplicates
        nodes = [mk_node("term:bed-%03d" % i, "Raised bed %s" % (chr(97 + i // 26) + chr(97 + i % 26)))
                 for i in range(120)]
        self.repo = write_topic(self.tmp, nodes, [], ns="beds")
        self.pairs = entities.duplicates(load(self.repo), limit=0)
        self.assertGreater(len(self.pairs), 50)

    def test_the_full_list_with_true_totals(self):
        dup = richness.ranked_gaps(load(self.repo), type="duplicate")
        self.assertEqual(len(dup), len(self.pairs))
        code, out, err = _support.run_cli(["gaps", "--type", "duplicate", "--limit", "5", "--json"], self.repo.root)
        self.assertEqual(code, 0, err)
        obj = json.loads(out)
        self.assertEqual(obj["totals"]["gaps"], len(self.pairs))
        self.assertEqual(len(obj["gaps"]), 5)
        code, out, err = _support.run_cli(["gaps", "--type", "duplicate", "--limit", "5", "--offset", "50"],
                                          self.repo.root)
        self.assertEqual(code, 0, err)
        self.assertIn("%d gaps" % len(self.pairs), out)
        self.assertIn("[page] gaps 51-55 of %d" % len(self.pairs), out)
        self.assertNotIn("no gaps", out)

    def test_the_node_filter_finds_every_pair_of_the_node(self):
        mine = sorted((p["a"], p["b"]) for p in self.pairs if "term:bed-119" in (p["a"], p["b"]))
        self.assertTrue(mine)
        code, out, err = _support.run_cli(["gaps", "--node", "term:bed-119", "--type", "duplicate", "--limit", "0",
                                           "--json"], self.repo.root)
        self.assertEqual(code, 0, err)
        found = sorted((g["node"], g["other"]) for g in json.loads(out)["gaps"])
        self.assertEqual(found, mine)

    def test_the_node_filter_lists_pairs_above_the_size_limit(self):
        onto = load(self.repo)
        mine = [p for p in self.pairs if "term:bed-119" in (p["a"], p["b"])]
        with mock.patch.object(richness, "DUPLICATE_MAX_NODES", 10):
            self.assertNotIn("duplicate", [g["type"] for g in richness.ranked_gaps(onto)])
            found = [g for g in richness.ranked_gaps(onto, node="term:bed-119") if g["type"] == "duplicate"]
        self.assertEqual(len(found), len(mine))


class UnlinkedImportTest(_support.TempCase):
    """An import linked to no goal yet still reads as unbridged when this topic has a goal of its own."""

    def kitchen(self):
        nodes = [mk_node("ingredient:tomato", "Tomato"), mk_node("process:prep", "Prep"),
                 mk_node("deliverable:menu", "Menu")]
        edges = [mk_edge("process:prep", "produces", "deliverable:menu")]
        return make_export("kitchen", nodes, edges, local_pack=KITCHEN_PACK)

    def unbridged(self, repo):
        return [(g["ns"], g["kind"]) for g in richness.topic_gaps(load(repo)) if g["type"] == "unbridged_import"]

    def test_a_local_goal_does_not_hide_an_unlinked_import(self):
        repo = write_topic(self.tmp, [mk_node("role:cook", "Cook")], [], ns="g2t")
        add_import(repo, "kitchen", self.kitchen())
        clear()
        without_goal = self.unbridged(repo)
        self.assertEqual(without_goal, [("kitchen", "deliverable"), ("kitchen", "kitchen/ingredient"),
                                        ("kitchen", "process")])
        write_rows(repo, "graph/nodes.jsonl", [mk_node("role:cook", "Cook"), mk_node("goal:weekly-menu", "Menu")])
        write_rows(repo, "graph/edges.jsonl", [mk_edge("role:cook", "works_on", "goal:weekly-menu")])
        self.assertEqual(self.unbridged(repo), without_goal)
        gap = [g for g in richness.topic_gaps(load(repo)) if g["type"] == "unbridged_import"][0]
        self.assertIn("no kitchen node is within 2 links of a goal yet", gap["why"])
        # a bridge links the import to the goal: the bridged kind goes, and only the kinds near the goal stay
        edges = [mk_edge("role:cook", "works_on", "goal:weekly-menu"),
                 mk_edge("kitchen/deliverable:menu", "serves", "goal:weekly-menu")]
        write_rows(repo, "graph/edges.jsonl", edges)
        self.assertEqual(self.unbridged(repo), [("kitchen", "process")])

    def test_why_counts_only_the_nodes_near_a_goal(self):
        nodes = [mk_node("goal:harvest", "Harvest"), mk_node("role:a", "A"), mk_node("role:b", "B"),
                 mk_node("role:c", "C")]
        garden = make_export("garden", nodes, [mk_edge("role:a", "works_on", "goal:harvest")])
        repo = write_topic(self.tmp, [], [], ns="g2t")
        add_import(repo, "garden", garden)
        clear()
        gap = [g for g in richness.topic_gaps(load(repo))
               if g["type"] == "unbridged_import" and g["kind"] == "role"][0]
        self.assertEqual(gap["why"], "garden has 1 of 3 roles near a goal, none bridged")
        self.assertEqual(gap["example"], "garden/role:a")


class MissingDimensionCallTest(_support.TempCase):
    def test_the_stage_is_the_questions_own_stage(self):
        root = _support.init_topic(self.tmp, "fresh")
        onto = load(store.Repo.open(root))
        gaps = {g["dimension"]: g for g in richness.topic_gaps(onto) if g["type"] == "missing_dimension"}
        deliverables = gaps["deliverables"]
        asked = {q["id"]: q for q in onto.registry.questions()}[deliverables["question"]]
        self.assertEqual(deliverables["stage"], asked["stage"])
        code, out, err = _support.run_cli(["gaps", "--type", "missing_dimension", "--json"], root)
        self.assertEqual(code, 0, err)
        items = {g["dimension"]: g for g in json.loads(out)["gaps"]}
        self.assertEqual(items["deliverables"]["call"], "onto next --stage %d" % asked["stage"])
        code, out, err = _support.run_cli(["next", "--stage", str(asked["stage"]), "--n", "20"], root)
        if code != 3:  # the interview module is built: the named call offers the question the gap printed
            self.assertEqual(code, 0, err)
            self.assertIn(deliverables["question"], out)

    def test_the_fallback_bridges_ask_names_this_topic_not_self(self):
        repo = write_topic(self.tmp, [mk_node("role:vendor", "Vendor")], [], ns="market")
        add_import(repo, "kitchen", make_export("kitchen", [mk_node("ingredient:tomato", "Tomato")],
                                                local_pack=KITCHEN_PACK))
        log = [{"id": "ans-20260928-000001", "q": richness.COMPOSE_QUESTION, "at": "2026-09-28T12:00:00Z",
                "status": "answered", "src": None, "proposal": None, "node": None}]
        store.write_jsonl(repo.path("interview/log.jsonl"), log, key=None)
        clear()
        onto = load(repo)
        gap = [g for g in richness.topic_gaps(onto)
               if g["type"] == "missing_dimension" and g["dimension"] == "bridges"][0]
        self.assertEqual(gap["ask"], "Which outputs of kitchen feed which processes of %s?" % onto.title())
        self.assertIn("kitchen|market 0 of 2", gap["why"])
        self.assertNotIn("self", gap["ask"] + gap["why"])


class HistoryPagingTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        self.repo = base_topic(self.tmp)
        points = [history.new_point("apply", {"richness": n}, at="2026-09-%02dT12:00:00Z" % (n + 1))
                  for n in range(6)]
        store.write_jsonl(self.repo.path("metrics/history.jsonl"), points, key=None)

    def cli(self, *args):
        code, out, err = _support.run_cli(["gaps", "--section", "history"] + list(args), self.repo.root)
        self.assertEqual(code, 0, err)
        return out

    def test_offset_reaches_older_points(self):
        newest = json.loads(self.cli("--limit", "2", "--json"))
        self.assertEqual([p["richness"] for p in newest["history"]], [4, 5])
        self.assertEqual(newest["paging"]["next_offset"], 2)
        older = json.loads(self.cli("--limit", "2", "--offset", "2", "--json"))
        self.assertEqual([p["richness"] for p in older["history"]], [2, 3])
        self.assertEqual(older["totals"]["history"], 6)
        oldest = json.loads(self.cli("--limit", "2", "--offset", "4", "--json"))
        self.assertEqual([p["richness"] for p in oldest["history"]], [0, 1])
        self.assertFalse(oldest["paging"]["more"])
        every = json.loads(self.cli("--limit", "0", "--json"))
        self.assertEqual([p["richness"] for p in every["history"]], [0, 1, 2, 3, 4, 5])

    def test_the_page_line_names_the_next_call(self):
        out = self.cli("--limit", "2", "--offset", "2")
        self.assertIn("history: 2 of 6 points (before the newest 2), oldest first", out)
        self.assertIn("[page] history 3-4 of 6, counted from the newest; next: onto gaps --section history "
                      "--limit 2 --offset 4", out)
        self.assertNotIn("[page]", self.cli("--limit", "2", "--offset", "4"))


class ConfDefaultTest(_support.TempCase):
    def test_update_ops_without_conf_forecast_the_default(self):
        self.assertEqual(richness.conf_ops(), {"add_node", "add_edge", "update_node", "update_edge", "merge"})
        repo = base_topic(self.tmp)
        ops = [{"n": 1, "op": "update_node", "id": "role:steward", "set": {"summary": "Waters the beds."}},
               {"n": 2, "op": "update_edge", "id": "e:000000000000", "set": {"note": "weekly"}},
               {"n": 3, "op": "add_gap", "id": "role:steward", "gap": {"field": "attrs.x", "note": "later"}}]
        prop = {"id": "prop-20260928-dddddd", "status": "applied", "created": "2026-09-28T12:00:00Z", "priority": 0,
                "source": None, "ops": ops,
                "review": {"by": "user", "at": "2026-09-28T12:00:00Z",
                           "verdicts": {"1": "accept", "2": "reject", "3": "accept"}}}
        os.makedirs(repo.path("proposals/done"), exist_ok=True)
        store.write_json(repo.path("proposals/done/prop-20260928-dddddd.json"), prop)
        cal = richness.calibration(repo)
        # (0.7 - 1)^2 + (0.7 - 0)^2 = 0.09 + 0.49 = 0.58 over 2 ops; add_gap carries no confidence
        self.assertEqual((cal["n"], cal["brier"]), (2, 0.29))
        self.assertEqual((cal["by_kind"]["role"]["n"], cal["by_kind"]["edge"]["n"]), (1, 1))


class InFlightPointTest(_support.TempCase):
    """A point measured with the proposal being applied counts it as saved: not pending, its verdicts scored."""

    def test_in_flight_proposal_is_not_pending_and_its_verdicts_count(self):
        repo = base_topic(self.tmp)
        ops = [{"n": 1, "op": "add_node", "node": {"kind": "term", "name": "Compost"}, "conf": 0.9}]
        prop = {"id": "prop-20260928-eeeeee", "status": "pending", "created": "2026-09-28T12:00:00Z",
                "priority": 5, "source": None, "ops": ops, "review": None}
        os.makedirs(repo.path("proposals/pending"), exist_ok=True)
        store.write_json(repo.path("proposals/pending/prop-20260928-eeeeee.json"), prop)
        onto = load(repo)
        before = richness.point(onto, "answer")
        self.assertEqual(before["breakdowns"]["pending"], 1)
        self.assertIsNone(before["values"]["brier"])
        final = dict(prop, review={"by": "user", "at": "2026-09-28T12:00:00Z", "verdicts": {"1": "accept"}})
        after = richness.point(onto, "answer", in_flight=final)
        self.assertEqual(after["breakdowns"]["pending"], 0)
        self.assertEqual((after["values"]["brier"], after["denominators"]["brier"]), (0.01, 1))
        self.assertEqual(records.check(after, "point"), [])
        # the stored copy did not change, so a call without in_flight still reads it as pending
        self.assertEqual(richness.point(onto, "answer")["breakdowns"]["pending"], 1)


class BridgeEndGapsTest(_support.TempCase):
    """A local bridge between two imports (the usual stage C case) shows its conflict and dangling_bridge gaps in
    ``gaps`` as it does in ``next`` and ``brief``: the imported ends are not local nodes, so ``needs.all_needs``
    alone misses them."""

    def pollen(self, hive_status="confirmed", local=()):
        hive = mk_node("dataset:hive-log", "Hive log", status=hive_status,
                       attrs={"format": "paper", "location": "shed"},
                       archived=archive_block() if hive_status == "archived" else None)
        bees = make_export("bees", [hive])
        orchard = make_export("orchard", [
            mk_node("dataset:bloom-log", "Bloom log", attrs={"format": "spreadsheet", "location": "office"}),
            mk_node("process:bloom-watch", "Bloom watch"),
            mk_node("process:pruning", "Pruning")],
            [mk_edge("process:bloom-watch", "consumes", "dataset:bloom-log")])
        edges = [mk_edge("bees/dataset:hive-log", "same_as", "orchard/dataset:bloom-log", symmetric=True),
                 mk_edge("orchard/process:bloom-watch", "consumes", "bees/dataset:hive-log"),
                 mk_edge("orchard/process:pruning", "consumes", "bees/dataset:hive-log", background=True)]
        repo = write_topic(self.tmp, [mk_node("goal:pollen", "Pollen")] + list(local[:1]), edges + list(local[1:]),
                           ns="pollen")
        add_import(repo, "bees", bees)
        add_import(repo, "orchard", orchard)
        clear()
        return repo

    def keys(self, gaps, gap_type):
        return sorted((g["node"], g.get("field") or g.get("rel")) for g in gaps if g["type"] == gap_type)

    def test_an_import_to_import_same_as_conflict_is_listed_once_per_class(self):
        repo = self.pollen()
        onto = load(repo)
        want = [("bees/dataset:hive-log", "attrs.format"), ("bees/dataset:hive-log", "attrs.location")]
        self.assertEqual(self.keys(richness.ranked_gaps(onto), "conflict"), want)
        self.assertEqual(self.keys(richness.ranked_gaps(onto, type="conflict"), "conflict"), want)
        self.assertEqual(self.keys(richness.ranked_gaps(onto, kind="dataset"), "conflict"), want)
        first = richness.ranked_gaps(onto)[0]  # severity 8 comes right after the missing dimensions
        self.assertEqual([g["type"] for g in richness.ranked_gaps(onto) if g["severity"] == 8], ["conflict"] * 2)
        self.assertEqual(first["type"], "missing_dimension")
        # the imported ends have no other gap here: the imported side is read-only
        listed = {g["node"] for g in richness.ranked_gaps(onto) if g.get("node")}
        self.assertNotIn("orchard/process:pruning", listed)  # a background edge is no bridge
        self.assertEqual(listed - {"goal:pollen"}, {"bees/dataset:hive-log"})
        code, out, err = _support.run_cli(["gaps", "--type", "conflict", "--json"], repo.root)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.keys(json.loads(out)["gaps"], "conflict"), want)
        code, out, err = _support.run_cli(["next", "--n", "40"], repo.root)
        if code != 3:  # the interview asks the very gaps listed
            self.assertEqual(code, 0, err)
            self.assertIn("q.gap.conflict.format@bees/dataset:hive-log", out)
            self.assertIn("q.gap.conflict.location@bees/dataset:hive-log", out)

    def test_a_local_member_of_the_class_keeps_the_conflict(self):
        local = (mk_node("dataset:pollen-log", "Pollen log", attrs={"format": "cards"}),
                 mk_edge("dataset:pollen-log", "same_as", "bees/dataset:hive-log", symmetric=True))
        onto = load(self.pollen(local=local))
        # the class disagrees on format and location; the local member lists both, the imported ends neither
        self.assertEqual(self.keys(richness.ranked_gaps(onto, type="conflict"), "conflict"),
                         [("dataset:pollen-log", "attrs.format"), ("dataset:pollen-log", "attrs.location")])

    def test_dangling_bridges_between_imports_are_listed(self):
        repo = self.pollen(hive_status="archived")
        onto = load(repo)
        want = [("orchard/dataset:bloom-log", "same_as"), ("orchard/process:bloom-watch", "consumes")]
        self.assertEqual(self.keys(richness.ranked_gaps(onto), "dangling_bridge"), want)
        self.assertEqual(self.keys(richness.ranked_gaps(onto, type="dangling_bridge"), "dangling_bridge"), want)
        gap = richness.ranked_gaps(onto, type="dangling_bridge")[0]
        self.assertEqual((gap["severity"], gap["note"]), (5, "bees/dataset:hive-log archived upstream"))
        self.assertEqual(richness.ranked_gaps(onto, node="orchard/process:bloom-watch", type="dangling_bridge"),
                         [g for g in richness.ranked_gaps(onto, type="dangling_bridge")
                          if g["node"] == "orchard/process:bloom-watch"])
        code, out, err = _support.run_cli(["next", "--n", "40"], repo.root)
        if code != 3:
            self.assertEqual(code, 0, err)
            self.assertIn("q.gap.dangling_bridge@orchard/process:bloom-watch", out)
            self.assertIn("q.gap.dangling_bridge@orchard/dataset:bloom-log", out)


def parse_call(call):
    """``(tool, arguments)`` of a follow-up call as an MCP reply prints it (``onto_get id=X chunk=1``)."""
    words = shlex.split(call)
    args = {}
    for word in words[1:]:
        key, _sep, value = word.partition("=")
        args[key] = int(value) if value.isdigit() else {"true": True, "false": False}.get(value, value)
    return words[0], args


class ProfileCallTest(_support.TempCase):
    """Every call ``gaps`` prints works in the caller's MCP profile. The query profile (a reader outside the topic
    repo) has no ``onto_next`` and no ``onto_review``, so its stage questions and backlog read "ask the user"."""

    def setUp(self):
        super().setUp()
        self.repo = base_topic(self.tmp)
        add_source(self.repo, SRC3, "an uncited note\n")
        add_import(self.repo, "kitchen", make_export("kitchen", [mk_node("ingredient:tomato", "Tomato")],
                                                     local_pack=KITCHEN_PACK))
        pending = {"id": "prop-20260928-abcdef", "status": "pending", "created": "2026-09-28T12:00:00Z",
                   "priority": 10, "source": None, "ops": [{"n": 1, "op": "add_gap", "id": "goal:harvest",
                                                            "gap": {"field": "attrs.x", "note": "later"}}]}
        store.write_json(self.repo.path("proposals/pending/prop-20260928-abcdef.json"), pending)
        clear()
        self.outside = os.path.join(self.tmp, "elsewhere")
        os.makedirs(self.outside)

    def gaps(self, profile):
        server = mcp_server.Server(profile, repo=self.repo.root, cwd=self.outside, env={}, err=io.StringIO())
        text = _support.mcp_call(server, "onto_gaps", limit=0, format="text")["content"][0]["text"]
        obj = json.loads(_support.mcp_call(server, "onto_gaps", limit=0, format="json")["content"][0]["text"])
        return server, text, obj

    def check_calls(self, profile):
        server, text, obj = self.gaps(profile)
        tools = {c.tool for c in commands.tools(profile)}
        self.assertEqual({g["type"] for g in obj["gaps"]} >= {"missing_dimension", "unbridged_import",
                                                               "uncited_source", "pending_backlog"}, True)
        named = set(re.findall(r"\bonto_[a-z_]+", text))
        self.assertTrue(named)
        self.assertLessEqual(named, tools, text)
        calls = [g["call"] for g in obj["gaps"] if g.get("call")]
        for call in calls:
            tool, args = parse_call(call)
            self.assertIn(tool, tools, call)
            result = _support.mcp_call(server, tool, **args)  # a protocol error (unknown tool) raises
            self.assertFalse(result["isError"], (call, result))
        return obj, text

    def test_query_profile_calls_exist_in_the_query_profile(self):
        obj, text = self.check_calls("query")
        self.assertNotIn("onto_next", text)
        self.assertNotIn("onto_review", text)
        by_type = {}
        for g in obj["gaps"]:
            by_type.setdefault(g["type"], g)
        self.assertEqual(by_type["missing_dimension"]["action"], richness.READ_ONLY_ACTIONS["missing_dimension"])
        self.assertNotIn("call", by_type["missing_dimension"])
        self.assertEqual(by_type["unbridged_import"]["call"], "onto_brief subject=kitchen/ingredient:tomato")
        self.assertEqual(by_type["pending_backlog"]["action"], richness.READ_ONLY_ACTIONS["pending_backlog"])
        self.assertNotIn("call", by_type["pending_backlog"])
        self.assertEqual(by_type["uncited_source"]["call"], "onto_get id=%s chunk=1" % SRC3)
        self.assertIn(richness.READ_ONLY_NOTE, obj["notes"])
        self.assertIn("note: " + richness.READ_ONLY_NOTE, text)
        self.assertIn("   action: ask the user the question\n", text)

    def test_full_profile_names_the_calls_that_close_the_gaps(self):
        obj, text = self.check_calls("full")
        calls = {g["type"]: g.get("call") for g in reversed(obj["gaps"])}
        self.assertTrue(calls["missing_dimension"].startswith("onto_next stage="))
        self.assertEqual(calls["unbridged_import"], "onto_next stage=%d" % richness.COMPOSE_STAGE)
        self.assertEqual(calls["pending_backlog"], "onto_review")
        self.assertNotIn(richness.READ_ONLY_NOTE, obj.get("notes") or [])
        self.assertIn("(onto_next stage=", text)

    def test_the_cli_keeps_every_call(self):
        code, out, err = _support.run_cli(["gaps", "--limit", "0", "--json"], self.repo.root)
        self.assertEqual(code, 0, err)
        calls = {g["type"]: g.get("call") for g in json.loads(out)["gaps"]}
        self.assertEqual(calls["pending_backlog"], "onto review")
        self.assertEqual(calls["unbridged_import"], "onto next --stage %d" % richness.COMPOSE_STAGE)


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()
