"""Review round 6 of kit 0.2.0: one regression test per confirmed finding.

Erase: the topic's own node is never erased, the finder marks a summary hit as a field to scrub, and onto erase
--scrub takes a text out of the fields that hold it while the records stay. Migrate: an archived record is history,
so archiving the records that do not fit a fold clears the refusal. Setup: a rerun after a failed plugin install
commits the settings.json setup left, and doctor flags an uncommitted one. Checkpoints: the user's words come from
files, never through a shell. Packs: a refused add_relation names the reason.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from tests import _support
from tests import test_field_learnings as learnings
from tests.test_migrate_020 import SPECIAL as MIGRATE_SPECIAL
from tests.test_migrate_020 import old_kit
from tests.test_setup import SYSTEM_PATH, SetupCase
from ontokit import doctor, graph, ledger, migrate, mutate, store

REPO_ROOT = os.path.dirname(os.path.dirname(_support.PLUGIN_DIR))
NAME = "Jane Doe"
SUMMARY = "A shared vegetable garden, owned by Jane Doe"


def clear():
    graph.clear_cache()
    store.clear_cache()


def read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# erase ------------------------------------------------------------------------------------------------------------
class EraseRootTest(learnings.TopicCase):
    def setUp(self):
        super().setUp()
        ops = [{"op": "update_node", "id": "topic:garden", "set": {"summary": SUMMARY},
                "reason": "The user described the garden in their own words.",
                "prov": [{"quote": SUMMARY, "by": "user"}]},
               learnings.node("process:watering", "process", "Watering", "Jane Doe waters the beds",
                              summary="Jane Doe waters the beds every morning."),
               learnings.edge("$watering", "part_of", "topic:garden", "Jane Doe waters the beds")]
        path = os.path.join(self.root, ".onto", "ops.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(ops, fh)
        text = "%s. Jane Doe waters the beds every morning." % SUMMARY
        code, out, err = self.cli("answer", "q.frame.goal", text, "--ops", "@" + path, "--apply", "--confirm")
        self.assertEqual(code, 0, out + err)
        os.remove(path)

    def decide(self, *scope):
        found = self.cli_json("decide", "--question", "Erase personal data?", "--options", "yes=Erase,no=Keep",
                              "--chosen", "yes", "--rationale", "The user asked to remove this data.",
                              "--scope", ",".join(scope))
        return found["decision"]["id"]

    def test_the_finder_marks_a_summary_hit_as_a_field_to_scrub_not_an_id_to_erase(self):
        found = self.cli_json("erase", "--find", NAME)["find"]
        self.assertNotIn("topic:garden", found["erase"])
        self.assertNotIn("process:watering", found["erase"])  # its name is Watering: only its summary holds it
        self.assertIn("topic:garden", found["edit"])
        self.assertIn("process:watering", found["edit"])
        _code, out, _err = self.cli("erase", "--find", NAME)
        self.assertIn('onto erase --scrub "Jane Doe" --decision <dec>', out)
        self.assertNotRegex(out, r"onto erase each of [^(]*topic:garden")

    def test_the_root_topic_node_is_never_erased(self):
        dec = self.decide("topic:garden")
        before = read(os.path.join(self.root, "graph", "nodes.jsonl"))
        code, out, err = self.cli("erase", "topic:garden", "--decision", dec)
        self.assertEqual(code, 1, out + err)
        self.assertIn("never erased", out + err)
        self.assertIn("onto erase --scrub", out + err)
        self.assertEqual(read(os.path.join(self.root, "graph", "nodes.jsonl")), before)
        clear()
        with self.assertRaises(Exception):
            mutate.apply_ops(self.repo, [{"n": 1, "op": "erase_node", "id": "topic:garden", "decision": dec}],
                             by="user", change_type="erase", summary="x")

    def test_scrub_takes_the_text_out_and_keeps_the_records(self):
        dec = self.decide("topic:garden", "process:watering")
        code, out, err = self.cli("erase", "--scrub", "jane   DOE", "--decision", dec)
        self.assertEqual(code, 0, out + err)
        self.assertIn("scrubbed the text under %s" % dec, out)
        clear()
        nodes = {n["id"]: n for n in read_jsonl(os.path.join(self.root, "graph", "nodes.jsonl"))}
        root = nodes["topic:garden"]
        self.assertEqual(root["status"], "confirmed")
        self.assertEqual(root["name"], "A community garden")
        self.assertEqual(root["summary"], "A shared vegetable garden, owned by [erased]")
        self.assertEqual(nodes["process:watering"]["name"], "Watering")
        self.assertNotIn("Jane Doe", json.dumps(nodes))
        edges = read_jsonl(os.path.join(self.root, "graph", "edges.jsonl"))
        self.assertTrue(edges and all(e["status"] != "archived" for e in edges))
        self.assertNotIn("Jane Doe", json.dumps(edges))
        for folder in ("proposals/done", "ledger/decisions"):
            for name in os.listdir(os.path.join(self.root, folder)):
                self.assertNotIn("Jane Doe", read(os.path.join(self.root, folder, name)), name)
        self.assertNotIn("Jane Doe", read(os.path.join(self.root, "ledger", "changes.jsonl")))
        self.assertEqual(self.cli("validate")[0], 0)
        found = self.cli_json("erase", "--find", NAME)["find"]
        self.assertEqual(found["edit"], [])
        self.assertTrue(all(h["file"].startswith("sources/") for h in found["hits"]), found["hits"])

    def test_scrub_needs_a_scope_that_covers_every_record_it_changes(self):
        dec = self.decide("topic:garden")
        code, out, err = self.cli("erase", "--scrub", NAME, "--decision", dec)
        self.assertEqual(code, 1, out + err)
        self.assertIn("does not cover process:watering", out + err)
        self.assertIn("Jane Doe", read(os.path.join(self.root, "graph", "nodes.jsonl")))

    def test_the_ledger_scrub_never_takes_out_the_topic_name(self):
        # a node named like the topic, erased: the setup decisions and the log keep naming the topic
        ops = [learnings.node("process:a-community-garden", "process", "A community garden", "the garden itself")]
        self.answer("q.process.main", "the garden itself", ops)
        self.cli_json("decide", "--question", "Is A community garden the main process?", "--chosen", "no",
                      "--scope", "setup")
        dec = self.decide("process:a-community-garden")
        code, out, err = self.cli("erase", "process:a-community-garden", "--decision", dec)
        self.assertEqual(code, 0, out + err)
        texts = [read(os.path.join(self.root, "ledger", "decisions", n))
                 for n in os.listdir(os.path.join(self.root, "ledger", "decisions"))]
        self.assertTrue(any("Is A community garden the main process?" in t for t in texts))

    def test_replace_names_ignores_runs_of_spacing(self):
        self.assertEqual(mutate._replace_names("met Jane\n  Doe today", ["Jane Doe"]), "met [erased] today")


# migrate ----------------------------------------------------------------------------------------------------------
class FoldArchivedTest(_support.TempCase):
    """A 0.1.0 topic whose local rests_on links a tree to a wall: archiving that edge clears the fold's refusal."""

    def setUp(self):
        super().setUp()
        place = os.path.join(self.tmp, MIGRATE_SPECIAL)
        self.onto010 = old_kit(os.path.join(place, "kit 0.1.0"))
        self.root = os.path.join(place, "orch")
        _support.git_init(self.root)
        self.old("init", "--name", "orch", "--ns", "orch", "--title", "Orchard")
        text = "The Gala tree depends on the north wall."
        ops = [{"op": "add_kind", "name": "tree", "kind": {"label": "Tree", "plural": "trees"}},
               {"op": "add_kind", "name": "wall", "kind": {"label": "Wall", "plural": "walls"}},
               {"op": "add_relation", "name": "rests_on", "relation": {"from": ["tree"], "to": ["wall"]}},
               {"op": "add_node", "ref": "$gala", "basis": "stated", "node": {"kind": "tree", "name": "Gala tree"},
                "prov": [{"quote": "The Gala tree", "by": "user"}]},
               {"op": "add_node", "ref": "$wall", "basis": "stated", "node": {"kind": "wall", "name": "North wall"},
                "prov": [{"quote": "the north wall", "by": "user"}]},
               {"op": "add_edge", "basis": "stated", "edge": {"src": "$gala", "rel": "rests_on", "dst": "$wall"},
                "prov": [{"quote": text, "by": "user"}]}]
        path = os.path.join(self.root, ".onto", "ops.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(ops, fh)
        self.old("answer", "q.deepen.more", text, "--ops", "@" + path, "--apply", "--confirm")

    def old(self, *args):
        env = dict(os.environ, **_support.GIT_ENV)
        proc = subprocess.run([sys.executable, self.onto010] + list(args), cwd=self.root, env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        self.assertEqual(proc.returncode, 0, (args, proc.stdout, proc.stderr))
        return proc.stdout.decode("utf-8")

    def test_archiving_the_edge_that_does_not_fit_lets_the_fold_through(self):
        code, out, err = _support.run_cli(["migrate"], self.root)
        self.assertEqual(code, 1, out + err)
        self.assertIn("rests_on does not link a tree to a wall", out + err)
        self.assertIn("archive the record", out + err)
        (old_edge,) = [e for e in read_jsonl(os.path.join(self.root, "graph", "edges.jsonl"))]
        code, out, err = _support.run_cli(["--json", "decide", "--question", "Keep rests_on for the wall?",
                                           "--chosen", "no", "--rationale", "rests_on now means resting on a premise",
                                           "--scope", old_edge["id"]], self.root)
        self.assertEqual(code, 0, out + err)
        dec = json.loads(out)["decision"]["id"]
        text = "The Gala tree is related to the north wall."
        ops = [{"op": "archive", "id": old_edge["id"],
                "archived": {"reason": "rests_on now means resting on a premise", "decision": dec}},
               {"op": "add_edge", "basis": "stated",
                "edge": {"src": old_edge["src"], "rel": "related_to", "dst": old_edge["dst"]},
                "prov": [{"quote": text, "by": "user"}]}]
        path = os.path.join(self.root, ".onto", "ops.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(ops, fh)
        code, out, err = _support.run_cli(["answer", "q.deepen.more", text, "--ops", "@" + path, "--apply",
                                           "--confirm"], self.root)
        self.assertEqual(code, 0, out + err)
        clear()
        code, out, err = _support.run_cli(["migrate"], self.root)
        self.assertEqual(code, 0, out + err)
        code, out, err = _support.run_cli(["validate"], self.root)
        self.assertEqual(code, 0, out + err)
        archived = [e for e in read_jsonl(os.path.join(self.root, "graph", "edges.jsonl")) if e["id"] == old_edge["id"]]
        self.assertEqual(archived[0]["status"], "archived")

    def test_an_active_edge_that_does_not_fit_still_refuses(self):
        code, out, err = _support.run_cli(["migrate", "--check"], self.root)
        self.assertEqual(code, 1, out + err)
        self.assertIn("P09", out + err)
        self.assertNotIn("Change or archive", out + err)  # a remedy that cannot work for an edge


# setup ------------------------------------------------------------------------------------------------------------
FLAKY_CLAUDE = """#!/bin/sh
if [ "$1 $2 $3" = "plugin marketplace list" ]; then printf '[]\\n'; exit 0; fi
if [ "$1 $2 $3" = "plugin marketplace add" ]; then
  mkdir -p .claude
  printf '{\\n  "extraKnownMarketplaces": {"general-ontology": {"source": {"source": "github", "repo": "x/y"}}}\\n}\\n' \\
    > .claude/settings.json
  exit 0
fi
if [ "$1 $2" = "plugin install" ]; then
  if [ -n "$CLAUDE_STUB_FAIL_INSTALL" ]; then echo "network error"; exit 1; fi
  printf '{\\n  "enabledPlugins": {"general-ontology@general-ontology": true},\\n  "extraKnownMarketplaces": {"general-ontology": {"source": {"source": "github", "repo": "x/y"}}}\\n}\\n' \\
    > .claude/settings.json
  exit 0
fi
exit 0
"""


class SettingsRerunTest(SetupCase):
    def stub(self):
        bin_dir = os.path.join(self.tmp, "stub-bin")
        os.makedirs(bin_dir)
        path = os.path.join(bin_dir, "claude")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(FLAKY_CLAUDE)
        os.chmod(path, 0o755)
        return bin_dir

    def test_a_rerun_after_a_failed_install_commits_setups_settings(self):
        from unittest import mock

        bin_dir = self.stub()
        target = os.path.join(self.place, "chess-openings")
        env = {"PATH": bin_dir + os.pathsep + SYSTEM_PATH, "CLAUDE_STUB_FAIL_INSTALL": "1"}
        with mock.patch.dict(os.environ, env):
            code, obj, err = self.setup("--yes", "--launch", "none", "--new", target)
        self.assertEqual(self.steps(obj)["plugin"], "failed", (obj, err))
        self.assertIn(".claude/settings.json", _support.git(target, "status", "--short", "--untracked-files=all"))
        env.pop("CLAUDE_STUB_FAIL_INSTALL")
        with mock.patch.dict(os.environ, env):
            os.environ.pop("CLAUDE_STUB_FAIL_INSTALL", None)
            code, obj, err = self.setup("--yes", "--launch", "none", "--new", target)
        self.assertEqual(code, 0, (obj, err))
        steps = {s["id"]: s for s in obj["steps"]}
        self.assertNotIn("did not write", steps["commit"]["detail"])
        self.assertEqual(steps["plugin"]["status"], "done", steps["plugin"])
        self.assertIn("committed it", steps["plugin"]["detail"])
        self.assertEqual(_support.git(target, "status", "--short", "--untracked-files=all"), "")
        self.assertIn("Wire the general-ontology plugin", _support.git(target, "log", "--format=%s"))

    def test_a_settings_file_with_the_users_own_entries_stays_theirs(self):
        bin_dir = self.stub()
        target = os.path.join(self.place, "tomato")
        from unittest import mock

        with mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + SYSTEM_PATH, "CLAUDE_STUB_FAIL_INSTALL": "1"}):
            self.setup("--yes", "--launch", "none", "--new", target)
        path = os.path.join(target, ".claude", "settings.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"permissions": {"allow": ["Bash(ls)"]}}, fh)
        with mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + SYSTEM_PATH}):
            os.environ.pop("CLAUDE_STUB_FAIL_INSTALL", None)
            _code, obj, _err = self.setup("--yes", "--launch", "none", "--new", target)
        plugin = [s for s in obj["steps"] if s["id"] == "plugin"][0]
        self.assertIn("uncommitted changes before setup ran", plugin["detail"])

    def test_doctor_flags_an_uncommitted_settings_file(self):
        root = _support.init_topic(self.place, "garden", "A community garden")
        _support.git(root, "init", "-q")
        _support.commit_all(root, "start")
        os.makedirs(os.path.join(root, ".claude"))
        with open(os.path.join(root, ".claude", "settings.json"), "w", encoding="utf-8") as fh:
            json.dump({"enabledPlugins": {"general-ontology@general-ontology": True}}, fh)
        checks = {c["id"]: c for c in doctor._topic_checks(root)}
        self.assertEqual(checks["wiring"]["status"], "warn")
        self.assertIn("this machine only", checks["wiring"]["detail"])
        _support.commit_all(root, "wire")
        self.assertNotIn("wiring", {c["id"] for c in doctor._topic_checks(root)})


