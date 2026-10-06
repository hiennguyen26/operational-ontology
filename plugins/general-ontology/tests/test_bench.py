"""The token benchmark: characters / 4 rounded half up, the JSON-RPC framing counted, the scrubbed server
environment, a missed id failing the run, a git-archive kit measured instead of the working tree, the tar guard,
golden comparison, generated tasks, and the topic left untouched."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tarfile
import unittest
from unittest import mock

from tests import _support
from ontokit import __version__, bench, commands, graph, store
from ontokit.errors import OntoError, UsageError

STATUS_TASK = {"id": "status", "title": "Where the topic stands", "calls": [["onto_status", {}]],
               "expect": ["mini"]}
SEARCH_TASK = {"id": "decisions", "title": "Decisions", "calls": [["onto_decisions", {"limit": 5}]],
               "expect": ["dec-20260928-keep-the-monthly-steward-rota-or-water-e-916a"]}


class BenchCase(_support.TempCase):
    def setUp(self):
        super().setUp()
        self.root = _support.make_topic(self.tmp)

    def tasks_file(self, tasks):
        path = os.path.join(self.tmp, "tasks.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"tasks": tasks}, fh)
        return path


class EstimateTest(unittest.TestCase):
    def test_chars_over_four_rounded_half_up(self):
        for chars, tokens in ((0, 0), (1, 0), (2, 1), (3, 1), (4, 1), (5, 1), (6, 2), (7, 2), (10, 3), (14, 4),
                              (4001, 1000), (4002, 1001)):
            self.assertEqual(bench.estimate_tokens(chars), tokens, chars)
        self.assertEqual(bench.median([3, 1, 2]), 2)
        self.assertEqual(bench.median([1, 2]), 1.5)
        self.assertEqual(bench.median([]), 0)


class EnvTest(unittest.TestCase):
    def test_the_server_env_is_scrubbed(self):
        dirty = {"ONTO_REPO": "/elsewhere", "ONTO_PROFILE": "full", "CLAUDE_PROJECT_DIR": "/elsewhere",
                 "ONTO_HANDOFF": "0", "ONTO_MCP_REEXEC": "1", "KEEP_ME": "yes"}
        with mock.patch.dict(os.environ, dirty):
            env = bench.server_env("2026-09-28T12:00:00Z")
        for key in ("ONTO_REPO", "ONTO_PROFILE", "CLAUDE_PROJECT_DIR", "ONTO_MCP_REEXEC"):
            self.assertNotIn(key, env)
        self.assertEqual(env["ONTO_HANDOFF"], "1")
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(env["ONTO_FIXED_NOW"], "2026-09-28T12:00:00Z")
        self.assertEqual(env["KEEP_ME"], "yes")


class RunTest(BenchCase):
    def test_framing_is_counted(self):
        result = bench.run_tasks([STATUS_TASK, SEARCH_TASK], bench.PLUGIN_DIR, self.root,
                                 fixed_now=_support.FIXED_NOW)
        self.assertEqual(result["profile"], "query")
        self.assertEqual(result["kit"]["version"], __version__)
        for task in result["tasks"]:
            self.assertEqual(task["missed"], [])
            self.assertEqual(task["chars"], sum(s["request_chars"] + s["result_chars"] for s in task["steps"]))
            self.assertEqual(task["tokens"], bench.estimate_tokens(task["chars"]))
            for step in task["steps"]:
                request = json.dumps({"jsonrpc": "2.0", "id": 0, "method": "tools/call",
                                      "params": {"name": step["tool"], "arguments": step["arguments"]}},
                                     separators=(",", ":"))
                self.assertGreaterEqual(step["request_chars"], len(request))  # the id grows, the frame is counted
                self.assertGreater(step["result_chars"], step["text_chars"] + 40)  # the envelope is counted too
                self.assertFalse(step["is_error"])
        totals = result["totals"]
        self.assertEqual(totals["tokens"], sum(t["tokens"] for t in result["tasks"]))
        self.assertEqual(totals["calls"], 2)
        self.assertEqual(totals["missed"], 0)
        overhead = {o["profile"]: o for o in result["overhead"]}
        self.assertEqual(overhead["query"]["tools"], 10)
        self.assertEqual(overhead["full"]["tools"], 18)
        self.assertEqual(overhead["full"]["tokens"], overhead["full"]["tools_list_tokens"]
                         + overhead["full"]["instructions_tokens"])
        self.assertTrue(result["version_line"].startswith("mini unreleased"))
        again = bench.run_tasks([STATUS_TASK, SEARCH_TASK], bench.PLUGIN_DIR, self.root,
                                fixed_now=_support.FIXED_NOW)
        self.assertEqual(again["totals"], totals)  # equal data, equal counts

    def test_the_environment_cannot_steer_the_server(self):
        # A kit inside the data would take over through the handoff, and a stray profile or repo would change
        # what is served: the scrubbed environment keeps the kit under test serving the data it was given.
        kit = os.path.join(self.root, "plugins", "general-ontology")
        os.makedirs(os.path.join(kit, "ontokit"))
        os.makedirs(os.path.join(kit, "bin"))
        with open(os.path.join(kit, "ontokit", "__init__.py"), "w", encoding="utf-8") as fh:
            fh.write("")
        with open(os.path.join(kit, "bin", "onto-mcp"), "w", encoding="utf-8") as fh:
            fh.write("import sys\nsys.exit(7)\n")
        task = dict(STATUS_TASK, calls=[["onto_status", {"format": "json"}]], expect=['"profile":"query"', "mini"])
        with mock.patch.dict(os.environ, {"ONTO_PROFILE": "full", "ONTO_REPO": self.tmp, "ONTO_HANDOFF": ""}):
            result = bench.run_tasks([task], bench.PLUGIN_DIR, self.root, profile="query")
        self.assertEqual(result["tasks"][0]["missed"], [])

    def test_a_missed_id_fails_the_run(self):
        missing = dict(STATUS_TASK, id="missing", expect=["mini", "zzz:not-there"])
        path = self.tasks_file([missing])
        code, out, err = _support.run_cli(["bench", "--tasks", path], self.root)
        self.assertEqual(code, 1, err)
        self.assertIn("zzz:not-there", out)
        self.assertIn("FAILED: 1 expected id(s) missed", out)
        code, out, err = _support.run_cli(["bench", "--tasks", path, "--json"], self.root)
        data = json.loads(out)
        self.assertEqual(data["tasks"][0]["found"], ["mini"])
        self.assertEqual(data["tasks"][0]["missed"], ["zzz:not-there"])
        self.assertEqual(data["exit_code"], 1)

    def test_a_protocol_error_is_a_bench_error(self):
        bad = {"id": "bad", "calls": [["onto_nothing", {}]], "expect": []}
        with self.assertRaises(bench.BenchmarkError):
            bench.run_tasks([bad], bench.PLUGIN_DIR, self.root)

    def test_the_topic_is_left_untouched(self):
        before = _support.snapshot(self.root)
        code, _out, err = _support.run_cli(["bench", "--tasks", self.tasks_file([STATUS_TASK])], self.root)
        self.assertEqual(code, 0, err)
        self.assertEqual(_support.snapshot(self.root), before)

    def test_without_a_topic_it_measures_the_mini_fixture(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        ctx = commands.Context(cwd=empty, env={})
        text, is_error, obj = commands.dispatch(commands.get("bench"), {"tasks": self.tasks_file([STATUS_TASK])},
                                                ctx, "compact")
        self.assertFalse(is_error, text)
        self.assertIn("mini fixture", obj["data"])
        self.assertEqual(obj["clock"], _support.FIXED_NOW)

    def test_tasks_are_checked(self):
        for bad in ([], {"tasks": []}, [{"id": "x"}], [{"id": "x", "calls": [["onto_status"]]}],
                    [{"id": "x", "calls": [["onto_status", {}]], "expect": "mini"}],
                    [{"id": "x", "calls": [["onto_status", {}]]}, {"id": "x", "calls": [["onto_status", {}]]}]):
            with self.assertRaises(UsageError):
                bench.normalize_tasks(bad)
        tasks = bench.normalize_tasks([{"calls": [{"tool": "onto_next", "arguments": {}}]}])
        self.assertEqual(tasks[0]["id"], "task-1")
        self.assertEqual(bench.task_profile(tasks), "full")
        self.assertEqual(bench.task_profile(bench.normalize_tasks([STATUS_TASK])), "query")


class TasksTest(BenchCase):
    def test_default_tasks_come_from_the_graph(self):
        onto = graph.Ontology.load(store.Repo.open(self.root))
        tasks = bench.normalize_tasks(bench.default_tasks(onto))
        ids = [t["id"] for t in tasks]
        for wanted in ("status", "brief", "search", "get", "neighbors", "card", "context", "gaps", "decisions"):
            self.assertIn(wanted, ids)
        names = [str(onto.node(n).get("name")) for n in onto.local_nodes()]
        for task in tasks:
            for expected in task["expect"]:
                self.assertTrue(expected == "mini" or expected in onto.nodes or expected.startswith("dec-"),
                                expected)
            for name in names:
                if " " in name:
                    self.assertNotIn(name, task["title"])  # titles name ids, never text
        self.assertEqual(bench.default_tasks(onto), bench.default_tasks(onto))


class CompareTest(unittest.TestCase):
    def result(self, tokens, over=100):
        return {"tasks": [{"id": "a", "tokens": tokens}, {"id": "b", "tokens": 50}],
                "overhead": [{"profile": "query", "tokens": over}], "totals": {"tokens": tokens + 50}}

    def test_regressions_over_the_tolerance(self):
        same = bench.compare(self.result(100), self.result(100))
        self.assertEqual(same["regressions"], [])
        near = bench.compare(self.result(110), self.result(100))
        self.assertEqual(near["regressions"], [])
        worse = bench.compare(self.result(111, over=120), self.result(100))
        self.assertEqual(worse["regressions"], ["task a", "overhead query"])
        golden = dict(self.result(100), tasks=self.result(100)["tasks"] + [{"id": "gone", "tokens": 1}])
        self.assertEqual(bench.compare(self.result(90), golden)["missing"], ["gone"])
        new = bench.compare(self.result(90), {"tasks": [], "overhead": []})
        self.assertTrue(all(r["golden"] is None for r in new["rows"]))
        with self.assertRaises(UsageError):
            bench.compare(self.result(1), ["not", "a", "result"])

    def test_a_malformed_golden_value_is_a_usage_error(self):
        for bad in ("abc", "100", True, -5, 1.5, [100], {"n": 1}):
            golden = {"tasks": [{"id": "a", "tokens": bad}], "overhead": []}
            with self.assertRaises(UsageError) as caught:
                bench.compare(self.result(100), golden)
            self.assertIn("tokens for task a must be a whole number", str(caught.exception))
        with self.assertRaises(UsageError):
            bench.compare(self.result(100), {"tasks": [], "overhead": [{"profile": "query", "tokens": "x"}]})
        for shape in ({"tasks": "abc"}, {"overhead": 5}, {"totals": "x"}):
            with self.assertRaises(UsageError):
                bench.compare(self.result(100), shape)
        # whole floats are fine, and odd ids are skipped rather than crashing
        ok = bench.compare(self.result(100), {"tasks": [{"id": "a", "tokens": 100.0}, {"id": ["x"], "tokens": 1}],
                                              "overhead": [{"profile": None, "tokens": 1}]})
        self.assertEqual(ok["regressions"], [])
        self.assertEqual(ok["rows"][0]["golden"], 100)

    def test_cli_against_a_golden_file(self):
        tmp = self._tmpdir()
        root = _support.make_topic(tmp)
        tasks = os.path.join(tmp, "tasks.json")
        with open(tasks, "w", encoding="utf-8") as fh:
            json.dump([STATUS_TASK, SEARCH_TASK], fh)
        code, out, err = _support.run_cli(["bench", "--tasks", tasks, "--json"], root)
        self.assertEqual(code, 0, err)
        golden = os.path.join(tmp, "golden.json")
        with open(golden, "w", encoding="utf-8") as fh:
            fh.write(out)
        code, out, err = _support.run_cli(["bench", "--tasks", tasks, "--against", golden], root)
        self.assertEqual(code, 0, err)
        self.assertIn("no regression", out)
        with open(golden, encoding="utf-8") as fh:
            data = json.load(fh)
        data["tasks"][0]["tokens"] = 10
        with open(golden, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        code, out, err = _support.run_cli(["bench", "--tasks", tasks, "--against", golden], root)
        self.assertEqual(code, 1)
        self.assertIn("REGRESSION", out)
        data["tasks"][0]["tokens"] = "abc"
        with open(golden, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        code, out, err = _support.run_cli(["bench", "--tasks", tasks, "--against", golden], root)
        self.assertEqual(code, 2, (out, err))
        self.assertIn("must be a whole number", err)
        self.assertNotIn("internal error", out + err)

    def _tmpdir(self):
        import tempfile

        tmp = os.path.realpath(tempfile.mkdtemp(prefix="onto-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        return tmp


class GitKitTest(BenchCase):
    def make_kit_repo(self):
        git_root = os.path.join(self.tmp, "kitrepo")
        kit = os.path.join(git_root, "plugins", "general-ontology")
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
        for part in ("ontokit", "bin"):
            shutil.copytree(os.path.join(bench.PLUGIN_DIR, part), os.path.join(kit, part), ignore=ignore)
        _support.git_init(git_root)
        _support.commit_all(git_root, "kit")
        return git_root, kit

    def test_the_committed_kit_is_measured_and_the_working_tree_is_not(self):
        git_root, kit = self.make_kit_repo()
        init = os.path.join(kit, "ontokit", "__init__.py")
        with open(init, encoding="utf-8") as fh:
            text = fh.read()
        committed = text.split('__version__ = "')[1].split('"')[0]
        with open(init, "w", encoding="utf-8") as fh:
            fh.write(text.replace('__version__ = "%s"' % committed, '__version__ = "9.9.9"'))
        result = bench.measure_ref(git_root, "HEAD", [STATUS_TASK], self.root, fixed_now=_support.FIXED_NOW)
        self.assertEqual(result["kit"]["version"], committed)
        self.assertEqual(result["kit"]["ref"], "HEAD")
        self.assertEqual(result["kit"]["commit"], _support.git(git_root, "rev-parse", "--short", "HEAD"))
        self.assertEqual(result["tasks"][0]["missed"], [])
        working = bench.run_tasks([STATUS_TASK], kit, self.root)
        self.assertEqual(working["kit"]["version"], "9.9.9")
        self.assertEqual(_support.git(git_root, "status", "--porcelain"), "M plugins/general-ontology/ontokit/"
                                                                         "__init__.py")

    def test_refs_are_checked_before_git_runs(self):
        git_root, _kit = self.make_kit_repo()
        for bad in ("--upload-pack=x", "main", "HEAD~1", "v1..v2", ""):
            with self.assertRaises(UsageError):
                bench.resolve(git_root, bad)
        with self.assertRaises(OntoError):
            bench.resolve(git_root, "v7")  # a tag that does not exist
        self.assertEqual(len(bench.resolve(git_root, "HEAD")), 40)

    def test_the_tar_guard(self):
        for name, kind in (("../evil.txt", tarfile.REGTYPE), ("/abs.txt", tarfile.REGTYPE),
                           ("plugins/general-ontology/link", tarfile.SYMTYPE)):
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode="w") as tar:
                info = tarfile.TarInfo(name)
                info.type = kind
                if kind == tarfile.SYMTYPE:
                    info.linkname = "/etc/passwd"
                    tar.addfile(info)
                else:
                    info.size = 1
                    tar.addfile(info, io.BytesIO(b"x"))
            fake = subprocess.CompletedProcess([], 0, data.getvalue(), b"")
            dest = os.path.join(self.tmp, "out")
            with mock.patch.object(bench.gitutil, "_run", return_value=fake):
                with self.assertRaises(bench.BenchmarkError):
                    bench.extract_kit(self.tmp, "0" * 40, dest)
            self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.txt")))


if __name__ == "__main__":
    unittest.main()
