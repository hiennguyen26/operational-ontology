"""schema_lite keywords (old and new), SchemaUnsupported, and the records and pack schemas against the spec's
example records."""

from __future__ import annotations

import copy
import json
import os
import unittest

from tests import _support  # noqa: F401
from ontokit import packs, records, schema_lite

SCHEMA_DIR = os.path.join(_support.PLUGIN_DIR, "ontokit", "schema")


def check(instance, schema):
    return schema_lite.validate(instance, "t", {"$defs": {"t": schema}})


class KeywordTest(unittest.TestCase):
    def test_subset_keywords(self):
        schema = {
            "type": "object",
            "required": ["a"],
            "additionalProperties": False,
            "properties": {
                "a": {"type": "string", "minLength": 2, "maxLength": 4, "pattern": "^x"},
                "b": {"type": ["integer", "null"], "minimum": 1, "maximum": 3},
                "c": {"type": "array", "items": {"enum": [1, 2]}, "minItems": 1, "maxItems": 2},
                "d": {"const": True},
            },
        }
        self.assertEqual(check({"a": "xy", "b": None, "c": [1], "d": True}, schema), [])
        errs = check({"a": "y", "b": 5, "c": [], "d": 1, "e": 0}, schema)
        text = "\n".join(errs)
        self.assertIn("$.a: 'y' is too short", text)
        self.assertIn("does not match", text)
        self.assertIn("$.b: 5 is greater than the maximum of 3", text)
        self.assertIn("$.c: [] should be non-empty", text)
        self.assertIn("$.d: true was expected", text)
        self.assertIn("Additional properties are not allowed ('e' was unexpected)", text)
        self.assertIn("$: 'a' is a required property", check({}, schema))
        self.assertEqual(check(True, {"type": "integer"}), ["$: true is not of type 'integer'"])
        self.assertEqual(check(1.0, {"type": "integer"}), [])

    def test_ref_allof_if(self):
        root = {
            "$defs": {
                "t": {"allOf": [{"$ref": "#/$defs/s"}], "if": {"properties": {"k": {"const": 1}}},
                      "then": {"required": ["v"]}},
                "s": {"type": "object"},
            }
        }
        self.assertEqual(schema_lite.validate({"k": 2}, "t", root), [])
        self.assertEqual(schema_lite.validate({"k": 1}, "t", root), ["$: 'v' is a required property"])
        self.assertTrue(schema_lite.validate([], "t", root))

    def test_any_of(self):
        schema = {"anyOf": [{"type": "string"}, {"type": "integer", "minimum": 5}]}
        self.assertEqual(check("x", schema), [])
        self.assertEqual(check(7, schema), [])
        self.assertEqual(check(3, schema), ["$: 3 is not valid under any of the given schemas"])

    def test_one_of(self):
        schema = {"oneOf": [{"type": "integer"}, {"type": "number", "minimum": 10}]}
        self.assertEqual(check(3, schema), [])
        self.assertEqual(check(10.5, schema), [])
        self.assertEqual(check(12, schema), ["$: 12 is valid under each of 2 of the given schemas"])
        self.assertEqual(check("x", schema), ["$: 'x' is not valid under any of the given schemas"])

    def test_not(self):
        schema = {"type": "string", "not": {"enum": ["self", "imp"]}}
        self.assertEqual(check("garden", schema), [])
        self.assertEqual(check("self", schema), ['$: \'self\' should not be valid under {"enum": ["self", "imp"]}'])

    def test_unique_items(self):
        schema = {"type": "array", "uniqueItems": True}
        self.assertEqual(check([1, "1", True, {"a": 1}, {"a": 2}], schema), [])
        self.assertEqual(check([{"a": 1}, {"a": 1}], schema), ['$: [{"a": 1}, {"a": 1}] has non-unique elements'])
        self.assertEqual(check([1, 1.0], schema), ["$: [1, 1.0] has non-unique elements"])
        self.assertEqual(check([1, True], schema), [])
        self.assertEqual(check([1, 1], {"type": "array", "uniqueItems": False}), [])

    def test_property_names(self):
        schema = {"type": "object", "propertyNames": {"pattern": "^[a-z]+$", "maxLength": 3}}
        self.assertEqual(check({"ab": 1}, schema), [])
        errs = check({"Ab": 1, "abcd": 2}, schema)
        self.assertEqual(len(errs), 2)
        self.assertTrue(all(e.startswith("$: property name ") for e in errs), errs)

    def test_unsupported_keywords_raise(self):
        for schema in ({"patternProperties": {}}, {"minProperties": 1}, {"exclusiveMinimum": 1},
                       {"anyOf": [{"dependentRequired": {}}]}, {"$ref": "other.json#/x"}):
            with self.assertRaises(schema_lite.SchemaUnsupported, msg=str(schema)):
                check({"a": 1}, schema)
        with self.assertRaises(schema_lite.SchemaUnsupported):
            schema_lite.validate({}, "missing", {"$defs": {}})
        self.assertEqual(check(1, {"description": "x", "title": "y", "default": 1, "examples": [], "format": "z"}), [])


