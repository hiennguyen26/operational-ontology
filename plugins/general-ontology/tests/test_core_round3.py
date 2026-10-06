"""Regression tests for the third core review round: a merge onto an archived edge, erase after out-of-order
applies, malformed JSON lines, a draft that re-asserts an untrusted edge, invisible format characters in names, the
release scan's credential rules, erase and decisions or checkpoints, source ends of imported edges, local pack
sharing, loose decision scopes, init on the template branch, inherited conflicts in gaps, the status Next line and
the ingest ``text`` argument."""

from __future__ import annotations

import json
import os

from tests import _support
from tests.test_graph import clear
from ontokit import graph, ledger, mutate, pipeline, store, validate
from ontokit.errors import Refused

NOTE = "src-000a61a61d03"  # the handbook excerpt of the mini fixture (12 lines, untrusted note)
MULCH = {"src": NOTE, "loc": "L11-L11", "quote": "Mulch keeps the soil moist between waterings.", "by": "agent"}
PESTS = {"src": NOTE, "loc": "L10-L10", "quote": "Companion planting keeps pests away from the tomato beds.",
         "by": "agent"}


def read(root, rel):
    with open(os.path.join(root, *rel.split("/")), "rb") as fh:
        return fh.read()


def write(root, rel, data):
    with open(os.path.join(root, *rel.split("/")), "wb") as fh:
        fh.write(data if isinstance(data, bytes) else data.encode("utf-8"))


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

    def codes(self):
        clear()
        return sorted({p.code for p in validate.validate(self.repo).problems})


# merge-archived-target-loses-edge --------------------------------------------------------------------------------
class MergeArchivedTargetTest(Base):
    def setup_merge(self):
        """term:mulch related_to term:companion-planting is archived under a decision; a newer term:mulching holds
        an active, confirmed related_to edge to the same node."""
        self.accept(self.propose([{"op": "add_node", "node": {"kind": "term", "name": "Mulch"}, "prov": [MULCH]}]))
        old = self.accept(self.propose([{"op": "add_edge", "edge": {"src": "term:mulch", "rel": "related_to",
                                                                    "dst": "term:companion-planting"},
                                         "prov": [PESTS]}]))
        old_edge = old["results"]["1"]["id"]
        dec = ledger.decide(self.repo, "Is mulch tied to companion planting?", ["yes=Yes", "no=No"], "no",
                            scope=["term:mulch"])
        self.accept(self.propose([
            {"op": "archive", "id": old_edge,
             "archived": {"reason": "mulch is not tied to companion planting", "decision": dec["id"]}},
            {"op": "add_node", "ref": "$m", "node": {"kind": "term", "name": "Mulching"}, "prov": [MULCH]},
            {"op": "add_edge", "edge": {"src": "$m", "rel": "related_to", "dst": "term:companion-planting"},
             "prov": [PESTS]},
        ]))
        onto = self.onto()
        new_edge = [e["edge"]["id"] for e in onto.edges_of("term:mulching")][0]
        self.assertEqual(onto.edges[new_edge]["status"], "confirmed")
        return old_edge, new_edge, dec["id"]

    def test_propose_refuses_a_merge_that_would_retire_an_active_edge(self):
        old_edge, new_edge, dec = self.setup_merge()
        with self.assertRaises(Refused) as ctx:
            self.propose([{"op": "merge", "keep": "term:mulch", "drop": "term:mulching",
                           "reason": "Mulching is the same term as mulch"}])
        problems = ctx.exception.problems
        self.assertEqual([p["code"] for p in problems], ["archived"], problems)
        self.assertIn(new_edge, problems[0]["message"])
        self.assertIn(old_edge, problems[0]["message"])
        self.assertIn(dec, problems[0]["message"])
        self.assertEqual(self.onto().edges[new_edge]["status"], "confirmed")

    def test_mutate_refuses_and_writes_nothing(self):
        old_edge, new_edge, _dec = self.setup_merge()
        before = read(self.root, "graph/edges.jsonl")
        with self.assertRaises(Refused) as ctx:
            mutate.apply_ops(self.repo, [{"n": 1, "op": "merge", "keep": "term:mulch", "drop": "term:mulching",
                                          "reason": "Mulching is the same term as mulch"}],
                             by="user", change_type="apply", summary="merge")
        self.assertIn("archived is final", ctx.exception.message)
        self.assertEqual(read(self.root, "graph/edges.jsonl"), before)

    def test_a_merge_without_an_archived_target_still_moves_every_edge(self):
        self.accept(self.propose([
            {"op": "add_node", "ref": "$a", "node": {"kind": "term", "name": "Mulch"}, "prov": [MULCH]},
            {"op": "add_node", "ref": "$b", "node": {"kind": "term", "name": "Mulching"}, "prov": [MULCH]},
            {"op": "add_edge", "edge": {"src": "$b", "rel": "related_to", "dst": "term:companion-planting"},
             "prov": [PESTS]},
        ]))
        done = self.accept(self.propose([{"op": "merge", "keep": "term:mulch", "drop": "term:mulching",
                                          "reason": "Mulching is the same term as mulch"}]))
        result = done["results"]["1"]
        self.assertEqual(len(result["moved"]), 1)
        onto = self.onto()
        moved = onto.edges[result["moved"][0]]
        self.assertEqual(moved["status"], "confirmed")
        self.assertEqual({moved["src"], moved["dst"]}, {"term:mulch", "term:companion-planting"})
        old = onto.edges[result["archived"][0]]
        self.assertEqual(old["archived"]["superseded_by"], [moved["id"]])


