"""util and ids: canonical bytes, keys and slugs, edit distance, the id grammar and the id makers."""

from __future__ import annotations

import hashlib
import os
import unittest
from datetime import timezone

from tests import _support  # noqa: F401  (sys.path, env, clock)
from ontokit import errors, ids, util


class CanonicalTest(unittest.TestCase):
    def test_canonical_line_and_bytes(self):
        obj = {"b": 1, "a": "é", "c": [1, {"z": None, "y": False}]}
        self.assertEqual(util.canonical_line(obj), '{"a":"é","b":1,"c":[1,{"y":false,"z":null}]}')
        self.assertEqual(
            util.canonical_bytes({"b": 1, "a": "é"}),
            '{\n "a": "é",\n "b": 1\n}\n'.encode("utf-8"),
        )
        self.assertEqual(util.canonical_bytes(obj), util.canonical_bytes(dict(reversed(list(obj.items())))))

    def test_hashes(self):
        self.assertEqual(util.sha256_hex(b"abc"), hashlib.sha256(b"abc").hexdigest())
        self.assertEqual(util.sha256_text("é"), hashlib.sha256("é".encode("utf-8")).hexdigest())
        self.assertEqual(util.short_hash({"a": 1}, 6), util.sha256_hex(util.canonical_bytes({"a": 1}))[:6])

    def test_clock_is_pinned(self):
        self.assertEqual(util.now_iso(), _support.FIXED_NOW)
        self.assertEqual(util.today(), "2026-09-28")
        self.assertEqual(util.now().tzinfo, timezone.utc)

    def test_placeholder_clock_counts_as_unset(self):
        saved = os.environ["ONTO_FIXED_NOW"]
        try:
            os.environ["ONTO_FIXED_NOW"] = "${ONTO_FIXED_NOW}"
            self.assertNotEqual(util.now_iso(), "${ONTO_FIXED_NOW}")
        finally:
            os.environ["ONTO_FIXED_NOW"] = saved

    def test_parse_ts(self):
        self.assertEqual(util.fmt_ts(util.parse_ts("2026-09-28T14:02:11Z")), "2026-09-28T14:02:11Z")
        self.assertEqual(util.fmt_ts(util.parse_ts("2026-09-28T14:02:11")), "2026-09-28T14:02:11Z")
        self.assertEqual(util.fmt_ts(util.parse_ts("2026-09-28T16:02:11+02:00")), "2026-09-28T14:02:11Z")
        self.assertEqual(util.fmt_ts(util.parse_ts("2026-09-28")), "2026-09-28T00:00:00Z")
        with self.assertRaises(ValueError):
            util.parse_ts("yesterday")


class KeysTest(unittest.TestCase):
    def test_name_key(self):
        self.assertEqual(util.name_key("Crème  Brûlée!"), "creme brulee")
        self.assertEqual(util.name_key("Straße"), "strasse")
        self.assertEqual(util.name_key("bed-captain / BED_steward"), "bed captain bed steward")
        self.assertEqual(util.tokens("Harvest-log (weekly)"), ["harvest", "log", "weekly"])
        self.assertEqual(util.normalize_ws("  a \n\t b  "), "a b")

    def test_slugify(self):
        self.assertEqual(util.slugify("Crème Brûlée"), "creme-brulee")
        self.assertEqual(util.slugify("  --North Bed #2!"), "north-bed-2")
        self.assertEqual(util.slugify("tomato 番茄 sauce"), "tomato-sauce")
        long = util.slugify("word " * 40)
        self.assertLessEqual(len(long), 60)
        self.assertFalse(long.endswith("-"))
        cjk = util.slugify("番茄")
        self.assertEqual(cjk, "x" + hashlib.sha256("番茄".encode("utf-8")).hexdigest()[:8])
        self.assertTrue(ids.SLUG_RE.match(cjk))

    def test_edit_distance(self):
        self.assertEqual(util.edit_distance("steward", "steward"), 0)
        self.assertEqual(util.edit_distance("steward", "stewart"), 1)
        self.assertEqual(util.edit_distance("abcd", "abdc"), 1)  # transposition counts 1
        self.assertEqual(util.edit_distance("", "ab"), 2)
        self.assertEqual(util.edit_distance("kitten", "sitting"), 3)
        self.assertEqual(util.edit_distance("tomato", "zucchini"), 4)  # past the cap: cap + 1
        self.assertEqual(util.edit_distance("a", "abcdefgh", cap=2), 3)
        self.assertEqual(util.edit_distance("kitten", "sitting", cap=2), 3)


class IdGrammarTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(ids.parse("crop:tomato"), ids.Ref(None, "crop", "tomato"))
        self.assertEqual(ids.parse("garden/crop:tomato"), ids.Ref("garden", "crop", "tomato"))
        self.assertEqual(ids.parse("self/crop:tomato"), ids.Ref(None, "crop", "tomato"))
        self.assertEqual(ids.parse("role:v2.steward@north_x-1"), ids.Ref(None, "role", "v2.steward@north_x-1"))
        for bad in ("Crop:tomato", "crop", "crop:", ":x", "crop:-x", "a/b/c:d", "imp/crop:x", "crop:x\n",
                    "crop:x#y", "e:3f9a1c0b77d2", "", None, "crop:" + "x" * 81):
            with self.assertRaises(errors.UsageError, msg=repr(bad)):
                ids.parse(bad)

    def test_predicates_and_split(self):
        self.assertTrue(ids.is_local("crop:tomato"))
        self.assertFalse(ids.is_local("garden/crop:tomato"))
        self.assertTrue(ids.is_qualified("garden/crop:tomato"))
        self.assertFalse(ids.is_qualified("imp/crop:tomato"))
        self.assertEqual(ids.split_ns("garden/crop:tomato"), ("garden", "crop:tomato"))
        self.assertEqual(ids.split_ns("self/crop:tomato"), (None, "crop:tomato"))
        self.assertEqual(ids.split_ns("crop:tomato"), (None, "crop:tomato"))
        self.assertEqual(ids.qualify("garden", "crop:tomato"), "garden/crop:tomato")
        self.assertEqual(ids.qualify(None, "crop:tomato"), "crop:tomato")
        self.assertEqual(ids.qualify("self", "crop:tomato"), "crop:tomato")
        self.assertEqual(ids.qualify("g2t", "garden/crop:tomato"), "garden/crop:tomato")
        with self.assertRaises(errors.UsageError):
            ids.qualify("imp", "crop:x")

    def test_record_ids(self):
        samples = {
            "e:3f9a1c0b77d2": "e",
            "src-9d2a41c07e55": "src",
            "prop-20260928-3fa9c1": "prop",
            "dec-20260928-rota-3f2a": "dec",
            "chg-20260928-4be1c0": "chg",
            "ans-20260928-a1b2c3": "ans",
            "imp:garden@a1b2c3d4e5f6": "imp",
        }
        for rid, prefix in samples.items():
            self.assertEqual(ids.record_prefix(rid), prefix, rid)
            self.assertTrue(ids.is_record(rid))
            self.assertFalse(ids.is_local(rid), rid)
        self.assertIsNone(ids.record_prefix("crop:tomato"))
        self.assertIsNone(ids.record_prefix("src-9d2a41c07e5"))  # 11 hex
        self.assertEqual(ids.impsrc("garden", "a1b2c3d4e5f6a7b8c9d0" * 2), "imp:garden@a1b2c3d4e5f6")

    def test_edge_id_is_stable(self):
        expected = "e:" + hashlib.sha256("role:bed-steward\x1ftends\x1fcrop:tomato\x1f".encode()).hexdigest()[:12]
        self.assertEqual(ids.edge_id("role:bed-steward", "tends", "crop:tomato"), expected)
        self.assertEqual(ids.edge_id("role:bed-steward", "tends", "crop:tomato", ""), expected)
        self.assertNotEqual(ids.edge_id("role:bed-steward", "tends", "crop:tomato", "2"), expected)
        self.assertTrue(ids.EDGE_RE.match(expected))

    def test_record_id_forms(self):
        body = {"by": "agent", "source": None, "ops": []}
        prop = ids.record_id("prop", body, date="2026-09-28")
        self.assertEqual(prop, "prop-20260928-" + util.short_hash(body, 6))
        self.assertTrue(ids.PROP_RE.match(prop))
        self.assertEqual(ids.record_id("prop", body), prop)  # default date is the pinned today
        dec = ids.record_id("dec", {"q": 1}, date="20260928", slug="Track watering per bed or per steward?")
        self.assertTrue(dec.startswith("dec-20260928-track-watering-per-bed-or-per-steward-"), dec)
        self.assertTrue(ids.DEC_RE.match(dec))
        long = ids.record_id("dec", {"q": 2}, slug="word " * 30)
        self.assertTrue(ids.DEC_RE.match(long), long)
        ans = ids.record_id("ans", "q.data.where" + "a text" + "2026-09-28T14:10:00Z")
        self.assertEqual(ans, "ans-20260928-" + util.sha256_text("q.data.wherea text2026-09-28T14:10:00Z")[:6])
        src = ids.record_id("src", b"clean text")
        self.assertEqual(src, "src-" + hashlib.sha256(b"clean text").hexdigest()[:12])
        with self.assertRaises(errors.UsageError):
            ids.record_id("dec", {"q": 1})
        with self.assertRaises(errors.UsageError):
            ids.record_id("nope", {})

    def test_record_id_extends_on_collision(self):
        body = {"type": "apply", "n": 1}
        first = ids.record_id("chg", body, date="2026-09-28")
        second = ids.record_id("chg", body, date="2026-09-28", taken={first})
        self.assertEqual(len(second), len(first) + 2)
        self.assertTrue(second.startswith(first))
        third = ids.record_id("chg", body, date="2026-09-28", taken=[first, second])
        self.assertEqual(len(third), len(first) + 4)
        self.assertTrue(ids.CHG_RE.match(third))
        with self.assertRaises(errors.DataError):
            ids.record_id("chg", body, date="2026-09-28", taken={first, second, third})
        src = ids.record_id("src", b"x")
        with self.assertRaises(errors.DataError):
            ids.record_id("src", b"x", taken={src})

    def test_new_node_id(self):
        self.assertEqual(ids.new_node_id("crop", "Tomato", set()), "crop:tomato")
        self.assertEqual(ids.new_node_id("crop", "Tomato", {"crop:tomato"}), "crop:tomato-2")
        self.assertEqual(ids.new_node_id("crop", "Tomato", {"crop:tomato", "crop:tomato-2"}), "crop:tomato-3")
        self.assertEqual(ids.new_node_id("role", "Bed steward", {"crop:bed-steward"}), "role:bed-steward")
        with self.assertRaises(errors.UsageError):
            ids.new_node_id("Crop", "x", set())
        with self.assertRaises(errors.UsageError):
            ids.new_node_id("e", "x", set())