# checkpoints ------------------------------------------------------------------------------------------------------
class CheckpointFromFileTest(learnings.TopicCase):
    def test_the_checkpoint_words_come_from_files_as_typed(self):
        folder = os.path.join(self.root, ".onto")
        os.makedirs(folder, exist_ok=True)
        files = {"done": "quick start, then the kinds\n", "next": "ask who waters\n",
                 "open-questions": "is the $200 seed budget fixed?\nand what does `whoami` mean here\n\n"}
        args = ["log", "--checkpoint"]
        for key, text in files.items():
            path = os.path.join(folder, "%s.txt" % key)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            args += ["--%s-file" % key, path]
        code, out, err = self.cli(*args)
        self.assertEqual(code, 0, out + err)
        last = ledger.last_checkpoint(self.repo)
        self.assertEqual(last["done"], ["quick start, then the kinds"])  # one item per line, no comma split
        self.assertEqual(last["next"], ["ask who waters"])
        self.assertEqual(last["open_questions"], ["is the $200 seed budget fixed?",
                                                  "and what does `whoami` mean here"])
        code, out, err = self.cli("log", "--checkpoint", "--done", "x", "--done-file", os.path.join(folder,
                                                                                                    "done.txt"))
        self.assertEqual(code, 2, out + err)
        self.assertIn("not both", err)

    def test_every_checkpoint_example_reads_from_files(self):
        # H5: the kit docs that took checkpoint examples out of AGENTS.md are checked too
        for rel in ("AGENTS.md", "CLAUDE.md", os.path.join("plugins", "general-ontology", "skills", "onto-interview",
                                                           "SKILL.md"),
                    os.path.join("plugins", "general-ontology", "docs", "interviewing.md"),
                    os.path.join("plugins", "general-ontology", "docs", "checks.md")):
            text = read(os.path.join(REPO_ROOT, rel))
            for flag in ("--done", "--next", "--open-questions"):
                self.assertNotRegex(text, r"%s \"" % re.escape(flag), rel)
            self.assertIn("--open-questions-file", text, rel)