NODE = {
    "id": "role:bed-steward", "kind": "role", "name": "Bed steward",
    "summary": "Volunteer who waters and weeds one bed each week.",
    "status": "confirmed", "trust": "reviewed", "conf": 0.8, "visibility": "shared",
    "attrs": {"shift": "weekly"}, "aliases": ["bed captain", "role:bed-captain"],
    "gaps": [{"field": "attrs.contact_channel", "note": "not asked yet"}],
    "prov": [{"src": "src-1f0c9a2b7d4e", "loc": "L3-L5", "quote": "stewards water the tomato beds", "by": "agent"}],
    "created": "2026-09-28", "updated": "2026-09-28", "change": "chg-20260928-4be1c0", "archived": None,
}
EDGE = {
    "id": "e:3f9a1c0b77d2", "src": "role:bed-steward", "rel": "tends", "dst": "crop:tomato", "key": "",
    "status": "confirmed", "trust": "reviewed", "conf": 0.7, "note": "", "background": False,
    "prov": [{"src": "src-9d2a41c07e55", "loc": "L12-L14", "quote": "stewards water the tomato beds", "by": "agent",
              "via": None}],
    "created": "2026-09-28", "updated": "2026-09-28", "change": "chg-20260928-4be1c0", "archived": None,
}
SHA = "a" * 64
PROV = [{"src": "src-9d2a41c07e55", "loc": "L3-L5", "quote": "stewards water", "by": "agent"}]
PROPOSAL = {
    "id": "prop-20260928-3fa9c1", "status": "pending", "by": "agent", "created": "2026-09-28T14:05:00Z",
    "source": "src-9d2a41c07e55", "summary": "Handbook: 2 roles, 2 crops, 1 dataset, 1 process",
    "supersedes": None, "base": SHA, "priority": 37,
    "ops": [
        {"n": 1, "op": "add_node", "ref": "$steward", "node": {"kind": "role", "name": "Bed steward",
                                                               "summary": "Waters one bed."},
         "conf": 0.8, "basis": "inferred", "prov": PROV,
         "annot": {"matches": [{"id": "role:bed-captain", "score": 0.62, "why": "name edit distance 2, same kind"}],
                   "expect": {}, "conflict": None, "stale": False, "assigned_id": "role:bed-steward"}},
        {"n": 2, "op": "add_edge", "edge": {"src": "$steward", "rel": "tends", "dst": "crop:tomato", "key": ""},
         "conf": 0.7, "prov": PROV},
        {"n": 3, "op": "update_node", "id": "dataset:harvest-log", "set": {"attrs.cadence": "weekly"}, "unset": [],
         "reason": "handbook states the log is filled every Saturday", "prov": PROV,
         "annot": {"expect": {"attrs.cadence": None, "change": "chg-20260920-aa11bb"}}},
        {"n": 4, "op": "merge", "keep": "$steward", "drop": "role:bed-captain"},
        {"n": 5, "op": "archive", "id": "process:old-rota",
         "archived": {"reason": "replaced by the weekly steward rota", "decision": "dec-20260928-rota-3f2a",
                      "superseded_by": ["process:weekly-rota"]}},
        {"n": 6, "op": "add_gap", "id": "role:bed-steward", "gap": {"field": "attrs.shift", "note": "unknown"}},
        {"n": 7, "op": "add_kind", "name": "crop", "kind": {"label": "Crop"}},
        {"n": 8, "op": "add_relation", "name": "tends", "relation": {"from": ["role"], "to": ["crop"]}},
        {"n": 9, "op": "add_field", "kind": "crop", "field": "season", "schema": {"type": "string"}},
        {"n": 10, "op": "map_kinds", "a": "garden/crop", "b": "kitchen/ingredient"},
        {"n": 11, "op": "add_question", "question": {"id": "q.local.soil", "ask": "What soil does {name} need?"}},
        {"n": 12, "op": "update_edge", "id": "e:3f9a1c0b77d2", "set": {"note": "weekly"}, "reason": "", "prov": PROV},
    ],
    "checks": {"problems": [], "warnings": [], "quotes": {"checked": 4, "failed": []}},
    "impact": ["crop:tomato", "dataset:harvest-log", "role:bed-captain"], "new_terms": ["first crack"],
    "review": {"by": "user", "at": "2026-09-28T14:20:00Z",
               "verdicts": {"1": "accept", "2": "accept", "3": "draft", "4": "accept", "5": "reject"},
               "edits": {}, "reason": "captain and steward are the same role"},
    "applied": {"at": "2026-09-28T14:20:01Z", "change": "chg-20260928-4be1c0",
                "results": {"1": {"id": "role:bed-steward", "status": "confirmed"}, "5": {"skipped": "rejected"}}},
}
LOCK_ENTRY = {
    "ns": "garden", "name": "community-garden", "from": "../community-garden", "ref": "v1", "commit": "c" * 40,
    "export_sha256": SHA, "manifest_sha256": SHA, "kit": "0.1.0", "format": 1,
    "packs": {"core": SHA, "discovery": SHA, "local": SHA}, "nodes": 42, "edges": 61,
    "via": None, "override": None, "locked_at": "2026-09-28",
}
EXAMPLES = {
    "node": NODE,
    "edge": EDGE,
    "source": {
        "id": "src-9d2a41c07e55", "kind": "note", "title": "Volunteer handbook excerpt", "sha256": SHA,
        "bytes": 2204, "lines": 61, "captured_at": "2026-09-28T14:02:11Z", "url": None, "fetched_at": None,
        "original": {"sha256": SHA, "bytes": 88123, "media_type": "application/pdf", "stored": False,
                     "converter": "agent:pdf-to-text"},
        "via": None, "stale_after_days": None, "trust": "untrusted", "redactions": {"email": 1},
        "supersedes": None, "erased": False,
    },
    "question": {
        "id": "q.data.where", "stage": 2, "dimension": "data", "priority": 50, "quick": True,
        "ask": "Where does the {topic} data you rely on live today?", "why": "Sources lead to evidence fastest.",
        "fills": {"kinds": ["dataset", "tool"], "fields": ["location"]},
        "when": [{"count": {"kind": "dataset", "lt": 3}}], "until": [{"count": {"kind": "dataset", "gte": 3}}],
        "follow_ups": ["q.data.owner"], "options": None, "repeatable": False,
    },
    "answer": {"id": "ans-20260928-a1b2c3", "q": "q.data.where", "at": "2026-09-28T14:10:00Z", "status": "answered",
               "src": "src-77aa01b2c3d4", "proposal": "prop-20260928-3fa9c1", "node": None},
    "proposal": PROPOSAL,
    "decision": {
        "id": "dec-20260928-rota-3f2a", "at": "2026-09-28T14:18:00Z",
        "question": "Track watering per bed or per steward?",
        "options": [{"id": "bed", "label": "Per bed"}, {"id": "steward", "label": "Per steward"}],
        "recommended": "bed", "chosen": "other", "chosen_text": "Per bed, but note the steward on each entry",
        "decided_by": "user", "rationale": "Beds change stewards mid-season",
        "scope": ["dataset:harvest-log", "garden/"], "status": "active", "supersedes": None, "superseded_by": None,
    },
    "change": {"id": "chg-20260928-4be1c0", "at": "2026-09-28T14:20:01Z", "type": "apply", "by": "user",
               "proposal": "prop-20260928-3fa9c1", "source": "src-9d2a41c07e55",
               "ids": ["role:bed-steward", "e:3f9a1c0b77d2"], "summary": "4 ops applied, 1 rejected",
               "before": SHA, "after": "b" * 64},
    "point": {
        "at": "2026-09-28T14:20:01Z", "kind": "apply", "label": None,
        "values": {"richness": 41, "coverage": 0.52, "completeness": 0.61, "connectivity": 0.44, "evidence": 0.38,
                   "confirmation": 0.70, "brier": 0.18},
        "denominators": {"completeness": 46, "confirmation": 57},
        "breakdowns": {"nodes_by_kind": {"role": 3, "crop": 4}, "edges_by_rel": {"tends": 5},
                       "bridges": {"garden|kitchen": 2}, "dimensions": {"people": 0.66, "data": 0.33}, "sources": 4,
                       "pending": 1, "open_questions": 3},
        "missing": {},
    },
    "lock": {"format": 1, "imports": [LOCK_ENTRY, dict(LOCK_ENTRY, ns="kitchen", name="neighborhood-kitchen",
                                                       **{"from": None}, manifest_sha256=None, packs={}, via="g2t")]},
    "card": {"id": "role:bed-steward", "ns": "self", "kind": "role", "title": "Bed steward", "chars": 612,
             "body": "role:bed-steward  Bed steward (confirmed, reviewed)\nVolunteer who waters one bed.",
             "follow": "Next: onto_get id=role:bed-steward full=true"},
    "ontology_manifest": {
        "format": 1, "kit": "0.1.0", "name": "community-garden", "ns": "garden", "title": "Community garden",
        "created": "2026-09-28", "packs": ["core", "discovery", "local"],
        "policy": {"personal": {"email": "redact", "phone": "redact", "name": "keep", "address": "redact"},
                   "max_pending": 20, "pending_stale_days": 30, "stage_done_at": 0.6,
                   "weights": {"coverage": 30, "completeness": 20, "connectivity": 15, "evidence": 20,
                               "confirmation": 15},
                   "keep_original_max_bytes": 5000000},
    },
    "manifest": {
        "format": 1, "kit": "0.1.0", "name": "community-garden", "ns": "garden", "version": "v1",
        "created": "2026-09-28T15:00:00Z", "data_hash": SHA, "last_change": "chg-20260928-4be1c0",
        "files": {"graph/nodes.jsonl": SHA}, "packs": {"core": SHA}, "imports": [LOCK_ENTRY],
        "counts": {"nodes": 42, "edges": 61, "sources": 4}, "richness": {"richness": 38, "band": "sketch"},
        "checks": {"validate": "ok", "scan": "ok", "tests": "skipped"},
    },
}


