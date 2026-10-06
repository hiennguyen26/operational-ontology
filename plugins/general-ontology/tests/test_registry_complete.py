"""The registry is complete: every handler and renderer string in ``commands.COMMANDS`` imports, and the MCP profiles
hold 10 and 18 tools (SPEC E.2, D.7). This fails until every package module is built, by design: a missing module
must never hide behind "not built" once the kit ships."""

from __future__ import annotations

import importlib
import unittest

from tests import _support  # noqa: F401  (puts the plugin folder on sys.path)
from ontokit import commands

QUERY_TOOLS = ("onto_status", "onto_brief", "onto_context", "onto_card", "onto_get", "onto_search", "onto_neighbors",
               "onto_path", "onto_gaps", "onto_decisions")
FULL_ONLY = ("onto_next", "onto_answer", "onto_ingest", "onto_propose", "onto_review", "onto_apply", "onto_decide",
             "onto_import")
CLI_ONLY = ("init", "validate", "log", "migrate", "dupes", "eval", "erase", "pack", "tools", "build", "release",
            "scan", "bench", "setup", "doctor", "agents")
# D.9: the modules a handler or renderer may live in
MODULES = ("cmd_core", "queries", "answers", "ingest", "proposals", "evaluate", "interview", "richness", "compose",
           "build", "release", "bench", "onboard", "doctor", "agents")


def _target(spec: str):
    module, sep, attr = spec.partition(":")
    return module, attr, bool(sep)


class RegistryCompleteTest(unittest.TestCase):
    def test_every_handler_and_renderer_imports(self) -> None:
        missing = []
        for cmd in commands.COMMANDS:
            for what, spec in (("handler", cmd.handler), ("renderer", cmd.renderer)):
                module, attr, ok = _target(spec)
                self.assertTrue(ok and module.startswith("ontokit.") and attr, "%s %s: %r" % (cmd.name, what, spec))
                self.assertIn(module[len("ontokit."):], MODULES, "%s %s: %s" % (cmd.name, what, spec))
                try:
                    mod = importlib.import_module(module)
                except ImportError as exc:
                    missing.append("%s %s: %s (%s)" % (cmd.name, what, spec, exc))
                    continue
                if not callable(getattr(mod, attr, None)):
                    missing.append("%s %s: %s (no such function)" % (cmd.name, what, spec))
        self.assertEqual(missing, [], "not built:\n" + "\n".join(missing))

    def test_no_command_reads_as_not_built(self) -> None:
        """The kit's own lookup (the one ``dispatch`` turns into exit 3) finds every handler and renderer, and the
        CLI offers every command."""
        not_built = []
        for cmd in commands.COMMANDS:
            for spec in (cmd.handler, cmd.renderer):
                try:
                    commands._import(spec)
                except LookupError:
                    not_built.append("%s (%s)" % (cmd.name, spec))
            code, _out, err = _support.run_cli([cmd.name, "--help"])
            self.assertEqual(code, 0, "%s --help: %s" % (cmd.name, err))
        self.assertEqual(not_built, [])

    def test_tool_counts_are_10_and_18(self) -> None:
        query = [c.tool for c in commands.tools("query")]
        full = [c.tool for c in commands.tools("full")]
        self.assertEqual(len(query), 10)
        self.assertEqual(len(full), 18)
        self.assertEqual(sorted(query), sorted(QUERY_TOOLS))
        self.assertEqual(sorted(full), sorted(QUERY_TOOLS + FULL_ONLY))
        for cmd in commands.COMMANDS:
            if cmd.tool:
                self.assertEqual(cmd.tool, "onto_" + cmd.name)
                self.assertLessEqual(len(cmd.short), 300, cmd.name)

    def test_every_e2_command_is_registered_once(self) -> None:
        names = [c.name for c in commands.COMMANDS]
        self.assertEqual(len(names), len(set(names)))
        tools = {t[len("onto_"):] for t in QUERY_TOOLS + FULL_ONLY}
        self.assertEqual(set(names), tools | set(CLI_ONLY))
        self.assertEqual(len(names), 34)  # 33 + agents (harness spec H2)
        for cmd in commands.COMMANDS:
            if cmd.name in CLI_ONLY:
                self.assertIsNone(cmd.tool, cmd.name)
                self.assertEqual(cmd.profile, "cli", cmd.name)

    def test_write_tools_carry_their_annotations(self) -> None:
        writes = ("onto_answer", "onto_ingest", "onto_propose", "onto_apply", "onto_decide", "onto_import")
        for cmd in commands.tools("full"):
            self.assertEqual(cmd.annotations.get("readOnlyHint"), cmd.tool not in writes, cmd.name)
        self.assertTrue(commands.get("onto_apply").annotations.get("destructiveHint"))
        self.assertTrue(commands.get("onto_apply").annotations.get("idempotentHint"))
        self.assertTrue(commands.get("onto_import").annotations.get("destructiveHint"))
        self.assertEqual(sorted(c.name for c in commands.COMMANDS if c.confirm), ["answer", "apply", "import"])


if __name__ == "__main__":
    unittest.main()
