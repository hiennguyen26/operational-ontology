"""needs (every node gap type, the substantive rule, dimension coverage) and render (cuts, calls, markers, literal
printing, paging, the budget engine, card fitting and the version line)."""

from __future__ import annotations

import unittest

from tests import _support
from tests.test_graph import (SRC, add_import, archive_block, clear, garden_export, mk_edge, mk_node, source_row,
                              write_topic)
from ontokit import graph, needs, render, store, util


def gap_types(onto, nid):
    return [g["type"] for g in needs.needs(onto, nid)["gaps"]]


class NeedsTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        other = "src-" + "1" * 12
        two = [{"src": SRC, "loc": "L1-L1", "by": "user"}, {"src": other, "loc": "L1-L1", "by": "user"}]
        self.repo = write_topic(self.tmp, [
            mk_node("topic:t", "Test garden", prov=two),
            mk_node("dataset:log", "Harvest log", attrs={"location": "shed"}, prov=two),
            mk_node("role:keeper", "Keeper", prov=two),
            mk_node("term:lonely", "Lonely", prov=two),
            mk_node("term:blank", "Blank", summary="", prov=two),
            mk_node("process:hub", "Hub", status="proposed", trust="agent", prov=[]),
            mk_node("step:a", "A", prov=two), mk_node("step:b", "B", prov=two), mk_node("step:c", "C", prov=two),
            mk_node("claim:shaky", "Shaky", conf=0.3, prov=two),
            # raised weeks ago: a question raised in the last two days is held back (needs.question_held)
            mk_node("question:who", "Who waters on Sundays?", prov=two, created="2026-09-01"),
            mk_node("term:with-gap", "With gap", gaps=[{"field": "attrs.abbreviation", "note": "not asked yet"}],
                    prov=two),
            mk_node("term:one-source", "One source"),
            mk_node("term:left", "Left", prov=two, attrs={"abbreviation": "L"}),
            mk_node("term:right", "Right", prov=two, attrs={"abbreviation": "R"}),
            mk_node("term:left-twin", "Left twin", prov=two, attrs={"abbreviation": "LT"}),
            mk_node("term:cited-stale", "Cited stale",
                    prov=[{"src": "src-" + "2" * 12, "loc": "L1-L1", "by": "user"}, two[0]]),
            mk_node("process:bridged", "Bridged", prov=two),
            mk_node("goal:done", "Done", status="archived", archived=archive_block(superseded_by=["topic:t"]),
                    prov=two),
        ], [
            mk_edge("role:keeper", "owns", "dataset:log"),
            mk_edge("step:a", "part_of", "process:hub"), mk_edge("step:b", "part_of", "process:hub"),
            mk_edge("step:c", "part_of", "process:hub"),
            mk_edge("claim:shaky", "about", "topic:t"), mk_edge("question:who", "about", "topic:t"),
            mk_edge("term:with-gap", "part_of", "topic:t"), mk_edge("term:one-source", "part_of", "topic:t"),
            mk_edge("term:left", "contradicts", "term:right", symmetric=True),
            mk_edge("term:blank", "part_of", "topic:t"),
            mk_edge("term:cited-stale", "part_of", "topic:t"),
            mk_edge("process:bridged", "consumes", "garden/plot:gone"),
            mk_edge("process:bridged", "related_to", "garden/crop:old-bean", symmetric=True),
            mk_edge("garden/crop:tomato", "same_as", "term:left", symmetric=True),
            mk_edge("term:left", "same_as", "term:left-twin", symmetric=True),
            mk_edge("src-" + "2" * 12, "refresh_with", "tool:gauge"),
            mk_edge("role:keeper", "works_on", "process:hub", background=True),
        ])
        rows, _ = store.read_jsonl(self.repo.path("sources/index.jsonl"))
        rows.append(source_row("src-" + "1" * 12))
        rows.append(source_row("src-" + "2" * 12, stale_after_days=7, captured_at="2026-09-01T00:00:00Z"))
        store.write_jsonl(self.repo.path("sources/index.jsonl"), rows)
        nodes, _ = store.read_jsonl(self.repo.path("graph/nodes.jsonl"))
        store.write_jsonl(self.repo.path("graph/nodes.jsonl"), nodes + [mk_node("tool:gauge", "Gauge", attrs={
            "interface": "manual", "invoke": "look"}, prov=two)])
        add_import(self.repo, "garden", garden_export())
        clear()
        self.onto = graph.Ontology.load(self.repo)

    def test_every_node_gap_type(self):
        cases = {
            "dataset:log": ["missing_field"],
            "goal:done": [],
            "role:keeper": [],
            "term:lonely": ["orphan"],
            "term:blank": ["thin"],
            "process:hub": ["unconfirmed_hub", "missing_relation", "no_provenance", "draft"],
            "claim:shaky": ["low_confidence"],
            "question:who": ["open_question"],
            "term:with-gap": ["open_question"],
            "term:one-source": ["single_source"],
            "term:left": ["contradiction", "conflict"],
            "term:cited-stale": ["stale_source"],
            "process:bridged": ["dangling_bridge", "dangling_bridge", "missing_relation"],
        }
        for nid, expected in cases.items():
            found = gap_types(self.onto, nid)
            for t in expected:
                self.assertIn(t, found, (nid, found))
        self.assertEqual(gap_types(self.onto, "role:keeper"), [])
        self.assertNotIn("orphan", gap_types(self.onto, "topic:t"))  # hubs are never orphans

    def test_gap_fields_and_asks(self):
        gaps = needs.needs(self.onto, "dataset:log")["gaps"]
        self.assertEqual(gaps[0], {"type": "missing_field", "severity": 6, "field": "attrs.format",
                                   "ask": "What is the format of Harvest log?"})
        hub = {g["type"]: g for g in needs.needs(self.onto, "process:hub")["gaps"]}
        self.assertEqual(hub["missing_relation"]["ask"], "Who runs Hub?")
        self.assertEqual((hub["missing_relation"]["rel"], hub["missing_relation"]["dir"]), ("owns", "in"))
        self.assertEqual(hub["unconfirmed_hub"]["severity"], 7)
        orphan = needs.needs(self.onto, "term:lonely")["gaps"][0]
        bank = sorted((q["id"], q["ask"]) for q in self.onto.registry.questions() if q.get("for_gap") == "orphan")
        template = bank[0][1] if bank else needs.DEFAULT_ASKS["orphan"]  # the bank's ask wins (lowest id)
        self.assertEqual(orphan["ask"], template.replace("{name}", "Lonely").replace("{topic}", "Test t"))
        conflict = [g for g in needs.needs(self.onto, "term:left")["gaps"] if g["type"] == "conflict"][0]
        self.assertEqual(conflict["field"], "attrs.abbreviation")
        self.assertEqual(conflict["note"], "L (self) vs LT (self)")
        stale = [g for g in needs.needs(self.onto, "term:cited-stale")["gaps"] if g["type"] == "stale_source"][0]
        self.assertIn("with tool:gauge", stale["ask"])

    def test_gaps_sorted_by_severity(self):
        for nid in self.onto.local_nodes(active_only=True):
            sev = [g["severity"] for g in needs.needs(self.onto, nid)["gaps"]]
            self.assertEqual(sev, sorted(sev, reverse=True))

    def test_facts_and_substantive(self):
        info = needs.needs(self.onto, "dataset:log")
        self.assertEqual((info["expected"], info["filled"], info["completeness"]), (2, 1, 0.5))
        self.assertEqual((info["prov"], info["sources"], info["quotes"], info["degree"]), (2, 2, 0, 1))
        self.assertTrue(info["confirmed"])
        self.assertTrue(info["substantive"])
        self.assertTrue(needs.substantive(self.onto, "dataset:log"))
        self.assertFalse(needs.substantive(self.onto, "term:lonely"))  # no edge
        self.assertFalse(needs.substantive(self.onto, "term:blank"))  # empty summary
        self.assertFalse(needs.substantive(self.onto, "process:hub"))  # no provenance
        self.assertEqual(needs.needs(self.onto, "process:hub")["degree"], 3)  # the background edge does not count

    def test_all_needs_is_local_active_and_cached(self):
        table = needs.all_needs(self.onto)
        self.assertNotIn("goal:done", table)
        self.assertNotIn("garden/crop:tomato", table)
        self.assertIs(needs.all_needs(self.onto), table)

    def test_gap_types_table(self):
        severities = {"missing_dimension": 9, "contradiction": 8, "conflict": 8, "unconfirmed_hub": 7,
                      "archived_premise": 7,
                      "missing_field": 6, "missing_relation": 6, "unbridged_import": 6, "orphan": 5, "thin": 5,
                      "dangling_bridge": 5, "no_provenance": 5, "single_source": 4, "low_confidence": 4, "draft": 3,
                      "draft_link": 3, "stale_source": 3, "open_question": 3, "uncited_source": 3, "duplicate": 3,
                      "pending_backlog": 2}
        self.assertEqual({k: v["severity"] for k, v in needs.GAP_TYPES.items()}, severities)
        self.assertTrue(all(needs.gap_action(k) for k in severities))