# packs ------------------------------------------------------------------------------------------------------------
class RelationRefusalTest(learnings.TopicCase):
    def test_a_malformed_add_relation_names_the_field(self):
        self.answer("q.vocab.kinds", "Recipes and ingredients.", [
            {"op": "add_kind", "name": "recipe", "kind": {"label": "Recipe", "plural": "recipes"}},
            {"op": "add_kind", "name": "ingredient", "kind": {"label": "Ingredient", "plural": "ingredients"}}])
        path = os.path.join(self.root, ".onto", "ops.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        cases = [({"from": ["recipe"], "to": ["ingredient"], "brief": "a recipe needs this"},
                  "$.relations.has_ingredient.brief: 'a recipe needs this' is not of type 'boolean'"),
                 ({"from": "recipe", "to": ["ingredient"]}, "$.relations.has_ingredient.from")]
        for relation, reason in cases:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump([{"op": "add_relation", "name": "has_ingredient", "relation": relation}], fh)
            code, out, err = self.cli("answer", "q.deepen.more", "Recipes need ingredients.", "--ops", "@" + path,
                                      "--apply", "--confirm")
            self.assertEqual(code, 1, out + err)
            self.assertIn("the local pack would fail its schema: " + reason, out + err)

    def test_the_documented_relation_example_is_accepted(self):
        from tests.test_e2e import doc_answer, doc_json

        rel = "onto-interview/references/recap-examples.md"
        (kinds,) = doc_json(rel, "The main kinds (right after the quick start)")
        self.answer("q.vocab.kinds", doc_answer(rel, "The main kinds (right after the quick start)"), kinds)
        (ops,) = doc_json(rel, "A link between two of your kinds")
        path = os.path.join(self.root, ".onto", "ops.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(ops, fh)
        code, out, err = self.cli("answer", "q.deepen.more", doc_answer(rel, "A link between two of your kinds"),
                                  "--ops", "@" + path, "--apply", "--confirm")
        self.assertEqual(code, 0, out + err)
        clear()
        edges = [e for e in read_jsonl(os.path.join(self.root, "graph", "edges.jsonl")) if e["rel"] == "grows_in"]
        self.assertEqual([(e["src"], e["dst"]) for e in edges], [("crop:tomatoes", "bed:north-bed")])


# the commit steps of the docs -------------------------------------------------------------------------------------
class GuardedCommitDocsTest(_support.TempCase):
    def test_no_upgrade_or_merge_step_sweeps_untracked_files(self):
        # H5: the merge steps and the commit checks moved from AGENTS.md to these kit docs, checked the same way
        for rel in ("README.md", "AGENTS.md", os.path.join("plugins", "general-ontology", "skills", "onto-review",
                                                           "SKILL.md"),
                    os.path.join("plugins", "general-ontology", "docs", "merging.md"),
                    os.path.join("plugins", "general-ontology", "docs", "checks.md")):
            text = read(os.path.join(REPO_ROOT, rel))
            for line in text.splitlines():
                if "merge" in line.lower() or "Upgrade the kit" in line or "baseline" in line:
                    self.assertNotIn("git add -A && git commit --no-edit", line, rel)
                    self.assertNotIn('git add -A && git commit -m "Upgrade the kit"', line, rel)
        readme = read(os.path.join(REPO_ROOT, "README.md"))
        block = readme.split("## Upgrading the kit", 1)[1].split("```", 2)[1]
        self.assertIn("git add -u", block)
        self.assertIn("onto scan .", block)
        self.assertIn("git status --short", block)
        self.assertNotIn("git add -A", block)


if __name__ == "__main__":
    import unittest

    unittest.main()
