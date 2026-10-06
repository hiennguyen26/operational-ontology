"""Regression tests for the cross-group changes of the third fix round: the scan skips bytecode caches, P18 covers
the release notes and the change log, a P09 mismatch names the relations that fit, interview problem lines are never
cut, status says when its question was put off, and onto_propose pages a refusal over MCP while ``limit`` stays on
the CLI."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from tests import _support
from tests.test_core_regressions import NOTE, Base
from ontokit import cmd_core, commands, interview, mcp_server, secrets, validate
from ontokit.errors import Refused


class ScanCacheTest(unittest.TestCase):
    def test_a_bytecode_cache_is_not_scanned_but_its_source_is(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "pkg", "__pycache__"))
        key = _support.fake_secret("aws")
        with open(os.path.join(tmp, "pkg", "__pycache__", "mod.cpython-314.pyc"), "wb") as fh:
            fh.write(b"\x00\x01" + key.encode("ascii") + b"\x00")
        with open(os.path.join(tmp, "pkg", "mod.py"), "w", encoding="utf-8") as fh:
            fh.write("KEY = 'AK' + 'IA' + '...'\n")
        _count, hits = secrets.scan_paths([tmp])
        self.assertEqual(hits, [])
        with open(os.path.join(tmp, "pkg", "leak.txt"), "w", encoding="utf-8") as fh:
            fh.write(key + "\n")
        _count, hits = secrets.scan_paths([tmp])
        self.assertEqual([h[1] for h in hits], ["aws"])


class LedgerPersonalTest(Base):
    def test_release_notes_and_change_text_with_personal_data_are_p18(self):
        self.assertNotIn("P18", self.codes())
        versions = ("# Versions\n\n| Version | Date | Data | Nodes | Edges | Richness | Notes |\n"
                    "|---|---|---|---|---|---|---|\n"
                    "| v1 | 2026-09-28 | 0123456789ab | 3 | 2 | 11 seed | first cut \\| ask %s |\n"
                    % ("lee" + "@" + "gardenmail.net"))
        with open(self.repo.path("VERSIONS.md"), "w", encoding="utf-8") as fh:
            fh.write(versions)
        problems = [p for p in validate.validate(self.repo).problems if p.code == "P18"]
        self.assertEqual([(p.file, p.line) for p in problems], [("VERSIONS.md", 5)], problems)
        self.assertIn("email", problems[0].message)
        with open(self.repo.path("VERSIONS.md"), "w", encoding="utf-8") as fh:
            fh.write(versions.replace("lee" + "@" + "gardenmail.net", "[redacted:email]"))
        self.assertNotIn("P18", self.codes())

        path = self.repo.path("ledger/changes.jsonl")
        with open(path, "rb") as fh:
            before = fh.read()
        row = {"after": None, "at": "2026-09-28T12:00:00Z", "before": None, "by": "agent", "id": "chg-20260928-aaaaaa",
               "ids": [], "proposal": None, "source": NOTE, "summary": "checkpoint", "type": "checkpoint",
               "next": ["write to " + "lee" + "@" + "gardenmail.net"]}
        with open(path, "ab") as fh:
            fh.write((json.dumps(row, sort_keys=True) + "\n").encode("utf-8"))
        problems = [p for p in validate.validate(self.repo).problems if p.code == "P18"]
        self.assertEqual(len(problems), 1, problems)
        self.assertEqual(problems[0].file, "ledger/changes.jsonl")
        self.assertEqual(problems[0].line, before.count(b"\n") + 1)
        self.assertIn("chg-20260928-aaaaaa", problems[0].message)

    def test_the_demo_like_dates_and_hashes_of_a_row_are_not_personal_data(self):
        with open(self.repo.path("VERSIONS.md"), "w", encoding="utf-8") as fh:
            fh.write("| v1 | 2026-09-28 | 020794609581 | 3 | 2 | 11 seed | first release |\n")
        self.assertNotIn("P18", self.codes())


class FittingRelationsTest(Base):
    def test_a_p09_mismatch_lists_the_relations_that_link_the_two_kinds(self):
        prov = {"src": NOTE, "loc": "L11-L11", "by": "agent", "quote": "Mulch keeps the soil moist between waterings."}
        edge = {"src": "role:bed-steward", "rel": "works_on", "dst": "topic:mini"}
        with self.assertRaises(Refused) as ctx:
            self.propose([{"op": "add_edge", "edge": edge, "prov": [prov]}])
        problems = ctx.exception.extra.get("problems") or []
        messages = [p["message"] for p in problems if p.get("code") == "P09"]
        self.assertTrue(messages, problems)
        self.assertIn("relations that link a role to a topic: ", messages[0])
        self.assertIn("part_of", messages[0].split("relations that link a role to a topic: ", 1)[1])


class ProblemLineTest(unittest.TestCase):
    def test_an_answer_problem_line_keeps_the_whole_message(self):
        message = "works_on does not link a role to a topic (it links " + "x, " * 60 + "y)"
        line = interview._problem_line({"n": 2, "code": "P09", "message": message})
        self.assertTrue(line.endswith(message), line)
        self.assertFalse(line.endswith("..."))


class PutOffStepTest(Base):
    def test_status_says_its_question_was_put_off(self):
        q = {"id": "q.deepen.more", "ask": "Anything else?",
             "put_off": {"status": "later", "until": "2026-09-30T12:00:00Z"}}
        ctx = commands.Context(repo=self.repo, mcp=True)
        onto = self.onto()
        with mock.patch.object(interview, "next_questions", return_value=[q]):
            step = cmd_core._next_step(ctx, onto, {"count": 0}, {"stage": 3}, [])
            self.assertEqual(step["question"], "q.deepen.more")
            self.assertEqual(step["put_off"]["status"], "later")
            self.assertTrue(step["why"].startswith("put off (later) until 2026-09-30; ask only if"), step["why"])
            step = cmd_core._next_step(ctx, onto, {"count": 2}, {"stage": 3}, [])
            self.assertNotIn("question", step)
            self.assertIn("review", step["call"])


class ProposePagingTest(unittest.TestCase):
    def test_offset_is_offered_over_mcp_and_limit_only_on_the_cli(self):
        cmd = commands.get("propose")
        self.assertIn("limit", cmd.props)
        self.assertIn("offset", cmd.props)
        listed = mcp_server.published_schema(cmd)["properties"]
        self.assertIn("offset", listed)
        self.assertNotIn("limit", listed)


if __name__ == "__main__":
    unittest.main()