# erase-stale-assigned-id -------------------------------------------------------------------------------------------
class EraseStaleAssignedIdTest(Base):
    def setup_pair(self):
        """PA plans person:jordan-vale for "Jördan Välé", PB the same id for "Jordan Vale"; PB is applied first, so
        PA creates person:jordan-vale-2."""
        from ontokit import sources

        a, _dup = sources.add(self.repo, "Jördan Välé runs the watering rota.\n", "note", "Rota note")
        b, _dup = sources.add(self.repo, "Jordan Vale keeps the seed log up to date.\n", "note", "Seed note")
        qa = {"src": a["id"], "loc": "L1-L1", "quote": "Jördan Välé runs the watering rota.", "by": "agent"}
        qb = {"src": b["id"], "loc": "L1-L1", "quote": "Jordan Vale keeps the seed log up to date.", "by": "agent"}
        pa = self.propose([{"op": "add_node", "node": {"kind": "person", "name": "Jördan Välé",
                                                       "summary": "Runs the watering rota."}, "prov": [qa]}],
                          source=a["id"])
        pb = self.propose([{"op": "add_node", "node": {"kind": "person", "name": "Jordan Vale",
                                                       "summary": "Keeps the seed log."}, "prov": [qb]}],
                          source=b["id"])
        self.assertEqual(pa["ops"][0]["annot"]["assigned_id"], "person:jordan-vale")
        self.assertEqual(pb["ops"][0]["annot"]["assigned_id"], "person:jordan-vale")
        self.assertEqual(self.accept(pb)["results"]["1"]["id"], "person:jordan-vale")
        self.assertEqual(self.accept(pa)["results"]["1"]["id"], "person:jordan-vale-2")
        return pa["id"], pb["id"]

    def erase(self, nid):
        from ontokit import ingest

        dec = ledger.decide(self.repo, "Erase %s?" % nid, ["yes=Yes", "no=No"], "yes", scope=[nid])
        clear()
        return ingest.erase(self.repo, nid, dec["id"])

    def test_the_stored_proposal_names_the_node_it_created(self):
        pa, _pb = self.setup_pair()
        done = pipeline.load(self.repo, pa)
        self.assertEqual(done["ops"][0]["annot"]["assigned_id"], "person:jordan-vale-2")

    def test_erasing_the_first_node_keeps_the_other_proposal(self):
        pa, pb = self.setup_pair()
        self.erase("person:jordan-vale")
        text_a = json.dumps(pipeline.load(self.repo, pa), ensure_ascii=False)
        text_b = json.dumps(pipeline.load(self.repo, pb), ensure_ascii=False)
        self.assertIn("Jördan Välé", text_a)  # still an active node: its audit text stays
        self.assertIn("Runs the watering rota.", text_a)
        self.assertNotIn("Jordan Vale", text_b)
        self.assertNotIn("Keeps the seed log.", text_b)
        self.assertEqual(self.onto().nodes["person:jordan-vale-2"]["name"], "Jördan Välé")

    def test_erasing_the_second_node_scrubs_its_own_proposal(self):
        pa, pb = self.setup_pair()
        self.erase("person:jordan-vale-2")
        text_a = json.dumps(pipeline.load(self.repo, pa), ensure_ascii=False)
        text_b = json.dumps(pipeline.load(self.repo, pb), ensure_ascii=False)
        self.assertNotIn("Jördan Välé", text_a)
        self.assertNotIn("Runs the watering rota.", text_a)
        self.assertNotIn("watering rota", text_a)  # the quote left too
        self.assertIn("Jordan Vale", text_b)

    def test_old_proposals_with_a_stale_id_are_read_by_their_results(self):
        pa, pb = self.setup_pair()
        # a proposal stored before the fix: its op still names the planned id
        path = self.repo.path("proposals/done/%s.json" % pa)
        doc = store.read_json(path)
        doc["ops"][0]["annot"]["assigned_id"] = "person:jordan-vale"
        store.write_json(path, doc)
        self.erase("person:jordan-vale")
        text_a = json.dumps(pipeline.load(self.repo, pa), ensure_ascii=False)
        self.assertIn("Jördan Välé", text_a)
        self.assertNotIn("Jordan Vale", json.dumps(pipeline.load(self.repo, pb), ensure_ascii=False))


