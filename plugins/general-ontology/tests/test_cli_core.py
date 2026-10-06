"""commands, cli and cmd_core: the registry shape, dispatch paging and the confirm gate, not built (exit 3), errors
under --json, the shared flags before and after the subcommand, status with the optional modules missing, and the
core commands end to end (init, status, validate, decide, decisions, log with --checkpoint, migrate)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests import _support
from ontokit import __version__, cmd_core, commands, graph, ledger, store

E2 = {
    # name: (tool, profile)
    "init": (None, "cli"), "status": ("onto_status", "query"), "brief": ("onto_brief", "query"),
    "context": ("onto_context", "query"), "card": ("onto_card", "query"), "get": ("onto_get", "query"),
    "search": ("onto_search", "query"), "neighbors": ("onto_neighbors", "query"), "path": ("onto_path", "query"),
    "gaps": ("onto_gaps", "query"), "decisions": ("onto_decisions", "query"), "next": ("onto_next", "full"),
    "answer": ("onto_answer", "full"), "ingest": ("onto_ingest", "full"), "propose": ("onto_propose", "full"),
    "review": ("onto_review", "full"), "apply": ("onto_apply", "full"), "decide": ("onto_decide", "full"),
    "import": ("onto_import", "full"), "validate": (None, "cli"), "log": (None, "cli"), "migrate": (None, "cli"),
    "dupes": (None, "cli"), "eval": (None, "cli"), "erase": (None, "cli"), "pack": (None, "cli"),
    "tools": (None, "cli"), "build": (None, "cli"), "release": (None, "cli"), "scan": (None, "cli"),
    "bench": (None, "cli"), "setup": (None, "cli"), "doctor": (None, "cli"), "agents": (None, "cli"),
}
CORE = ("init", "status", "validate", "decide", "decisions", "log", "pack", "migrate")
FAKE_ITEMS = ["item-%02d" % i for i in range(25)]


# a fake command for dispatch tests -----------------------------------------------------------------------------
def fake_handler(ctx, args):
    if args.get("fail"):
        from ontokit.errors import NotFound

        raise NotFound("thing: not in the ontology", candidates=["crop:tomato"])
    return {"items": list(FAKE_ITEMS), "preview_seen": bool(ctx.preview), "exit_code": args.get("code") or 0}


def preview_handler(ctx, args):
    return {"preview": {"destructive": [1]}}


def waive_handler(ctx, args):
    was = bool(ctx.preview)
    ctx.preview = False
    return {"gate_was_set": was}


def fake_render(result, mode, ctx):
    return ["%s %s" % (mode, ", ".join(result["items"]))]


FAKE = commands.Command(
    "fake", "onto_fake", "full", "Fake", "A fake command for tests.",
    {"word": {"type": "string", "default": None}, "fail": {"type": "boolean", "default": False},
     "code": {"type": "integer", "default": 0}, "limit": {"type": "integer", "default": 10},
     "offset": {"type": "integer", "default": 0}, "confirm": {"type": "boolean", "default": False}},
    positional=("word",), handler="tests.test_cli_core:fake_handler", renderer="tests.test_cli_core:fake_render",
    lists=("items",), default_limit=10, confirm=True,
)


def clear():
    graph.clear_cache()
    store.clear_cache()


class RegistryTest(unittest.TestCase):
    def test_every_command_of_e2(self):
        self.assertEqual({c.name for c in commands.COMMANDS}, set(E2))
        for cmd in commands.COMMANDS:
            tool, profile = E2[cmd.name]
            self.assertEqual((cmd.tool, cmd.profile), (tool, profile), cmd.name)
            self.assertIsInstance(cmd.props, dict)
            self.assertRegex(cmd.handler, r"^ontokit\.[a-z_]+:cmd_[a-z_]+$")
            self.assertRegex(cmd.renderer, r"^ontokit\.[a-z_]+:render_[a-z_]+$")
            self.assertLessEqual(len(cmd.short), 300)
            self.assertTrue(set(cmd.required) <= set(cmd.props), cmd.name)
            self.assertTrue(set(cmd.positional) <= set(cmd.props), cmd.name)
            for key in cmd.lists:
                self.assertIn("limit", cmd.props, cmd.name)
            for name, schema in cmd.props.items():
                self.assertIn("type", schema, "%s.%s" % (cmd.name, name))
                self.assertIn("default", schema, "%s.%s" % (cmd.name, name))
            if cmd.tool:
                self.assertEqual(cmd.tool, "onto_" + cmd.name)
                self.assertIn("readOnlyHint", cmd.annotations)
        self.assertEqual(len(commands.tools("query")), 10)
        self.assertEqual(len(commands.tools("full")), 18)

    def test_annotations_confirm_and_defaults(self):
        get = commands.get
        self.assertEqual({c.name for c in commands.COMMANDS if c.confirm}, {"answer", "apply", "import"})
        self.assertEqual(get("onto_apply").annotations,
                         {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
        self.assertTrue(get("import").annotations["destructiveHint"])
        for name in ("status", "brief", "search", "get", "decisions", "gaps"):
            self.assertTrue(get(name).annotations["readOnlyHint"], name)
        self.assertEqual(get("brief").props["budget"]["default"], 1500)
        self.assertEqual(get("context").props["budget"]["default"], 1000)
        self.assertEqual(get("neighbors").props["limit"]["default"], 40)
        self.assertEqual(get("get").props["limit"]["default"], 10)
        self.assertEqual(get("path").props["max_depth"]["default"], 4)
        self.assertEqual(get("log").props["since"]["default"], "7d")
        self.assertEqual(get("gaps").aliases, {"richness": {"section": "summary"}})
        self.assertIs(get("richness"), get("gaps"))
        self.assertEqual(get("ingest").cli_only, ("allow_any_path",))
        with self.assertRaises(Exception):
            get("no-such-command")

    def test_optional_returns_none_for_missing_modules(self):
        self.assertIsNone(commands.optional("no_such_module_here"))
        self.assertIsNotNone(commands.optional("render"))

    def test_a_broken_module_is_not_reported_as_not_built(self):
        # regression: an ImportError inside a built package module used to read as "not built" (exit 3) and made
        # sanitize fall back to a secret-only scan without a word
        import tempfile
        import ontokit

        folder = tempfile.mkdtemp(prefix="onto-broken-")
        self.addCleanup(__import__("shutil").rmtree, folder, True)
        with open(os.path.join(folder, "zz_broken.py"), "w", encoding="utf-8") as fh:
            fh.write("import zz_no_such_dependency\n\ndef cmd_x(ctx, args):\n    return {}\n")
        self.addCleanup(sys.modules.pop, "ontokit.zz_broken", None)
        with mock.patch.object(ontokit, "__path__", list(ontokit.__path__) + [folder]):
            with self.assertRaises(ImportError):
                commands.optional("zz_broken")
            broken = commands.Command("zz", None, "cli", "Broken", "A module that fails to import.", {},
                                      handler="ontokit.zz_broken:cmd_x", needs_repo=False)
            with self.assertRaises(ImportError):
                commands.dispatch(broken, {}, commands.Context(env={}), "compact")
            missing_fn = commands.Command("zz2", None, "cli", "Missing", "A function not written yet.", {},
                                          handler="ontokit.render:cmd_nothing", needs_repo=False)
            _t, is_error, obj = commands.dispatch(missing_fn, {}, commands.Context(env={}), "compact")
            self.assertEqual((is_error, obj["error"], obj["exit_code"]), (True, "not_built", 3))

    def test_every_switch_has_a_no_form(self):
        # regression: render.call dropped False on the CLI, so a follow-up for `decisions --no-active` (or a brief
        # with drafts=false) silently turned the switch back on
        from ontokit import cli, render

        parser = cli.build_parser()
        self.assertFalse(vars(parser.parse_args(["get", "x:y", "--no-full"]))["full"])
        self.assertFalse(vars(parser.parse_args(["decisions", "--no-active"]))["active"])
        self.assertEqual(render.call(False, "decisions", active=False, offset=20), "onto decisions --no-active --offset 20")
        self.assertEqual(render.call(True, "decisions", active=False), "onto_decisions active=false")


class DispatchTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        clear()
        self.root = _support.make_topic(self.tmp)
        self.ctx = commands.Context(explicit=self.root)

    def test_paging_totals_and_page_line(self):
        text, is_error, obj = commands.dispatch(FAKE, {"word": "x"}, self.ctx, "compact")
        self.assertFalse(is_error)
        self.assertEqual(obj["items"], FAKE_ITEMS[:10])
        self.assertEqual(obj["totals"], {"items": 25})
        self.assertEqual(obj["paging"], {"key": "items", "offset": 0, "limit": 10, "more": True, "remaining": 15,
                                         "next_offset": 10})
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("mini unreleased"))
        self.assertEqual(lines[-1], "[page] items 1-10 of 25; next: onto fake --word x --offset 10")
        text, _e, obj = commands.dispatch(FAKE, {"word": "x", "offset": 20}, self.ctx, "compact")
        self.assertEqual(obj["items"], FAKE_ITEMS[20:])
        self.assertFalse(obj["paging"]["more"])
        self.assertIsNone(obj["paging"]["next_offset"])
        self.assertNotIn("[page]", text)
        _t, _e, obj = commands.dispatch(FAKE, {"limit": 0}, self.ctx, "compact")
        self.assertEqual(len(obj["items"]), 25)

    def test_json_and_text_formats(self):
        text, _e, obj = commands.dispatch(FAKE, {}, self.ctx, "json")
        parsed = json.loads(text)
        self.assertEqual(parsed["command"], "fake")
        self.assertEqual(parsed["version"]["ns"], "mini")
        text, _e, _o = commands.dispatch(FAKE, {}, self.ctx, "text")
        self.assertIn("\ntext item-00", text)

    def test_mcp_confirm_gate_previews(self):
        mcp = commands.Context(explicit=self.root, mcp=True, profile="full")
        text, is_error, obj = commands.dispatch(FAKE, {}, mcp, "compact")
        self.assertTrue(is_error)
        self.assertTrue(obj["preview_seen"])
        self.assertTrue(text.endswith(commands.PREVIEW_TEXT))
        self.assertIn("onto_fake offset=10", text)
        self.assertFalse(mcp.preview)
        _t, is_error, obj = commands.dispatch(FAKE, {"confirm": True}, mcp, "compact")
        self.assertFalse(is_error)
        self.assertFalse(obj["preview_seen"])
        _t, _e, obj = commands.dispatch(FAKE, {}, self.ctx, "compact")
        self.assertFalse(obj["preview_seen"])  # the CLI runs directly

    def test_a_handler_may_waive_the_gate(self):
        # the confirm gate: a call that needs no confirm (import status or suggest, answer without apply) clears
        # ctx.preview itself and is then not a preview error
        mcp = commands.Context(explicit=self.root, mcp=True, profile="full")
        waiver = commands.Command("waive", "onto_waive", "full", "Waive", "Clears the gate.",
                                  {"confirm": commands.CONFIRM}, handler="tests.test_cli_core:waive_handler",
                                  confirm=True)
        text, is_error, obj = commands.dispatch(waiver, {}, mcp, "compact")
        self.assertFalse(is_error)
        self.assertTrue(obj["gate_was_set"])
        self.assertNotIn(commands.PREVIEW_TEXT, text)
        self.assertFalse(mcp.preview)

    def test_a_handler_preview_object_survives_json(self):
        mcp = commands.Context(explicit=self.root, mcp=True, profile="full")
        gated = commands.Command("gated", "onto_gated", "full", "Gated", "Previews.", {"confirm": commands.CONFIRM},
                                 handler="tests.test_cli_core:preview_handler", confirm=True)
        for fmt in ("json", "compact"):
            _text, is_error, obj = commands.dispatch(gated, {}, mcp, fmt)
            self.assertTrue(is_error)
            self.assertEqual(obj["preview"], {"destructive": [1]}, fmt)

    def test_decisions_read_both_spellings_of_a_local_id(self):
        repo = store.Repo.open(self.root)
        ledger.decide(repo, "Who waters on Sundays?", [], "the steward", scope=["mini/role:bed-steward"])
        ledger.decide(repo, "Does the steward weed too?", [], "yes", scope=["role:bed-steward"])
        clear()
        for scope in (["role:bed-steward"], ["mini/role:bed-steward"]):
            _t, _e, obj = commands.dispatch(commands.get("decisions"), {"scope": scope}, self.ctx, "json")
            self.assertEqual(len(obj["decisions"]), 2, scope)

    def test_decisions_page_line_keeps_a_switch_off(self):
        for n in range(3):
            ledger.decide(store.Repo.open(self.root), "Question %d?" % n, [], "yes", scope=["x:%d" % n])
        clear()
        text, _e, obj = commands.dispatch(commands.get("decisions"), {"active": False, "limit": 1}, self.ctx)
        page = [ln for ln in text.splitlines() if ln.startswith("[page]")]
        self.assertEqual(len(page), 1)
        self.assertIn("--no-active", page[0])

    def test_errors_become_results(self):
        text, is_error, obj = commands.dispatch(FAKE, {"fail": True}, self.ctx, "compact")
        self.assertTrue(is_error)
        self.assertEqual(obj["error"], "not_found")
        self.assertEqual(obj["exit_code"], 1)
        self.assertEqual(text.splitlines()[1:], ["thing: not in the ontology", "did you mean: crop:tomato"])
        _t, is_error, obj = commands.dispatch(FAKE, {"bogus": 1}, self.ctx, "compact")
        self.assertEqual((obj["error"], obj["exit_code"]), ("usage", 2))
        self.assertIn("accepted:", obj["message"])
        _t, is_error, obj = commands.dispatch(FAKE, {"code": 2}, self.ctx, "compact")
        self.assertTrue(is_error)
        self.assertEqual(obj["exit_code"], 2)

    def test_not_built_is_exit_3(self):
        missing = commands.Command("ghost", None, "cli", "Ghost", "Not built.", {},
                                   handler="ontokit.no_such_module:cmd_ghost", renderer="ontokit.x:render_ghost")
        text, is_error, obj = commands.dispatch(missing, {}, self.ctx, "compact")
        self.assertTrue(is_error)
        self.assertEqual((obj["error"], obj["exit_code"]), ("not_built", 3))
        self.assertIn("ghost: not built", text)

    def test_context_call_and_stamp(self):
        self.assertEqual(self.ctx.call("get", id="crop:tomato", full=True), "onto get crop:tomato --full")
        mcp = commands.Context(explicit=self.root, mcp=True)
        self.assertEqual(mcp.call("onto_get", id="crop:tomato", full=True), "onto_get id=crop:tomato full=true")
        stamp = self.ctx.stamp()
        self.assertEqual((stamp["ns"], stamp["version"]), ("mini", "unreleased"))
        self.assertIsNone(commands.Context(cwd=self.tmp, env={}).stamp())
        onto = self.ctx.onto()
        self.assertIs(self.ctx.onto(), onto)
        self.assertEqual(self.ctx.loads, 1)


class CliTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        clear()

    def mini(self, fixture="mini"):
        return _support.make_topic(self.tmp, fixture)

    def test_every_unbuilt_command_exits_3(self):
        root = self.mini()
        args = {"brief": ["brief", "watering"], "context": ["context", "write a plan"], "card": ["card", "x:y"],
                "get": ["get", "x:y"], "search": ["search", "tomato"], "neighbors": ["neighbors", "x:y"],
                "path": ["path", "a:b", "c:d"], "gaps": ["gaps"], "next": ["next"],
                "answer": ["answer", "q.frame.you", "hello"], "ingest": ["ingest", "--title", "Note"],
                "propose": ["propose", "--proposal", "{}"], "review": ["review"],
                "apply": ["apply", "prop-20260928-e5c7e9"], "import": ["import", "status"], "dupes": ["dupes"],
                "eval": ["eval", "--gold", "g.json", "--proposal", "p.json"],
                "erase": ["erase", "x:y", "--decision", "dec-20260928-x-0000"], "tools": ["tools", "check"],
                "build": ["build"], "release": ["release"], "scan": ["scan"], "bench": ["bench"]}
        checked = 0
        for cmd in commands.COMMANDS:
            if cmd.name in CORE:
                continue
            module = cmd.handler.split(":")[0].split(".")[1]
            if commands.optional(module) is not None:
                continue  # built by its package; its own tests cover it
            code, out, err = _support.run_cli(args[cmd.name], root)
            self.assertEqual(code, 3, (cmd.name, out, err))
            self.assertIn("%s: not built" % cmd.name, err)
            self.assertTrue(err.startswith("mini unreleased"), err)
            checked += 1
        code, out, _err = _support.run_cli(["richness", "--json"], root)
        if commands.optional("richness") is None:
            self.assertEqual(code, 3)
            self.assertEqual(json.loads(out)["error"], "not_built")

    def test_errors_under_json_go_to_stdout(self):
        root = self.mini()
        code, out, err = _support.run_cli(["decide", "--question", "Which?", "--options", "a=A,b=B",
                                           "--chosen", "c", "--json"], root)
        self.assertEqual(code, 2)
        self.assertEqual(err, "")
        obj = json.loads(out)
        self.assertEqual((obj["error"], obj["exit_code"]), ("usage", 2))
        self.assertEqual(obj["version"]["ns"], "mini")
        code, out, err = _support.run_cli(["decide", "--question", "Which?", "--options", "a=A,b=B",
                                           "--chosen", "c"], root)
        self.assertEqual((code, out), (2, ""))
        self.assertRegex(err.splitlines()[0], r"^mini unreleased( \| richness [0-9]+ [a-z]+)?$")
        code, out, err = _support.run_cli(["propose", "--proposal", "not json"], root)
        self.assertEqual(code, 2)
        self.assertIn("not valid JSON", err)

    def test_shared_flags_before_and_after_the_subcommand(self):
        root = self.mini()
        for argv in (["--json", "status", "--repo", root], ["status", "--json", "--repo", root],
                     ["--repo", root, "--json", "status"]):
            code, out, err = _support.run_cli(argv)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["command"], "status")
        code, out, _err = _support.run_cli(["--text", "status", "--detail"], root)
        self.assertEqual(code, 0)
        self.assertIn("nodes by kind", out)
        code, _out, err = _support.run_cli(["status", "--bogus"], root)
        self.assertEqual(code, 2)
        code, out, _err = _support.run_cli([])
        self.assertEqual(code, 0)
        self.assertIn("usage: onto", out)

    def test_no_repo_is_a_domain_error(self):
        env = {"ONTO_REPO": os.path.join(self.tmp, "nowhere")}
        with mock.patch.dict(os.environ, env):
            code, out, err = _support.run_cli(["status"])
        self.assertEqual(code, 1)
        self.assertIn("not a topic repo", err)

    def test_init_then_status(self):
        path = os.path.join(self.tmp, "garden")
        code, out, err = _support.run_cli(["init", "--ns", "garden", "--title", "Community garden", "--path", path])
        self.assertEqual(code, 0, err)
        first, second = out.splitlines()[:2]
        self.assertRegex(first, r"^garden unreleased( \| richness [0-9]+ [a-z]+)?$")
        self.assertTrue(second.startswith("created topic garden"), second)
        self.assertIn("Next: the quick start", out)
        self.assertIn("q.frame.you", out)
        code, out, err = _support.run_cli(["status"], path)
        self.assertEqual(code, 0, err)
        self.assertIn("Community garden (ns garden)", out)
        self.assertIn("nodes 1 (0 drafts)", out)
        self.assertIn("Next:", out)
        code, out, err = _support.run_cli(["validate"], path)
        self.assertEqual(code, 0, err)
        self.assertIn("ok: 1 nodes, 0 edges, 1 sources, 0 imports", out)
        code, _out, err = _support.run_cli(["init", "--ns", "garden", "--title", "Again", "--path", path])
        self.assertEqual(code, 1)
        self.assertIn("already holds ontology.json", err)
        with open(os.path.join(path, "ontology.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["name"], "garden")

    def test_status_with_modules_missing(self):
        root = self.mini()
        with mock.patch.object(cmd_core, "optional", lambda name: None), \
                mock.patch.object(commands, "optional", lambda name: None):
            code, out, err = _support.run_cli(["status", "--json"], root)
        self.assertEqual(code, 0, err)
        obj = json.loads(out)
        for key in ("stage", "progress", "richness", "pending", "stale_sources", "imports", "last_checkpoint", "kit",
                    "profile", "load"):
            self.assertIn(key, obj)
        self.assertIsNone(obj["progress"])
        self.assertIsNone(obj["richness"])
        self.assertIsNone(obj["stage"])
        self.assertEqual(obj["pending"]["count"], 1)
        self.assertEqual(obj["pending"]["top"], ["prop-20260928-e5c7e9"])
        self.assertFalse(obj["pending"]["over_max"])
        self.assertTrue(any("interview module is not built" in n for n in obj["notes"]))
        self.assertTrue(any("richness module is not built" in n for n in obj["notes"]))
        self.assertEqual(set(obj["load"]), {"loads", "load_s", "calls"})
        self.assertEqual(obj["profile"], "cli")
        self.assertIn("review", obj["next"]["call"])

    def test_status_survives_a_failing_module(self):
        # regression: status (the first call of every session) crashed when a package module raised
        root = self.mini()

        class Boom(object):
            def progress(self, onto):
                raise RuntimeError("boom")

            def summary(self, onto):
                raise RuntimeError("boom")

        with mock.patch.object(cmd_core, "optional", lambda name: Boom() if name in ("interview", "richness")
                               else None), mock.patch.object(commands, "optional", lambda name: None):
            code, out, err = _support.run_cli(["status", "--json"], root)
        self.assertEqual(code, 0, err)
        obj = json.loads(out)
        self.assertIsNone(obj["progress"])
        self.assertIsNone(obj["richness"])
        self.assertTrue(any("interview.progress failed: RuntimeError" in n for n in obj["notes"]))
        self.assertTrue(any("richness.summary failed: RuntimeError" in n for n in obj["notes"]))

    def test_harness_keeps_the_test_switches(self):
        self.assertEqual(set(_support.KEEP_ENV), {"ONTO_SKIP_PERF", "ONTO_DEBUG", "ONTO_RECORD_GOLDEN"})

    def test_validate_mini_and_broken(self):
        code, out, err = _support.run_cli(["validate"], self.mini())
        self.assertEqual(code, 0, err)
        self.assertIn("W01", out)
        self.assertIn("ok: 12 nodes, 15 edges, 2 sources, 0 imports", out)
        broken = _support.make_topic(self.tmp, "mini-broken", ns="broken")
        code, out, err = _support.run_cli(["validate"], broken)
        self.assertEqual(code, 1)
        self.assertIn("P10", out)
        code, out, _err = _support.run_cli(["validate", "--json"], broken)
        self.assertEqual(code, 1)
        self.assertEqual([p["code"] for p in json.loads(out)["problems"]], ["P10"])

    def test_decide_decisions_log_and_checkpoint(self):
        root = self.mini()
        code, out, err = _support.run_cli(["decide", "--question", "Track watering per bed or per steward?",
                                           "--options", "bed=Per bed,steward=Per steward", "--chosen", "bed",
                                           "--scope", "dataset:harvest-log", "--rationale", "beds change stewards"],
                                          root)
        self.assertEqual(code, 0, err)
        self.assertIn("decided dec-20260928-track-watering-per-bed-or-per-steward-", out)
        code, out, _err = _support.run_cli(["decisions", "--scope", "dataset:harvest-log", "--json"], root)
        decisions = json.loads(out)["decisions"]
        self.assertEqual([d["chosen"] for d in decisions], ["bed"])
        code, out, _err = _support.run_cli(["decisions", "--scope", ""], root)
        self.assertIn("an empty scope list matches none", out)
        code, out, _err = _support.run_cli(["decisions", "--json"], root)
        self.assertEqual(len(json.loads(out)["decisions"]), 2)
        code, out, _err = _support.run_cli(["decisions", "every", "morning"], root)
        self.assertIn("1 decision", out)
        code, out, err = _support.run_cli(["log", "--checkpoint", "--done", "asked about people",
                                           "--next", "ask about data,ingest the handbook", "--open-questions",
                                           "who owns the rain gauge?"], root)
        self.assertEqual(code, 0, err)
        self.assertIn("checkpoint chg-", out)
        last = ledger.last_checkpoint(store.Repo.open(root))
        self.assertEqual(last["next"], ["ask about data", "ingest the handbook"])
        code, out, _err = _support.run_cli(["log"], root)
        self.assertEqual(code, 0)
        for heading in ("2026-09-28", "  Done:", "  Decisions:", "  Delta:", "  Follow-ups:"):
            self.assertIn(heading, out)
        self.assertIn("next: ingest the handbook", out)
        code, _out, err = _support.run_cli(["log", "--checkpoint"], root)
        self.assertEqual(code, 2)
        code, out, _err = _support.run_cli(["log", "--limit", "2", "--json"], root)
        self.assertEqual(len(json.loads(out)["changes"]), 2)

    def test_migrate(self):
        root = self.mini()
        code, out, _err = _support.run_cli(["migrate", "--check"], root)
        self.assertEqual(code, 0)
        self.assertIn("up to date", out)
        repo = store.Repo.open(root)
        store.write_json(repo.path("ontology.json"), dict(repo.manifest, kit="0.0.9"))
        code, out, _err = _support.run_cli(["migrate", "--check"], root)
        self.assertIn("would run 1 step", out)
        code, out, _err = _support.run_cli(["migrate"], root)
        self.assertIn("stamp kit", out)
        self.assertEqual(store.Repo.open(root).manifest["kit"], __version__)
        store.write_json(repo.path("ontology.json"), dict(repo.manifest, format=9))
        code, _out, err = _support.run_cli(["migrate"], root)
        self.assertEqual(code, 1)
        self.assertIn("kit too old", err)

    def test_launcher_and_module_entry(self):
        root = self.mini()
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        launcher = os.path.join(_support.PLUGIN_DIR, "bin", "onto")
        for argv in ([sys.executable, launcher, "status", "--repo", root],
                     [sys.executable, "-m", "ontokit", "status", "--repo", root]):
            proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=_support.PLUGIN_DIR,
                                  env=env, stdin=subprocess.DEVNULL)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(proc.stdout.decode("utf-8").startswith("mini unreleased"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