class CoverageTest(_support.TempCase):
    def test_dimension_coverage(self):
        root = _support.make_topic(self.tmp)
        onto = graph.Ontology.load(root)
        cov = needs.dimension_coverage(onto)
        self.assertEqual(cov["people"], 1.0)  # 3 substantive of target 3
        self.assertEqual(cov["process"], 0.5)  # the archived rota does not count
        self.assertEqual(cov["vocabulary"], 0.2)
        self.assertEqual(cov["questions"], 0.0)
        self.assertEqual(cov["data"], 1.0)  # a dataset, a tool and 2 cited sources, target 3
        self.assertNotIn("questions", needs.dimension_coverage(onto, closed=["questions"]))
        self.assertEqual(needs.cited_sources(onto), sorted(onto.sources))

    def test_imports_count_only_when_asked(self):
        repo = write_topic(self.tmp, [mk_node("role:keeper", "Keeper")], [])
        add_import(repo, "garden", garden_export())
        rows, _ = store.read_jsonl(repo.path("graph/edges.jsonl"))
        clear()
        onto = graph.Ontology.load(repo)
        plain = needs.dimension_coverage(onto)
        with_imports = needs.dimension_coverage(onto, count_imports=True)
        self.assertAlmostEqual(plain["data"], 1 / 3.0)  # only the cited local source
        self.assertEqual(with_imports["data"], 1.0)  # plus the imported crop and plot, linked and cited
        self.assertEqual(rows, [])