# malformed-json-lines-crash ----------------------------------------------------------------------------------------
class MalformedLinesTest(Base):
    LEAD = '"name":"Volunteer lead"'

    def corrupt(self, old, new):
        text = read(self.root, "graph/nodes.jsonl").decode("utf-8")
        self.assertIn(old, text)
        write(self.root, "graph/nodes.jsonl", text.replace(old, new, 1))
        clear()
        return read(self.root, "graph/nodes.jsonl")

    def mint(self):
        return [{"op": "add_node", "node": {"kind": "term", "name": "Mulch"}, "prov": [MULCH]}]

    def assert_nothing_rewrites(self, before):
        from ontokit import entities, needs

        code, out, err = _support.run_cli(["validate", "--fix"], repo=self.root)
        self.assertEqual(code, 1, out + err)
        self.assertNotIn("internal error", out + err)
        self.assertEqual(read(self.root, "graph/nodes.jsonl"), before)
        with self.assertRaises(Refused):
            self.propose(self.mint())
        self.assertEqual(read(self.root, "graph/nodes.jsonl"), before)
        onto = self.onto()
        needs.all_needs(onto)
        entities.duplicates(onto)
        for args in (["gaps"], ["dupes"], ["search", "volunteer"], ["get", "person:volunteer-lead"], ["status"],
                     ["build"]):
            code, out, err = _support.run_cli(args, repo=self.root)
            self.assertNotIn("internal error", out + err, args)

    def test_a_lone_surrogate_is_p01_and_nothing_crashes(self):
        before = self.corrupt(self.LEAD, '"name":"Volunteer lead \\ud83c"')
        problems = [p for p in validate.validate(self.repo).problems if p.code == "P01"]
        self.assertEqual([(p.file, p.line) for p in problems], [("graph/nodes.jsonl", 5)])
        self.assertIn("U+D83C", problems[0].message)
        self.assert_nothing_rewrites(before)

    def test_an_escaped_surrogate_pair_is_fine(self):
        self.corrupt(self.LEAD, '"name":"Volunteer lead \\ud83c\\udf45"')
        self.assertNotIn("P01", self.codes())
        self.assertEqual(self.onto().nodes["person:volunteer-lead"]["name"], "Volunteer lead \U0001F345")

    def test_a_duplicate_key_is_p01_and_no_value_is_dropped(self):
        before = self.corrupt(self.LEAD + ",", '"name":"Sweet lead",' + self.LEAD + ",")
        problems = [p for p in validate.validate(self.repo).problems if p.code == "P01"]
        self.assertEqual(len(problems), 1)
        self.assertIn('"name" appears twice', problems[0].message)
        self.assert_nothing_rewrites(before)
        self.assertIn(b"Sweet lead", read(self.root, "graph/nodes.jsonl"))

    def test_a_duplicate_key_in_a_json_file_is_a_data_error(self):
        from ontokit.errors import DataError

        path = self.repo.path("packs/local.pack.json")
        write(self.root, "packs/local.pack.json", '{"kinds":{},"kinds":{}}\n')
        with self.assertRaises(DataError):
            store.read_json(path)

    def test_non_list_aliases_and_prov_are_p02_and_nothing_crashes(self):
        for field, value in (("aliases", "5"), ("prov", "7"), ("gaps", "true"), ("attrs", "[]")):
            with self.subTest(field=field):
                text = read(self.root, "graph/nodes.jsonl").decode("utf-8")
                rows = text.splitlines(True)
                row = json.loads(rows[4])
                self.assertEqual(row["id"], "person:volunteer-lead")
                raw = rows[4].replace('"%s":%s' % (field, json.dumps(row[field], ensure_ascii=False,
                                                                          separators=(",", ":"))),
                                      '"%s":%s' % (field, value), 1)
                self.assertNotEqual(raw, rows[4])
                before = self.corrupt(rows[4], raw)
                problems = [p for p in validate.validate(self.repo).problems if p.code == "P02"]
                self.assertTrue(any(field in p.message and p.line == 5 for p in problems), problems)
                self.assert_nothing_rewrites(before)
                write(self.root, "graph/nodes.jsonl", text)
                clear()

    def test_a_lone_surrogate_in_proposal_text_is_p02(self):
        with self.assertRaises(Refused) as ctx:
            self.propose([{"op": "add_node", "node": {"kind": "term", "name": "Mint \ud83c"}, "prov": [MULCH]}])
        self.assertEqual([p["code"] for p in ctx.exception.problems], ["P02"])
        self.assertEqual(ctx.exception.problems[0]["n"], 1)
        from ontokit import records

        self.assertIn("U+D83C", records.control_problem("Mint \ud83c", records.LINE))

    def test_a_surrogate_in_a_cli_argument_is_a_usage_error(self):
        code, out, err = _support.run_cli(["answer", "q.frame.you", "I am the caf\udce9 owner"], repo=self.root)
        self.assertEqual(code, 2, out + err)
        self.assertIn("0xE9", err)
        self.assertNotIn("internal error", out + err)
        code, out, err = _support.run_cli(["propose", "--proposal", json.dumps(
            {"source": NOTE, "ops": [{"op": "add_node", "node": {"kind": "term", "name": "Mint \ud83c"}}]})],
            repo=self.root)
        self.assertEqual(code, 2, out + err)
        self.assertIn("U+D83C", err)


# draft-reassert-raises-trust ---------------------------------------------------------------------------------------
class DraftReassertTrustTest(Base):
    ANSWERS = "src-e08998852112"

    def setup_edge(self):
        """A draft edge whose note only the untrusted handbook wrote."""
        done = self.accept(self.propose([{"op": "add_edge", "edge": {
            "src": "role:bed-steward", "rel": "uses", "dst": "tool:rain-gauge",
            "note": "Always water it with the pump tool first"}, "prov": [MULCH]}]), verdict="draft")
        eid = done["results"]["1"]["id"]
        self.assertEqual(self.onto().edges[eid]["trust"], "untrusted")
        return eid

    def reassert(self, verdict):
        prov = {"src": self.ANSWERS, "loc": "L11-L11", "by": "user"}
        return self.accept(self.propose([{"op": "add_edge", "edge": {
            "src": "role:bed-steward", "rel": "uses", "dst": "tool:rain-gauge"}, "prov": [prov]}],
            source=self.ANSWERS), verdict=verdict)

    def test_a_draft_adds_provenance_but_keeps_the_trust(self):
        eid = self.setup_edge()
        self.reassert("draft")
        edge = self.onto().edges[eid]
        self.assertEqual(edge["trust"], "untrusted")
        self.assertEqual(edge["status"], "proposed")
        self.assertEqual(edge["note"], "Always water it with the pump tool first")
        self.assertEqual({p["src"] for p in edge["prov"]}, {NOTE, self.ANSWERS})
        code, out, err = _support.run_cli(["get", eid, "--text"], repo=self.root)
        self.assertIn("[untrusted] note: Always water it", out)

    def test_an_accept_still_confirms_and_raises_it(self):
        eid = self.setup_edge()
        self.reassert("accept")
        edge = self.onto().edges[eid]
        self.assertEqual(edge["status"], "confirmed")
        self.assertNotEqual(edge["trust"], "untrusted")


