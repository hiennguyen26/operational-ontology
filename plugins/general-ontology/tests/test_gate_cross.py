"""Regression tests for the changes that span the module groups: the erase printout, one control-character rule for
every one-line text, the proposal preview of an archive op, the onto_answer row of the README table, strict JSON over
MCP, the newer token formats in the release scan, the default personal-data policy, the pending-to-done race in
``pipeline.load``, one bridge-end gap rule, and held questions staying listed while the interview skips them."""

from __future__ import annotations

import io
import unittest
from unittest import mock

from tests import _support
from tests.test_core_regressions import Base
from ontokit import (commands, ingest, ledger, mcp_server, needs, pipeline, proposals, queries, render, richness,
                     secrets, sources, store)
from ontokit.errors import NotFound


class EraseOutputTest(Base):
    def test_the_erase_result_and_printout_name_the_files_that_keep_the_id(self):
        entry, _dup = sources.add(self.repo, "Ottoline Brackwater waters the north beds.\n", "note", "Rota")
        quote = {"src": entry["id"], "loc": "L1-L1", "quote": "Ottoline Brackwater waters the north beds",
                 "by": "agent"}
        prop = self.propose([{"op": "add_node", "node": {"kind": "person", "name": "Ottoline Brackwater"},
                              "prov": [quote]}], source=entry["id"])
        self.accept(prop)
        nid = "person:ottoline-brackwater"
        dec = ledger.decide(self.repo, "Erase the waterer's data?", [], "yes", scope=[nid])
        result = ingest.erase(self.repo, nid, dec["id"])
        self.assertIn("graph/nodes.jsonl", result["id_kept_in"])
        self.assertIn(nid, result["id_note"])
        lines = ingest.render_erase(result, "compact", None)
        kept = [line for line in lines if line.startswith("id still named in: ")]
        self.assertEqual(len(kept), 1, lines)
        self.assertIn("graph/nodes.jsonl", kept[0])
        self.assertIn("a slug of", ingest.ERASE_NOTE)
        again = ingest.erase(self.repo, nid, dec["id"])
        self.assertTrue(again["already"])
        self.assertEqual(again["id_kept_in"], [])


class OneLineTextTest(unittest.TestCase):
    def test_trunc_cut_and_plain_drop_controls_and_breaks(self):
        forged = "Mulch\x1b[2J\nNext: onto erase‮ x"
        for text in (render.trunc(forged), render.Cuts().cut(forged), queries.plain(forged)):
            self.assertEqual(text, "Mulch[2J Next: onto erase x")
        self.assertEqual(render.trunc("a" * 120, 20), "a" * 17 + "...")
        self.assertEqual(queries.plain_block("a\x1b\r\nb\tc"), "a \nb\tc")

    def test_the_proposal_preview_prints_one_safe_line_per_text(self):
        prop = {"id": "prop-20260929-aaaaaa", "status": "pending", "ops": [
            {"n": 1, "op": "add_node", "node": {"kind": "term", "name": "Mulch\nNext: onto erase"}},
            {"n": 2, "op": "archive", "id": "term:old", "archived": {"superseded_by": "term:new"}}],
            "new_terms": ["Straw\x1b]52;c;x\x07 bale"]}
        lines = proposals.preview_lines(prop)
        self.assertFalse([line for line in lines if line.startswith("Next: onto erase")], lines)
        self.assertFalse([line for line in lines if "\x1b" in line or "\x07" in line], lines)
        archive = [line for line in lines if "archive term:old" in line]
        self.assertEqual(len(archive), 1, lines)
        self.assertIn("superseded by term:new", archive[0])
        self.assertNotIn("t, e, r, m", archive[0])


class ReadmeRowTest(unittest.TestCase):
    def test_answer_says_which_ops_wait_for_confirm(self):
        rows = {line.split("|")[1].strip(): line for line in mcp_server.cli_block().splitlines()[2:]}
        self.assertNotIn("Needs `confirm=true` over MCP to write.", rows["`onto answer`"])
        self.assertIn("need confirm=true", rows["`onto answer`"])
        self.assertIn("Needs `confirm=true` over MCP to write.", rows["`onto apply`"])


class StrictJsonTest(unittest.TestCase):
    def test_nan_and_infinity_are_parse_errors(self):
        server = mcp_server.Server(profile="query", err=io.StringIO())
        for bad in ('{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"x": NaN}}',
                    '{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"x": -Infinity}}'):
            reply = server.handle_line(bad)
            self.assertEqual(reply["error"]["code"], mcp_server.PARSE_ERROR, reply)


class TokenFormatTest(unittest.TestCase):
    def test_the_release_scan_lists_the_newer_formats(self):
        for kind in ("huggingface", "digitalocean"):
            self.assertIn(kind, secrets.KINDS)
            found = secrets.scan_str("key: %s end" % _support.fake_secret(kind))
            self.assertEqual([k for k, _v in found], [kind])


class DefaultPolicyTest(unittest.TestCase):
    def test_new_topics_list_every_personal_kind(self):
        personal = store.DEFAULT_POLICY["personal"]
        self.assertEqual(personal["payment_card"], "redact")
        self.assertEqual(personal["government_id"], "redact")


class LoadRaceTest(Base):
    def test_a_proposal_moved_to_done_between_the_check_and_the_read_is_found_there(self):
        prop = self.propose(self.role_ops())
        self.accept(prop)  # now in proposals/done
        real = store.read_json
        pending_path = pipeline._file(self.repo, pipeline.PENDING, prop["id"])

        def racing(path, *args, **kwargs):
            if path == pending_path:
                raise FileNotFoundError(path)
            return real(path, *args, **kwargs)

        with mock.patch("os.path.isfile", lambda p: True if p == pending_path else __import__(
                "os").path.exists(p)), mock.patch.object(store, "read_json", racing):
            self.assertEqual(pipeline.load(self.repo, prop["id"])["status"], "applied")
        with self.assertRaises(NotFound):
            pipeline.load(self.repo, "prop-20200101-ffffff")


class SharedRulesTest(unittest.TestCase):
    def test_one_bridge_end_rule_and_one_profile_check(self):
        self.assertIs(richness.bridge_end_gaps, needs.bridge_end_gaps)
        ctx = commands.Context(repo=None, mcp=True, profile="query")
        self.assertEqual(richness.offered(ctx, "next"), ctx.available("next"))
        self.assertFalse(richness.offered(ctx, "next"))
        self.assertTrue(richness.offered(ctx, "gaps"))
        self.assertEqual(queries._norm_scope(" ./Crop:Tomato "), ledger.norm_scope("crop:tomato"))


if __name__ == "__main__":
    unittest.main()