class ErrorsTest(unittest.TestCase):
    def test_codes_and_json(self):
        cases = [
            (errors.NotFound("not in the ontology (garden v1)"), "not_found", 1),
            (errors.NotFound("tomato", candidates=["crop:tomato", "dish:tomato"], ambiguous=True), "ambiguous", 1),
            (errors.UsageError("bad"), "usage", 2),
            (errors.Refused("no", problems=["p"]), "refused", 1),
            (errors.Conflict("changed", ops=[2]), "conflict", 1),
            (errors.DataError("broken"), "data", 1),
            (errors.GitError("git"), "git", 1),
            (errors.LockBusy("busy"), "lock_busy", 1),
            (errors.NotBuilt("search"), "not_built", 3),
        ]
        for exc, kind, code in cases:
            self.assertIsInstance(exc, errors.OntoError)
            self.assertEqual((exc.kind, exc.code), (kind, code))
            data = exc.to_json()
            self.assertEqual(data["error"], kind)
            self.assertIn("message", data)
        self.assertEqual(cases[1][0].to_json()["candidates"], ["crop:tomato", "dish:tomato"])
        self.assertEqual(cases[3][0].to_json()["problems"], ["p"])
        self.assertEqual(cases[4][0].to_json()["ops"], [2])
        self.assertEqual(cases[8][0].to_json()["command"], "search")
        problem = errors.Problem("P07", "graph/nodes.jsonl", 3, "bad id")
        self.assertEqual(problem.text(), "graph/nodes.jsonl:3: P07 bad id")
        self.assertEqual(errors.Refused("x", problems=[problem]).to_json()["problems"][0]["code"], "P07")


if __name__ == "__main__":
    unittest.main()