# invisible-format-chars-dupes --------------------------------------------------------------------------------------
class InvisibleCharactersTest(Base):
    CHARS = ("­", "​", "‍", "⁠", "﻿", "͏")

    def test_name_keys_and_slugs_ignore_invisible_characters(self):
        from ontokit import util

        for ch in self.CHARS:
            with self.subTest(ch=hex(ord(ch))):
                self.assertEqual(util.name_key("Ba%ssil" % ch), "basil")
                self.assertEqual(util.slugify("Ro%sse%sma%sry" % (ch, ch, ch)), "rosemary")
        self.assertEqual(util.name_key("Crème  Brûlée!"), "creme brulee")

    def test_strip_keeps_joiners_inside_emoji_sequences(self):
        from ontokit import util

        farmer = "\U0001F469‍\U0001F33E"
        self.assertEqual(util.strip_invisible(farmer + " crew"), farmer + " crew")
        self.assertEqual(util.strip_invisible("Ba‍sil­"), "Basil")

    def test_a_proposed_name_loses_them_and_matches_the_existing_node(self):
        self.accept(self.propose([{"op": "add_node", "node": {"kind": "term", "name": "Mulch"}, "prov": [MULCH]}]))
        prop = self.propose([{"op": "add_node", "node": {"kind": "term", "name": "Mul­ch",
                                                         "aliases": ["Wood​ chips"]}, "prov": [MULCH]}])
        node = prop["ops"][0]["node"]
        self.assertEqual(node["name"], "Mulch")
        self.assertEqual(node["aliases"], ["Wood chips"])
        codes = [w["code"] for w in prop["checks"]["warnings"]]
        self.assertIn("invisible", codes)
        self.assertIn("match", codes)
        self.assertEqual(prop["ops"][0]["annot"]["matches"][0]["id"], "term:mulch")

    def test_dupes_find_a_stored_name_with_a_soft_hyphen(self):
        from ontokit import entities

        self.accept(self.propose([{"op": "add_node", "node": {"kind": "term", "name": "Mulch"}, "prov": [MULCH]}]))
        text = read(self.root, "graph/nodes.jsonl").decode("utf-8")
        row = json.loads([ln for ln in text.splitlines() if '"id":"term:mulch"' in ln][0])
        twin = dict(row, id="term:mul-ch", name="Mul­ch")
        rows = [json.loads(ln) for ln in text.splitlines()] + [twin]
        store.write_jsonl(self.repo.path("graph/nodes.jsonl"), sorted(rows, key=lambda r: r["id"]))
        onto = self.onto()
        pairs = entities.duplicates(onto)
        self.assertTrue(any({p.get("a"), p.get("b")} == {"term:mulch", "term:mul-ch"} for p in pairs), pairs)


# release-scan-weak-credentials -------------------------------------------------------------------------------------
B64 = "".join(chr(65 + (i * 7) % 26) for i in range(64))


def armor(label):
    """A key block with ``label`` (assembled here, so no key header sits in the source)."""
    return "-----BEGIN %s-----\n\n%s-----END %s-----\n" % (label, (B64 + "\n") * 4, label)