class RecordsSchemaTest(unittest.TestCase):
    def test_defs_exist(self):
        defs = records.schema()["$defs"]
        for name in records.DEFS:
            self.assertIn(name, defs)

    def test_spec_examples_validate(self):
        for name, example in EXAMPLES.items():
            self.assertEqual(records.check(example, name), [], name)
        for op in PROPOSAL["ops"]:
            self.assertEqual(records.check(op, "op_" + op["op"]), [], op["op"])
        self.assertEqual(records.check({"ops": PROPOSAL["ops"][:2], "by": "agent"}, "proposal_draft"), [])

    def test_export_and_cards_file(self):
        inner = {
            "meta": {"format": 1, "kit": "0.1.0", "name": "community-garden", "ns": "garden", "title": "Garden",
                     "version": "v1", "data_hash": SHA, "last_change": None,
                     "packs": {"core": {"sha256": SHA, "pack": {"pack": "core"}}}, "imports": [],
                     "counts": {"nodes": 1, "edges": 1, "sources": 1, "bridges": 0},
                     "richness": {"richness": 52, "band": "working"},
                     "statuses": [{"id": "confirmed", "label": "Confirmed", "tone": "ok"}]},
            "nodes": [NODE], "edges": [EDGE],
            "sources": [{"id": "src-9d2a41c07e55", "kind": "note", "title": "t", "sha256": SHA, "bytes": 1,
                         "captured_at": "2026-09-28T14:02:11Z"}],
            "bundled": {},
        }
        outer = copy.deepcopy(inner)
        outer["meta"]["ns"] = "g2t"
        outer["meta"]["version"] = "unreleased"
        outer["bundled"] = {"garden": {"sha256": SHA, "export": inner}}
        self.assertEqual(records.check(outer, "export"), [])
        outer["bundled"]["garden"]["export"]["nodes"][0] = dict(NODE, id="Bad Id")
        self.assertTrue(records.check(outer, "export"))
        cards = {"meta": {"data_hash": SHA, "kit": "0.1.0"}, "cards": [EXAMPLES["card"]]}
        self.assertEqual(records.check(cards, "cards_file"), [])

    def test_bad_records_fail(self):
        cases = [
            ("node", dict(NODE, summary="TODO: fill in")),
            ("node", dict(NODE, summary="x" * 601)),
            ("node", dict(NODE, conf=1.5)),
            ("node", dict(NODE, attrs={"shift": {"nested": 1}})),
            ("node", dict(NODE, attrs={"Bad Key": 1})),
            ("node", dict(NODE, extra=True)),
            ("node", dict(NODE, erased=False)),
            ("node", {k: v for k, v in NODE.items() if k != "change"}),
            ("edge", dict(EDGE, rel="Tends")),
            ("edge", dict(EDGE, id="e:xyz")),
            ("prov", {"src": "src-1", "loc": "L1-L2", "by": "agent"}),
            ("prov", dict(PROV[0], quote="q" * 301)),
            ("prov", dict(PROV[0], loc="line 3")),
            ("prov", dict(PROV[0], by="someone")),
            ("op", {"op": "add_node", "node": {"kind": "role"}}),
            ("op", {"op": "update_node", "id": "role:x", "set": {"status": "confirmed"}}),
            ("op", {"op": "merge", "keep": "role:x"}),
            ("op", {"op": "delete", "id": "role:x"}),
            ("decision", dict(EXAMPLES["decision"], status="maybe")),
            ("ontology_manifest", dict(EXAMPLES["ontology_manifest"], ns="self")),
            ("ontology_manifest", dict(EXAMPLES["ontology_manifest"], ns="Garden")),
            ("lock_entry", dict(LOCK_ENTRY, ref="--upload-pack=x")),
            ("card", dict(EXAMPLES["card"], body="x" * 1001)),
            ("answer", dict(EXAMPLES["answer"], status="done")),
        ]
        for name, record in cases:
            self.assertTrue(records.check(record, name), "%s should fail: %r" % (name, record))

    def test_node_accepts_erased_and_qualified_ids(self):
        self.assertEqual(records.check(dict(NODE, erased=True), "node"), [])
        self.assertEqual(records.check(dict(NODE, id="garden/role:bed-steward"), "node"), [])
        archived = {"on": "2026-09-28", "reason": "replaced by the weekly rota", "decision": "dec-20260928-rota-3f2a",
                    "superseded_by": ["process:weekly-rota"]}
        self.assertEqual(records.check(dict(NODE, status="archived", archived=archived), "node"), [])
        self.assertEqual(records.check(dict(EDGE, src="src-9d2a41c07e55", rel="refresh_with", dst="tool:fetcher"),
                                       "edge"), [])
        for loc in ("Q:q.data.where", "T00:12:05", "P3", "garden/crop:tomato"):
            self.assertEqual(records.check(dict(PROV[0], loc=loc), "prov"), [], loc)
        self.assertEqual(records.check(dict(PROV[0], src="imp:garden@a1b2c3d4e5f6", loc="garden/crop:tomato"),
                                       "prov"), [])