class RenderHelpersTest(unittest.TestCase):
    def test_cuts_and_whole_text_line(self):
        cuts = render.Cuts()
        self.assertEqual(cuts.cut("short"), "short")
        long = "word " * 40
        cut = cuts.cut(long)
        self.assertEqual(len(cut), render.WIDTH)
        self.assertTrue(cut.endswith("..."))
        self.assertEqual(cuts.count, 1)
        self.assertEqual(cuts.whole_text_line("role:a", True),
                         ["  (1 text(s) cut at 100 characters; the whole text: onto_get id=role:a full=true)"])
        self.assertEqual(render.Cuts().whole_text_line("role:a", False), [])

    def test_call_mcp_and_cli(self):
        self.assertEqual(render.call(True, "get", id="role:a", full=True), "onto_get id=role:a full=true")
        self.assertEqual(render.call(False, "get", id="role:a", full=True), "onto get role:a --full")
        self.assertEqual(render.call(True, "brief", subject="weekly menu", budget=0),
                         'onto_brief subject="weekly menu" budget=0')
        self.assertEqual(render.call(False, "brief", subject="weekly menu", budget=0),
                         'onto brief "weekly menu" --budget 0')
        self.assertEqual(render.call(True, "neighbors", id="g/crop:x", depth=2, rels=["owns", "uses"]),
                         'onto_neighbors id=g/crop:x depth=2 rels=["owns","uses"]')
        self.assertEqual(render.call(False, "neighbors", id="g/crop:x", rels=["owns", "uses"], drafts=False),
                         "onto neighbors g/crop:x --rels owns,uses --no-drafts")
        self.assertEqual(render.call(False, "path", **{"from": "a:b", "to": "c:d", "max_depth": 3}),
                         "onto path a:b c:d --max-depth 3")
        self.assertEqual(render.call(True, "decisions", scope=None, active=False), "onto_decisions active=false")

    def test_more_counts_after_the_page(self):
        self.assertEqual(render.more(10, 3), "+7 more")
        self.assertEqual(render.more(10, 3, offset=5), "+2 more")
        self.assertEqual(render.more(10, 5, offset=5), "")
        self.assertEqual(render.more(3, 3), "")

    def test_mark_and_flags(self):
        self.assertEqual(render.mark({"id": "role:a", "trust": "untrusted", "status": "proposed"}),
                         "[untrusted] role:a (draft)")
        self.assertEqual(render.mark({"id": "role:a", "status": "archived", "archived": {"on": "x"}}),
                         "role:a (archived)")
        self.assertEqual(render.mark({"id": "role:a", "draft": True}), "role:a (draft)")
        self.assertEqual(render.mark({"id": "role:a", "untrusted": True}, text="Name"), "[untrusted] Name")
        self.assertEqual(render.flags({"trust": "untrusted", "status": "proposed"}),
                         {"untrusted": True, "draft": True})
        self.assertEqual(render.flags({"trust": "user", "status": "confirmed", "archived": None}), {})

    def test_fmt_prints_literals(self):
        self.assertEqual([render.fmt(v) for v in (False, 0, None, True, 1.5, "text")],
                         ["false", "0", "null", "true", "1.5", "text"])
        self.assertEqual(render.fmt(["a", 0, None]), "a, 0, null")
        self.assertEqual(render.fmt({"b": 1, "a": False}), '{"a":false,"b":1}')

    def test_page_and_collapse(self):
        self.assertEqual(render.page(list(range(10)), 3, 4), ([4, 5, 6], 10))
        self.assertEqual(render.page(list(range(5)), 0, 2), ([2, 3, 4], 5))
        groups, totals = render.collapse_relations({"owns": [{"id": "a:1"}, {"id": "a:1"}, {"id": "a:2"}],
                                                    "uses": [{"id": "t:1"}]}, totals={"owns": 7})
        self.assertEqual(groups["owns"], [{"id": "a:1", "count": 2}, {"id": "a:2"}])
        self.assertEqual(totals, {"owns": 6, "uses": 1})

    def test_version_line_variants(self):
        base = {"ns": "g2t", "version": "unreleased", "matches_release": None, "changes_after": None,
                "imports": [], "kit": "0.2.0", "repo_kit": "0.2.0", "kit_mismatch": False, "richness": None}
        self.assertEqual(render.version_line(base), "g2t unreleased")
        full = dict(base, version="v1", matches_release=False, changes_after=3,
                    richness={"score": 41, "band": "working", "change_text": "+12 since 09-21"},
                    imports=[{"ns": "garden", "ref": "v1", "commit7": "a1b2c3d", "ok": True},
                             {"ns": "kitchen", "ref": "v1", "commit7": "9e8f7a6", "ok": False}],
                    kit_mismatch=True, repo_kit="0.3.0")
        self.assertEqual(render.version_line(full),
                         "g2t v1 + 3 changes after it | richness 41 working (+12 since 09-21) | imports: garden v1 "
                         "a1b2c3d ok, kitchen v1 9e8f7a6 mismatch | kit 0.2.0, topic written by 0.3.0")
        self.assertEqual(render.version_line(dict(base, version="v2", matches_release=True, changes_after=0)),
                         "g2t v2")
        self.assertEqual(render.version_line(dict(base, version="v2", matches_release=False, changes_after=1,
                                                  richness={"score": 52, "band": "working", "change_text": None})),
                         "g2t v2 + 1 change after it | richness 52 working")
        # regression: data that differs from the release with no logged change (a hand edit) printed a bare "v2"
        self.assertEqual(render.version_line(dict(base, version="v2", matches_release=False, changes_after=0)),
                         "g2t v2 + changes after it")

    def test_version_line_from_a_real_stamp(self):
        tmp = self._tmp()
        root = _support.make_topic(tmp)
        stamp = store.version_stamp(store.Repo.open(root))
        self.assertEqual(render.version_line(stamp), "mini unreleased")

    def _tmp(self):
        import shutil
        import tempfile

        tmp = tempfile.mkdtemp(prefix="onto-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        return tmp


def units(n_primary=3, n_related=6, n_quotes=4):
    out = []
    for i in range(n_primary):
        out.append(render.Unit("primary", ["role:p%d  role, confirmed | %s" % (i, "x" * 60)], {"id": "role:p%d" % i},
                               "summaries", "onto_get id=role:p%d" % i, essential=True))
    for i in range(n_related):
        out.append(render.Unit("related", ["related: term:r%d %s" % (i, "y" * 40)], {"id": "term:r%d" % i},
                               "related", "onto_neighbors id=role:p0 depth=2"))
    for i in range(n_quotes):
        out.append(render.Unit("quotes", ['[untrusted] "%s"' % ("q" * 50)], {"n": i}, "quotes",
                               "onto_get id=role:p0 full=true"))
    return out


class SelectTest(unittest.TestCase):
    everything = 'onto_brief subject="weekly menu" budget=0'

    def test_always_fits_and_names_calls(self):
        head = ["g2t unreleased", 'scope "weekly menu" -> role:p0']
        for budget in range(40, 400, 7):
            res = render.select(head, units(), [], budget, self.everything, True)
            self.assertLessEqual(res["budget"]["chars"], budget * render.CHARS_PER_TOKEN, budget)
            text = "\n".join(res["lines"])
            self.assertEqual(res["budget"]["chars"], len(text))
            if res["omitted"]:
                self.assertTrue(res["lines"][-1].startswith("left out to fit %d tokens:" % budget))
                if "items (" in res["lines"][-1]:
                    self.assertIn(self.everything, res["lines"][-1])  # nothing optional fits: one line
                    continue
                for g in res["omitted"][:3]:
                    self.assertIn(g["call"], text)

    def test_no_cap_takes_everything(self):
        res = render.select(["head"], units(), ["tail"], 0, self.everything)
        self.assertEqual(len(res["chosen"]), 13)
        self.assertEqual(res["omitted"], [])
        self.assertEqual(res["lines"][-1], "tail")

    def test_essential_halts_and_sections_stop(self):
        us = units(n_primary=3, n_related=2, n_quotes=0)
        us[1].lines = ["z" * 900]  # the second primary summary cannot fit
        res = render.select(["head"], us, [], 150, self.everything)
        self.assertEqual([u.item["id"] for u in res["chosen"]], ["role:p0"])
        whats = {g["what"]: g["count"] for g in res["omitted"]}
        self.assertEqual(whats, {"summaries": 2, "related": 2})
        us = units(n_primary=1, n_related=3, n_quotes=1)
        us[1].lines = ["z" * 500]
        res = render.select(["head"], us, [], 120, self.everything)
        self.assertEqual([u.section for u in res["chosen"]], ["primary", "quotes"])  # related stops, quotes go on

    def test_footer_names_at_most_three_calls(self):
        us = []
        for i in range(5):
            us.append(render.Unit("s%d" % i, ["line %d %s" % (i, "w" * 80)], None, "group %d" % i, "call_%d" % i))
        res = render.select(["head"], us, [], 60, self.everything)
        foot = res["lines"][-1]
        self.assertEqual([u.section for u in res["chosen"]], ["s0"])
        self.assertEqual(sum(1 for i in range(5) if "(call_%d)" % i in foot), 3)
        self.assertIn("more (%s)" % self.everything, foot)

    def test_one_line_when_nothing_optional_fits(self):
        res = render.select(["h" * 70], units(), [], 20, self.everything)
        self.assertEqual(res["lines"][-1], "left out to fit 20 tokens: 13 items (%s)" % self.everything)

    def test_same_input_same_output(self):
        one = render.select(["head"], units(), [], 120, self.everything)
        two = render.select(["head"], units(), [], 120, self.everything)
        self.assertEqual(one["lines"], two["lines"])


class FitCardTest(unittest.TestCase):
    def test_fits_and_lesser_lists_shrink_first(self):
        lines = [
            render.Line("role:a  Keeper (confirmed, user)"),
            render.Line("owns: ", ["dataset:log-%d" % i for i in range(30)], rank=1),
            render.Line("sources: ", ["src-%012d (2 quotes)" % i for i in range(30)], rank=3),
            "needs: missing_relation owns(in)",
        ]
        body = render.fit_card(lines, 300)
        self.assertLessEqual(len(body), 300)
        owns = [ln for ln in body.split("\n") if ln.startswith("owns: ")][0]
        srcs = [ln for ln in body.split("\n") if ln.startswith("sources: ")][0]
        self.assertIn("+", srcs)
        self.assertGreater(owns.count("dataset:"), srcs.count("src-"))
        self.assertTrue(body.endswith("needs: missing_relation owns(in)"))

    def test_last_resort_cut_and_empty_list(self):
        self.assertEqual(render.Line("owns: ", []).render(), "owns: none")
        self.assertEqual(render.Line("owns: ", ["a", "b"], more=2).render(), "owns: a; b; +2 more")
        body = render.fit_card(["x" * 2000], 100)
        self.assertEqual(len(body), 100)
        self.assertTrue(body.endswith("..."))
        self.assertEqual(render.follow_line([("get", {"id": "role:a", "full": True})]),
                         "Next: onto_get id=role:a full=true")


class TokensTest(unittest.TestCase):
    def test_chars_and_tokens(self):
        self.assertEqual(render.chars(["ab", "c"]), 5)
        self.assertEqual(render.tokens("abcde"), 2)
        self.assertEqual(render.tokens(["abc"]), 1)
        self.assertEqual(util.normalize_ws(render.trunc("a  b")), "a b")


if __name__ == "__main__":
    unittest.main()