class ReleaseScanCredentialsTest(_support.TempCase):
    def put(self, rel, text):
        path = os.path.join(self.tmp, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_every_private_key_form_is_found(self):
        from ontokit import secrets

        forms = {
            "pgp": armor("PGP " + "PRIVATE KEY BLOCK"),
            "pem": armor("RSA " + "PRIVATE KEY"),
            "encrypted": armor("ENCRYPTED " + "PRIVATE KEY"),
            "openssh": armor("OPENSSH " + "PRIVATE KEY"),
            "ssh2": "---- BEGIN SSH2 ENCRYPTED " + "PRIVATE KEY ----\n" + B64 + "\n",
            "putty": "PuTTY-User-" + "Key-File-3: ssh-ed25519\nEncryption: none\n",
        }
        for name, text in forms.items():
            with self.subTest(form=name):
                self.assertIn("private_key", [k for k, _m in secrets.scan_str(text)])
                self.assertIn("private_key", [k for k, _m in secrets.scan_bytes(text.encode("ascii"))])
        self.assertEqual(secrets.scan_str("the private key lives in the tool shed"), [])

    def setup_text(self):
        return ("DB_PASS" + "WORD=S3cretPass99\n"
                "DATABASE_URL=postgres://admin:S3cret" + "Pass99@db.internal.invalid:5432/garden\n"
                "Authorization: Bearer 8f3a9c2d" + "7e6b5a4f3e2d1c0b9a8f7e6d\n")

    def test_the_file_scan_runs_the_sanitizer_credential_rules(self):
        from ontokit import secrets

        path = self.put("topic/SETUP.md", self.setup_text())
        self.put("topic/notes.md", "Water the beds before nine. Call the plot coordinator at noon.\n")
        _count, hits = secrets.scan_paths([os.path.join(self.tmp, "topic")])
        kinds = sorted({kind for name, kind, _p in hits if name == path})
        self.assertEqual(kinds, ["auth_header", "credential", "url_credentials"])
        self.assertEqual([h for h in hits if h[0] != path], [])
        self.assertTrue(all(prefix == "" for _n, _k, prefix in hits))  # the kind only, never the value
        code, out, err = _support.run_cli(["scan", os.path.join(self.tmp, "topic")])
        self.assertEqual(code, 2, out + err)
        self.assertIn("credential", out)
        self.assertNotIn("S3cret", out + err)

    def test_a_copy_of_the_kit_keeps_only_the_patterns(self):
        from ontokit import secrets

        self.put("topic/plugins/general-ontology/ontokit/__init__.py", "")
        self.put("topic/plugins/general-ontology/bin/onto", "")
        example = self.put("topic/plugins/general-ontology/tests/test_x.py", self.setup_text())
        key = self.put("topic/plugins/general-ontology/tests/key.txt", armor("PGP " + "PRIVATE KEY BLOCK"))
        _count, hits = secrets.scan_paths([os.path.join(self.tmp, "topic")])
        self.assertEqual([h for h in hits if h[0] == example], [])
        self.assertIn("private_key", [k for name, k, _p in hits if name == key])

    def test_the_kit_itself_scans_clean(self):
        from ontokit import secrets

        _count, hits = secrets.scan_paths([_support.PLUGIN_DIR])
        self.assertEqual(hits, [])


# erase-name-in-decisions-checkpoints -------------------------------------------------------------------------------
class EraseLedgerNamesTest(Base):
    def test_erase_scrubs_decisions_and_checkpoints_in_one_write(self):
        from ontokit import ingest, sources

        src, _dup = sources.add(self.repo, "Sam Oduya holds the shed key.\n", "note", "Key note")
        quote = {"src": src["id"], "loc": "L1-L1", "quote": "Sam Oduya holds the shed key.", "by": "agent"}
        self.accept(self.propose([{"op": "add_node", "node": {"kind": "person", "name": "Sam Oduya",
                                                              "aliases": ["Sammy O"]}, "prov": [quote]}],
                                 source=src["id"]))
        key = ledger.decide(self.repo, "Who holds the spare shed key?", ["sam=Sam Oduya", "none=Nobody"], "sam",
                            rationale="Sam Oduya is there every morning; sammy o said yes.", scope=["mini/"])
        free = ledger.decide(self.repo, "Who opens the gate on Sundays?", [], "Sam Oduya, until spring",
                             scope=["mini/"])
        ledger.checkpoint(self.repo, ["people"], ["confirm the key rota with Sam Oduya"], ["Does SAM ODUYA ride?"])
        dec = ledger.decide(self.repo, "Erase the person record?", ["yes=Yes", "no=No"], "yes",
                            scope=["person:sam-oduya"])
        clear()
        before_other = read(self.root, "ledger/decisions/%s.json" % dec["id"])
        ingest.erase(self.repo, "person:sam-oduya", dec["id"])
        self.assertNotIn(b"Oduya", read(self.root, "ledger/decisions/%s.json" % key["id"]))
        self.assertNotIn(b"Oduya", read(self.root, "ledger/decisions/%s.json" % free["id"]))
        self.assertNotIn(b"ammy O", read(self.root, "ledger/decisions/%s.json" % key["id"]))
        self.assertEqual(read(self.root, "ledger/decisions/%s.json" % dec["id"]), before_other)
        changes = read(self.root, "ledger/changes.jsonl")
        self.assertNotIn(b"Oduya", changes)
        self.assertNotIn(b"ODUYA", changes)
        self.assertEqual(ledger.last_checkpoint(self.repo)["next"], ["confirm the key rota with [erased]"])
        self.assertEqual(ledger.load_decision(self.repo, key["id"])["options"][0]["label"], "[erased]")
        self.assertEqual(ledger.load_decision(self.repo, key["id"])["options"][0]["id"], "sam")  # ids stay
        rows, bad = store.read_jsonl(self.repo.path("ledger/changes.jsonl"))
        self.assertEqual(bad, [])
        self.assertEqual(rows[-1]["type"], "erase")  # the erase change is appended after the rewrite
        self.assertIsNone(store.pending_intent(self.repo))
        self.assertEqual([p for p in validate.validate(self.repo).problems], [])

    def test_the_erase_result_lists_the_scrubbed_ledger_records(self):
        self.accept(self.propose([{"op": "add_node", "node": {"kind": "person", "name": "Sam Oduya"},
                                   "prov": [MULCH]}]))
        key = ledger.decide(self.repo, "Who holds the spare shed key?", ["sam=Sam Oduya", "none=Nobody"], "sam")
        dec = ledger.decide(self.repo, "Erase the person record?", ["yes=Yes", "no=No"], "yes",
                            scope=["person:sam-oduya"])
        clear()
        out = mutate.apply_ops(self.repo, [{"n": 1, "op": "erase_node", "id": "person:sam-oduya",
                                            "decision": dec["id"]}], by="user", change_type="erase", summary="erase")
        scrubbed = out["results"]["1"]["names_scrubbed_in"]
        self.assertIn(key["id"], scrubbed)
        self.assertTrue(any(c.startswith("chg-") for c in scrubbed))  # the decide change's summary

    def test_recovery_undoes_the_append_then_the_rewrite(self):
        log = self.repo.path("ledger/changes.jsonl")
        old = read(self.root, "ledger/changes.jsonl")
        new = old.replace(b"steward", b"[erased]")
        self.assertNotEqual(new, old)
        store.begin_write(self.repo, [("ledger/changes.jsonl", old, new)], [("ledger/changes.jsonl", new)])
        with open(log, "wb") as fh:
            fh.write(new)
        store.append_jsonl(log, {"id": "chg-x"})  # the process died after the append (the intent recorded it)
        store._ACTIVE.clear()  # a killed process keeps nothing in memory
        store.recover(self.repo)
        self.assertEqual(read(self.root, "ledger/changes.jsonl"), old)


# source-end-edges-dangle-in-importers ------------------------------------------------------------------------------
class ImportedSourceEndTest(_support.TempCase):
    SRC_A = "src-" + "a1" * 6
    SRC_B = "src-" + "b2" * 6

    def entry(self, sid, title):
        return {"id": sid, "kind": "note", "title": title, "sha256": "0" * 64, "bytes": 10,
                "captured_at": "2026-09-28T12:00:00Z", "url": None}

    def setUp(self):
        super().setUp()
        from tests.test_graph import add_import, make_export, mk_edge, mk_node

        clear()
        self.root = _support.bare_topic(self.tmp, ns="coop")
        self.repo = store.Repo.open(self.root)
        seed = mk_node("dataset:seed-list", "Seed list")
        scanner = mk_node("tool:scanner", "Scanner")
        tiny = make_export("tiny", [seed, scanner], [mk_edge("dataset:seed-list", "derived_from", self.SRC_A),
                                                     mk_edge(self.SRC_A, "refresh_with", "tool:scanner")],
                           sources=[self.entry(self.SRC_A, "A note")])
        # a composed topic's bridge to its own source, bundled with tiny
        tu = make_export("tu", [], [mk_edge("tiny/dataset:seed-list", "derived_from", self.SRC_B)],
                         sources=[self.entry(self.SRC_B, "B note")])
        add_import(self.repo, "tiny", tiny)
        add_import(self.repo, "tu", tu)
        clear()

    def test_the_source_end_is_a_node(self):
        from ontokit import needs

        onto = graph.Ontology.load(self.repo)
        for qid, title in (("tiny/" + self.SRC_A, "A note"), ("tu/" + self.SRC_B, "B note")):
            self.assertIn(qid, onto.nodes)
            self.assertIn(qid, onto.virtual)
            self.assertEqual(onto.kind_of(qid), "source")
            self.assertEqual(onto.nodes[qid]["name"], title)
            self.assertFalse(onto.is_local(qid))
        others = sorted(e["other"] for e in onto.edges_of("tiny/dataset:seed-list"))
        self.assertEqual(others, ["tiny/" + self.SRC_A, "tu/" + self.SRC_B])
        self.assertEqual([e["other"] for e in onto.edges_of("tiny/tool:scanner")], ["tiny/" + self.SRC_A])
        self.assertEqual(needs.inherited_bridge_gaps(onto), [])
        self.assertNotIn("W04", {p.code for p in validate.validate(self.repo).warnings})

    def test_neighbors_and_gaps_do_not_say_absent_upstream(self):
        code, out, err = _support.run_cli(["neighbors", "tiny/dataset:seed-list"], repo=self.root)
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("absent upstream", out)
        self.assertIn("(source=2)", out)
        code, out, err = _support.run_cli(["gaps", "--limit", "0"], repo=self.root)
        self.assertNotIn("dangling_bridge", out)
        code, out, err = _support.run_cli(["get", "tiny/" + self.SRC_A], repo=self.root)
        self.assertNotIn("internal error", out + err)


# local-pack-sharing-flips-kinds ------------------------------------------------------------------------------------
class LocalPackSharingTest(_support.TempCase):
    def pack(self, **extra_kinds):
        from tests.test_graph import GARDEN_PACK
        import copy

        out = copy.deepcopy(GARDEN_PACK)
        out["kinds"].update(extra_kinds)
        return out

    def setup_pair(self, parent_pack, child_pack, rel="grown_in"):
        from tests.test_graph import add_import, make_export, mk_edge, mk_node, write_topic

        clear()
        beans = mk_node("crop:beans", "Beans")
        north = mk_node("plot:north-field", "North field")
        export = make_export("fieldp", [beans, north], [mk_edge("crop:beans", "grown_in", "plot:north-field")],
                             local_pack=parent_pack)
        east = mk_node("plot:east-bed", "East bed")
        bridge = mk_edge("fieldp/crop:beans", rel, "plot:east-bed")
        repo = write_topic(self.tmp, [east], [bridge], ns="fieldc", local_pack=child_pack)
        add_import(repo, "fieldp", export)
        clear()
        return repo, bridge["id"]

    def p09(self, repo):
        clear()
        return [p for p in validate.validate(repo).problems if p.code == "P09"]

    def test_identical_packs_share(self):
        repo, _bridge = self.setup_pair(self.pack(), self.pack())
        self.assertEqual(graph.Ontology.load(repo).kind_of("fieldp/crop:beans"), "crop")
        self.assertEqual(self.p09(repo), [])

    def test_an_additive_change_on_either_side_keeps_the_kinds_shared(self):
        shed = {"label": "Shed", "plural": "sheds", "dimension": "data"}
        barn = {"label": "Barn", "plural": "barns", "dimension": "data"}
        variety = self.pack()
        variety["kinds"]["crop"]["fields"]["variety"] = {"type": "string"}
        for parent, child in ((self.pack(barn=barn), self.pack()), (self.pack(), self.pack(shed=shed)),
                              (variety, self.pack())):
            with self.subTest(parent=sorted(parent["kinds"]), child=sorted(child["kinds"])):
                repo, _bridge = self.setup_pair(parent, child)
                onto = graph.Ontology.load(repo)
                self.assertEqual(onto.kind_of("fieldp/crop:beans"), "crop")
                self.assertEqual(self.p09(repo), [])
                import shutil

                shutil.rmtree(repo.root)

    def test_a_pack_change_that_would_break_a_bridge_is_refused(self):
        parent = self.pack()
        parent["kinds"]["crop"]["fields"]["variety"] = {"type": "string"}
        child = self.pack()
        child["relations"]["planted_in"] = {"inverse": "planted_with", "from": ["crop"], "to": ["plot"]}
        repo, bridge = self.setup_pair(parent, child, rel="planted_in")
        self.assertEqual(self.p09(repo), [])
        before = read(repo.root, "packs/local.pack.json")
        with self.assertRaises(Refused) as ctx:
            mutate.apply_ops(repo, [{"n": 1, "op": "add_field", "kind": "crop", "field": "variety",
                                     "schema": {"type": "integer"}}], by="user", change_type="apply",
                             summary="pack")
        self.assertIn("P09", ctx.exception.message)
        self.assertIn(bridge, ctx.exception.message)
        self.assertEqual(read(repo.root, "packs/local.pack.json"), before)


# decision-scope-no-resolve -----------------------------------------------------------------------------------------
class DecisionScopeResolveTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        from tests.test_graph import add_import, make_export, mk_node

        clear()
        self.root = _support.bare_topic(self.tmp, ns="g2t")
        self.repo = store.Repo.open(self.root)
        menu = mk_node("deliverable:weekly-menu", "Weekly menu")
        add_import(self.repo, "kitchen", make_export("kitchen", [menu]))
        clear()

    def cli(self, *args):
        code, out, err = _support.run_cli(list(args) + ["--json"], repo=self.root)
        self.assertEqual(code, 0, out + err)
        return json.loads(out)

    def test_decide_stores_the_resolved_id(self):
        out = self.cli("decide", "--question", "Who posts the weekly menu?", "--options", "cook=Cook,plan=Planner",
                       "--chosen", "plan", "--scope", "weekly-menu", "--scope", "kitchen/", "--scope", "pricing",
                       "--scope", "note:nothing-here")
        self.assertEqual(out["decision"]["scope"],
                         ["kitchen/deliverable:weekly-menu", "kitchen/", "pricing", "note:nothing-here"])
        self.assertEqual(out["scope_resolved"], [{"given": "weekly-menu", "id": "kitchen/deliverable:weekly-menu"}])
        self.assertEqual(out["scope_unknown"], ["note:nothing-here"])
        brief = _support.run_cli(["brief", "kitchen/deliverable:weekly-menu"], repo=self.root)[1]
        self.assertIn(out["decision"]["id"], brief)

    def test_decisions_reads_loose_ids(self):
        dec = ledger.decide(self.repo, "When does the weekly menu lock?", [], "Thursday noon",
                            scope=["kitchen/deliverable:weekly-menu"])
        for given in ("deliverable:weekly-menu", "weekly-menu", "kitchen/deliverable:weekly-menu", "kitchen/"):
            with self.subTest(scope=given):
                out = self.cli("decisions", "--scope", given)
                self.assertEqual([d["id"] for d in out["decisions"]], [dec["id"]])
        out = self.cli("decisions", "--scope", "note:nothing-here")
        self.assertEqual(out["decisions"], [])
        self.assertEqual(out["scope_unknown"], ["note:nothing-here"])
        text = _support.run_cli(["decisions", "--scope", "note:nothing-here"], repo=self.root)[1]
        self.assertIn("not in the ontology", text)

    def test_a_namespace_stays_a_namespace(self):
        out = self.cli("decide", "--question", "Is the kitchen import pinned?", "--chosen", "yes", "--scope",
                       "kitchen", "--scope", "g2t")
        self.assertEqual(out["decision"]["scope"], ["kitchen", "g2t"])


# init-on-template-branch -------------------------------------------------------------------------------------------
class InitOnTemplateBranchTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        self.clone = os.path.join(self.tmp, "clone")
        _support.git_init(self.clone)
        kit = os.path.join(self.clone, "plugins", "general-ontology", "ontokit")
        os.makedirs(kit)
        with open(os.path.join(kit, "__init__.py"), "w") as fh:
            fh.write("")
        _support.commit_all(self.clone, "kit")
        _support.git(self.clone, "branch", "-m", "general-ontology")

    def init(self):
        return _support.run_cli(["init", "--name", "bees", "--ns", "bees", "--title", "Bees", "--path", self.clone])

    def test_init_stops_on_the_template_branch(self):
        before = _support.snapshot(self.clone, skip=[".git"])
        code, out, err = self.init()
        self.assertEqual(code, 1, out + err)
        self.assertIn("template branch general-ontology", err)
        self.assertIn("git remote rename origin kit", err)
        self.assertEqual(_support.snapshot(self.clone, skip=[".git"]), before)

    def test_init_stops_while_origin_is_the_template(self):
        _support.git(self.clone, "checkout", "-q", "-b", "main")
        _support.git(self.clone, "remote", "add", "origin", "https://example.invalid/kit.git")
        _support.git(self.clone, "update-ref", "refs/remotes/origin/general-ontology", "HEAD")
        code, out, err = self.init()
        self.assertEqual(code, 1, out + err)
        self.assertIn("origin is still the template", err)
        self.assertFalse(os.path.exists(os.path.join(self.clone, "ontology.json")))
        _support.git(self.clone, "remote", "rename", "origin", "kit")
        code, out, err = self.init()
        self.assertEqual(code, 0, out + err)
        self.assertTrue(os.path.exists(os.path.join(self.clone, "ontology.json")))

    def test_init_outside_a_template_clone_is_unchanged(self):
        plain = os.path.join(self.tmp, "plain")
        _support.git_init(plain)
        code, out, err = _support.run_cli(["init", "--name", "bees", "--ns", "bees", "--title", "Bees", "--path",
                                           plain])
        self.assertEqual(code, 0, out + err)


# inherited-conflict-missing-from-gaps ------------------------------------------------------------------------------
class InheritedConflictTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        from tests.test_graph import add_import, make_export, mk_edge, mk_node

        clear()
        self.root = _support.bare_topic(self.tmp, ns="coop")
        self.repo = store.Repo.open(self.root)
        hive = mk_node("dataset:honey-log", "Honey log", attrs={"format": "paper"})
        bakery = mk_node("dataset:honey-log", "Honey log", attrs={"format": "sheet"})
        add_import(self.repo, "hive", make_export("hive", [hive]))
        add_import(self.repo, "bakery", make_export("bakery", [bakery]))
        bridge = mk_edge("hive/dataset:honey-log", "same_as", "bakery/dataset:honey-log", symmetric=True)
        add_import(self.repo, "hb", make_export("hb", [], [bridge]))
        clear()

    def conflicts(self, args):
        code, out, err = _support.run_cli(args, repo=self.root)
        self.assertEqual(code, 0, out + err)
        return [line for line in out.splitlines() if "conflict" in line]

    def test_gaps_and_next_list_the_conflict_brief_shows(self):
        from ontokit import needs

        onto = graph.Ontology.load(self.repo)
        found = [(nid, g) for nid, need in needs.bridge_end_gaps(onto) for g in need["gaps"]]
        self.assertTrue(found)
        self.assertTrue(all(g["type"] == "conflict" and g["owner"] == "hb" for _nid, g in found), found)
        self.assertTrue(self.conflicts(["gaps", "--limit", "0"]))
        self.assertTrue(self.conflicts(["next", "--n", "40"]))
        self.assertTrue(self.conflicts(["brief", "honey log"]))

    def test_a_local_decision_settles_it_everywhere(self):
        ledger.decide(self.repo, "Which honey log format holds?", ["paper=Paper", "sheet=Sheet"], "paper",
                      scope=["hive/dataset:honey-log#attrs.format"])
        clear()
        self.assertEqual(self.conflicts(["gaps", "--limit", "0"]), [])
        self.assertEqual(self.conflicts(["next", "--n", "40"]), [])


# status-next-review-pointer-cut ------------------------------------------------------------------------------------
class StatusReviewPointerTest(Base):
    LONG = ("What is the first thing you want to produce for Mini garden, and who will read or use it, and when "
            "do they need it?")

    def test_the_review_pointer_survives_a_long_question(self):
        from unittest import mock
        from ontokit import interview

        self.assertEqual(len(pipeline.pending(self.repo)), 1)
        fake = [{"id": "q.frame.deliverable", "ask": self.LONG}]
        with mock.patch.object(interview, "next_questions", lambda onto, n=3, stage=None: fake), \
                mock.patch.object(interview, "progress", lambda onto: {"stage": 0, "stages": []}):
            for fmt in ([], ["--text"]):
                code, out, err = _support.run_cli(["status"] + fmt, repo=self.root)
                self.assertEqual(code, 0, out + err)
                self.assertIn("Then: review 1 pending proposal: onto review", out)
                nxt = [line for line in out.splitlines() if line.startswith("Next: ")][0]
                self.assertNotIn("then review", nxt)
            code, out, err = _support.run_cli(["status", "--json"], repo=self.root)
            nxt = json.loads(out)["next"]
            self.assertTrue(nxt["why"].endswith("; then review 1 pending proposal"))
            self.assertEqual(nxt["then"], "review 1 pending proposal: onto review")
            import io
            from ontokit import mcp_server

            server = mcp_server.Server("full", err=io.StringIO(), repo=self.root, cwd=self.tmp, env={})
            reply = _support.mcp_call(server, "onto_status")
            text = "".join(part.get("text", "") for part in reply["content"])
            self.assertIn("Then: review 1 pending proposal: onto_review", text)


# ingest-text-arg-cli-confusion -------------------------------------------------------------------------------------
class IngestTextFlagTest(Base):
    def test_body_takes_the_text_inline(self):
        code, out, err = _support.run_cli(["ingest", "--body", "The repair stand is booked by phone.", "--title",
                                           "Pasted"], repo=self.root)
        self.assertEqual(code, 0, out + err)
        clear()
        found = [s for s in graph.Ontology.load(self.repo).sources.values() if s.get("title") == "Pasted"]
        self.assertEqual(len(found), 1)

    def test_text_is_the_output_switch_and_says_so(self):
        words = "the wifi password is hunter2secret"
        code, out, err = _support.run_cli(["ingest", "--text", words, "--title", "Pasted"], repo=self.root)
        self.assertEqual(code, 2, out + err)
        self.assertIn("--body", err)
        self.assertNotIn("hunter2secret", out + err)  # the words are never echoed

    def test_body_runs_the_credential_check(self):
        secret = "the wifi password is Garden2026x"
        code, out, err = _support.run_cli(["ingest", "--body", secret, "--title", "Pasted"], repo=self.root)
        self.assertNotEqual(code, 0, out + err)
        self.assertNotIn("Garden2026x", out + err)