class PackSchemaTest(unittest.TestCase):
    def test_question_defs_match_records(self):
        with open(os.path.join(SCHEMA_DIR, "pack.schema.json"), encoding="utf-8") as fh:
            pack_defs = json.load(fh)["$defs"]
        record_defs = records.schema()["$defs"]
        for name in ("question", "predicate", "option", "question_id", "kind_name"):
            self.assertEqual(pack_defs[name], record_defs[name], name)

    def test_schema_files_are_canonical(self):
        for name in ("records.schema.json", "pack.schema.json"):
            path = os.path.join(SCHEMA_DIR, name)
            with open(path, "rb") as fh:
                data = fh.read()
            obj = json.loads(data.decode("utf-8"))
            self.assertEqual(data, (json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=1) + "\n").encode())

    def test_predicates(self):
        good = [{"count": {"kind": "dataset", "lt": 3}}, {"count": {"kind": "crop", "gte": 1, "ns": "self"}},
                {"missing_field": {"kind": "dataset", "field": "location"}}, {"answered": "q.frame.goal"},
                {"not_answered": "q.frame.goal"}, {"stage_done": 2}, {"has_imports": True}]
        for pred in good:
            self.assertEqual(packs.check_question({"id": "q.x", "ask": "?", "when": [pred]}), [], pred)
        for pred in ({"count": {"kind": "dataset"}}, {"answered": "frame"}, {"stage_done": 12}, {"nope": 1},
                     {"answered": "q.a", "stage_done": 1}):
            self.assertTrue(packs.check_question({"id": "q.x", "ask": "?", "when": [pred]}), pred)
        self.assertEqual(packs.check_question({"id": "q.gap.orphan", "for_gap": "orphan",
                                               "ask": "How does {name} relate to the rest of {topic}?",
                                               "priority": 30}), [])


if __name__ == "__main__":
    unittest.main()
