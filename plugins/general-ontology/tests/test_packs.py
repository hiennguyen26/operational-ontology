"""packs: the built-in packs, the registry, inverses, aliases, shas, (pack, kind) qualification and the
additive-only rule."""

from __future__ import annotations

import copy
import json
import os
import unittest

from tests import _support
from ontokit import errors, packs, store, util

CORE_KINDS = {"topic", "term", "question", "tool", "note", "premise"}
CORE_RELS = {"about", "answers", "defines", "same_as", "related_to", "contradicts", "derived_from", "supports",
             "part_of", "refresh_with", "uses", "rests_on"}
DISCOVERY_KINDS = {"person", "role", "org", "goal", "deliverable", "dataset", "process", "step", "constraint",
                   "metric", "claim"}
DISCOVERY_RELS = {"owns", "member_of", "works_on", "decides", "produces", "consumes", "constrains", "precedes",
                  "measures", "serves"}
BY_DIMENSION = {
    "frame": {"topic", "goal"},
    "people": {"person", "role", "org"},
    "data": {"dataset", "tool"},
    "vocabulary": {"term"},
    "process": {"process", "step"},
    "constraints": {"constraint", "premise"},
    "deliverables": {"deliverable", "metric"},
    "questions": {"question", "claim"},
}

GARDEN = {
    "pack": "local",
    "version": 1,
    "extends": ["core"],
    "kinds": {
        "plot": {"label": "Plot", "plural": "plots", "dimension": "data", "aliases": ["bed"],
                 "fields": {"area_m2": {"type": "number", "minimum": 0}}, "expected": ["area_m2"]},
        "crop": {"label": "Crop", "plural": "crops", "dimension": "data", "priority": 6},
    },
    "relations": {
        "tends": {"inverse": "tended_by", "from": ["role", "person"], "to": ["crop", "plot"], "brief": True},
        "grown_in": {"from": ["crop"], "to": ["plot"]},
    },
    "kind_map": [],
}


def builtin(name):
    with open(os.path.join(packs.BUILTIN_DIR, "%s.pack.json" % name), encoding="utf-8") as fh:
        return json.load(fh)


class BuiltinPacksTest(unittest.TestCase):
    def test_content_matches_spec(self):
        core, disc = builtin("core"), builtin("discovery")
        self.assertEqual(packs.check_pack(core), [])
        self.assertEqual(packs.check_pack(disc), [])
        self.assertEqual(set(core["kinds"]), CORE_KINDS)
        self.assertEqual(set(core["relations"]), CORE_RELS)
        self.assertEqual(set(disc["kinds"]), DISCOVERY_KINDS)
        self.assertEqual(set(disc["relations"]), DISCOVERY_RELS)
        self.assertEqual({k: (v["stage"], v["target"]) for k, v in core["dimensions"].items()},
                         {"frame": (0, 2), "vocabulary": (3, 5), "questions": (7, 2)})
        self.assertEqual({k: (v["stage"], v["target"]) for k, v in disc["dimensions"].items()},
                         {"people": (1, 3), "data": (2, 3), "process": (4, 2), "constraints": (5, 2),
                          "deliverables": (6, 1)})
        self.assertEqual({k for k, v in core["relations"].items() if v["symmetric"]},
                         {"same_as", "related_to", "contradicts"})
        self.assertEqual(disc["kinds"]["person"]["visibility"], "local")
        tool = core["kinds"]["tool"]["fields"]
        self.assertEqual(tool["interface"]["enum"], ["mcp", "cli", "http", "skill", "manual"])
        self.assertEqual(set(tool), {"interface", "invoke", "inputs", "outputs"})
        dataset = disc["kinds"]["dataset"]
        self.assertEqual(dataset["expected"], ["location", "format"])
        self.assertEqual(dataset["expects"], [{"rel": "owns", "dir": "in", "min": 1,
                                               "ask": "Who keeps {name} up to date?"}])
        self.assertEqual([s["title"] for s in disc["deliverables"]["brief"]["sections"]],
                         ["Goal", "People and roles", "Data and sources", "Processes", "Constraints",
                          "Active decisions", "Open points"])

    def test_files_are_canonical(self):
        self.assertEqual(packs.builtin_names(), ["assessment", "core", "discovery"])
        for name in packs.builtin_names():
            with open(os.path.join(packs.BUILTIN_DIR, "%s.pack.json" % name), "rb") as fh:
                data = fh.read()
            self.assertEqual(data, util.canonical_bytes(json.loads(data.decode("utf-8"))), name)


class RegistryTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        self.root = _support.bare_topic(self.tmp, "garden")
        store.write_json(os.path.join(self.root, "packs", "local.pack.json"), GARDEN)
        self.repo = store.Repo.open(self.root)
        self.reg = packs.load(self.repo, self.repo.manifest)

    def test_loads_clean(self):
        self.assertEqual(self.reg.problems(), [])
        self.assertEqual(self.reg.pack_names(), ["core", "discovery", "local"])
        self.assertEqual(set(self.reg.kinds()), CORE_KINDS | DISCOVERY_KINDS | {"plot", "crop"})
        for dim, kinds in BY_DIMENSION.items():
            extra = {"plot", "crop"} if dim == "data" else set()
            self.assertEqual(set(self.reg.kinds_of_dimension(dim)), kinds | extra, dim)
        self.assertEqual(self.reg.dimension_of("note"), None)
        self.assertEqual(self.reg.stage_of("data"), 2)
        self.assertEqual(set(self.reg.dimensions()), {"frame", "vocabulary", "questions", "people", "data",
                                                      "process", "constraints", "deliverables"})
        self.assertIn("brief", self.reg.deliverables())

    def test_local_pack_and_questions_in_memory(self):
        # a writer checks a changed local pack before it writes it: nothing is read from disk for "local"
        pack = copy.deepcopy(GARDEN)
        pack["kinds"]["shed"] = {"label": "Shed", "plural": "sheds"}
        question = {"id": "q.local.sheds", "ask": "Which sheds exist?", "priority": 10}
        reg = packs.load(self.repo, self.repo.manifest, local_pack=pack, local_questions=[question])
        self.assertEqual(reg.problems(), [])
        self.assertIsNotNone(reg.kind("shed"))
        self.assertIsNone(self.reg.kind("shed"))
        self.assertIn("q.local.sheds", [q["id"] for q in reg.questions()])
        self.assertEqual(store.read_json(os.path.join(self.root, "packs", "local.pack.json")), GARDEN)
        alone = packs.load(None, self.repo.manifest, local_pack=pack)
        self.assertIsNotNone(alone.kind("shed"))

    def test_default_manifest_without_repo(self):
        reg = packs.load(None, {})
        self.assertEqual(reg.problems(), [])
        self.assertEqual(reg.pack_names(), ["core", "discovery"])
        self.assertIsNone(reg.kind("plot"))

    def test_kind_accessors(self):
        reg = self.reg
        self.assertEqual(reg.expected("dataset"), ["location", "format"])
        self.assertEqual(reg.expects("dataset")[0]["rel"], "owns")
        self.assertEqual(reg.text_fields("plot"), ["name", "summary"])
        self.assertIn("attrs.location", reg.text_fields("dataset"))
        self.assertTrue(reg.is_hub("topic"))
        self.assertFalse(reg.is_hub("role"))
        self.assertTrue(reg.has_card("dataset"))
        self.assertFalse(reg.has_card("note"))
        self.assertEqual(reg.default_visibility("person"), "local")
        self.assertEqual(reg.default_visibility("plot"), "shared")
        self.assertGreater(reg.priority("goal"), reg.priority("note"))
        self.assertEqual(reg.priority("nope"), 0)
        self.assertEqual(reg.label("org"), "Organization")
        self.assertEqual(reg.fields("plot"), {"area_m2": {"type": "number", "minimum": 0}})

    def test_inverses_and_relations(self):
        reg = self.reg
        self.assertEqual(reg.inverse("owns"), "owned_by")
        self.assertEqual(reg.inverse("tends"), "tended_by")
        self.assertEqual(reg.inverse("same_as"), "same_as")
        self.assertEqual(reg.inverse("grown_in"), "grown_in_by")  # no declared inverse
        self.assertEqual(reg.inverse("unknown"), "unknown_by")
        self.assertEqual(reg.forward("owned_by"), ("owns", True))
        self.assertEqual(reg.forward("owns"), ("owns", False))
        self.assertIsNone(reg.forward("nothing"))
        self.assertTrue(reg.allowed("owns", "role", "dataset"))
        self.assertFalse(reg.allowed("owns", "dataset", "role"))
        self.assertTrue(reg.allowed("tends", "role", "crop"))
        self.assertTrue(reg.allowed("tends", "role", "bed"))  # kind alias
        self.assertFalse(reg.allowed("tends", "crop", "role"))
        self.assertTrue(reg.allowed("about", "note", "crop"))  # "*"
        self.assertTrue(reg.allowed("same_as", "crop", "term"))
        self.assertFalse(reg.allowed("nope", "role", "crop"))
        self.assertTrue(reg.is_symmetric("related_to"))
        self.assertTrue(reg.is_brief("owns"))
        self.assertFalse(reg.is_brief("derived_from"))

    def test_plural_and_kind_aliases(self):
        self.assertEqual(self.reg.plural_alias("Datasets"), "dataset")
        self.assertEqual(self.reg.plural_alias("people"), "person")
        self.assertEqual(self.reg.plural_alias("plots"), "plot")
        self.assertIsNone(self.reg.plural_alias("tomatoes"))
        self.assertIs(self.reg.kind("bed"), self.reg.kind("plot"))
        self.assertEqual(self.reg.kind_key("bed"), "plot")
        self.assertIsNone(self.reg.kind("nope"))

    def test_pack_shas(self):
        for name in ("core", "discovery"):
            self.assertEqual(self.reg.pack_sha(name), util.sha256_hex(util.canonical_bytes(builtin(name))))
        self.assertEqual(self.reg.pack_sha("local"), packs.sha_of(GARDEN))
        self.assertIsNone(self.reg.pack_sha("nope"))

    def test_questions_merged_and_checked(self):
        with open(os.path.join(self.root, "packs", "local.questions.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": "q.local.zeta", "ask": "Z?"}) + "\n")
            fh.write(json.dumps({"id": "q.local.alpha", "ask": "A?", "stage": 2}) + "\n")
            fh.write(json.dumps({"id": "q.local.alpha", "ask": "again"}) + "\n")
            fh.write(json.dumps({"id": "bad", "ask": "?"}) + "\n")
            fh.write("not json\n")
        reg = packs.load(self.repo)
        local = [q["id"] for q in reg.questions() if q["id"].startswith("q.local.")]
        self.assertEqual(local, ["q.local.alpha", "q.local.zeta"])
        ids = [q["id"] for q in reg.questions()]
        self.assertEqual(ids, sorted(ids))
        lines = sorted((p.code, p.line) for p in reg.problems() if p.file == packs.LOCAL_QUESTIONS)
        self.assertEqual(lines, [("P01", 5), ("P20", 3), ("P20", 4)])


class QualificationTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        self.root = _support.bare_topic(self.tmp, "g2t")
        self.repo = store.Repo.open(self.root)
        self.core, self.disc = builtin("core"), builtin("discovery")

    def test_shared_and_unshared_packs(self):
        kitchen_local = {"pack": "local", "version": 1, "kinds": {
            "ingredient": {"label": "Ingredient"}, "plot": {"label": "Kitchen plot"}},
            "relations": {"serves_in": {"from": ["ingredient"], "to": ["deliverable"]}}}
        extra = [("garden", self.core), ("garden", self.disc), ("garden", GARDEN),
                 ("kitchen", self.core), ("kitchen", kitchen_local)]
        reg = packs.load(self.repo, self.repo.manifest, extra)
        self.assertEqual(reg.problems(), [])
        self.assertEqual(reg.shared(), {"garden/core": "core", "garden/discovery": "discovery",
                                        "kitchen/core": "core"})
        # shared packs keep bare kinds; unshared register as ns/kind
        self.assertIn("role", reg.kinds())
        self.assertNotIn("garden/role", reg.kinds())
        self.assertIn("garden/plot", reg.kinds())
        self.assertIn("kitchen/plot", reg.kinds())
        self.assertNotIn("plot", reg.kinds())
        self.assertEqual(reg.kind_key("plot", ns="garden"), "garden/plot")
        self.assertEqual(reg.kind_key("role", ns="garden"), "role")
        self.assertEqual(reg.kind_key("garden/role"), "role")  # shared pack: the bare kind
        self.assertEqual(reg.kind_key("garden/bed"), "garden/plot")  # alias under the namespace
        self.assertEqual(reg.kind_key("crop"), "garden/crop")  # unique across imports
        self.assertIsNone(reg.kind_key("plot"))  # ambiguous across imports
        self.assertEqual(reg.label("garden/plot"), "Plot")
        self.assertEqual(reg.display("garden/plot"), "plot (garden)")
        self.assertEqual(reg.display("garden/crop"), "crop")
        self.assertEqual(reg.display("role"), "role")
        # relations: qualified endpoints, bare name kept when free
        self.assertTrue(reg.allowed("tends", "role", "garden/crop"))
        self.assertTrue(reg.allowed("garden/tends", "role", "garden/crop"))
        self.assertFalse(reg.allowed("tends", "role", "kitchen/ingredient"))
        self.assertTrue(reg.allowed("serves_in", "kitchen/ingredient", "deliverable"))
        self.assertEqual(reg.inverse("tends"), "tended_by")
        self.assertEqual(reg.pack_sha("garden/local"), packs.sha_of(GARDEN))
        self.assertEqual(reg.pack_sha("garden/core"), reg.pack_sha("core"))
        self.assertEqual(reg.namespaces(), ["garden", "kitchen"])
        # imported questions, deliverables and kind maps are not merged
        self.assertEqual(set(reg.deliverables()), {"brief"})

    def test_changed_builtin_is_shared_kind_by_kind(self):
        # a compatible change (a label, a new optional field) keeps the bare kind; the pack as a whole differs
        old_core = copy.deepcopy(self.core)
        old_core["kinds"]["term"]["label"] = "Glossary term"
        old_core["kinds"]["term"]["fields"]["plural_form"] = {"type": "string"}
        reg = packs.load(self.repo, self.repo.manifest, [("garden", old_core)])
        self.assertEqual(reg.shared(), {})
        self.assertNotIn("garden/term", reg.kinds())
        self.assertEqual(reg.kind_key("term", "garden"), "term")
        self.assertEqual(reg.kind_key("garden/term"), "term")
        self.assertEqual(reg.label("term"), "Term")
        self.assertEqual(reg.problems(), [])

    def test_changed_builtin_kind_that_differs_is_qualified(self):
        old_core = copy.deepcopy(self.core)
        old_core["kinds"]["term"]["label"] = "Glossary term"
        old_core["kinds"]["term"]["fields"]["abbreviation"] = {"type": "integer"}  # the same field, declared otherwise
        reg = packs.load(self.repo, self.repo.manifest, [("garden", old_core)])
        self.assertEqual(reg.shared(), {})
        self.assertIn("garden/term", reg.kinds())
        self.assertEqual(reg.display("garden/term"), "term (garden)")
        self.assertEqual(reg.label("term"), "Term")
        self.assertEqual(reg.label("garden/term"), "Glossary term")
        self.assertEqual(reg.kind_key("topic", "garden"), "topic")  # the other kinds stay shared

    def test_local_relation_may_name_imported_kinds(self):
        g2t = {"pack": "local", "version": 1, "kinds": {}, "relations": {
            "feeds": {"from": ["garden/crop"], "to": ["deliverable"]}},
            "kind_map": [{"a": "garden/crop", "b": "garden/plot"}]}
        store.write_json(os.path.join(self.root, "packs", "local.pack.json"), g2t)
        reg = packs.load(self.repo, None, [("garden", GARDEN)])
        self.assertEqual(reg.problems(), [])
        self.assertEqual(reg.kind_map(), [{"a": "garden/crop", "b": "garden/plot"}])
        self.assertTrue(reg.allowed("feeds", "garden/crop", "deliverable"))
        # without the import the qualified kinds are inert (import remove must stay possible), not P20
        alone = packs.load(self.repo)
        self.assertEqual(alone.problems(), [])
        # an imported namespace that lacks the kind is still P20 (a typo is never silent)
        typo = dict(g2t, kind_map=[{"a": "garden/crp", "b": "garden/plot"}])
        store.write_json(os.path.join(self.root, "packs", "local.pack.json"), typo)
        broken = packs.load(self.repo, None, [("garden", GARDEN)])
        self.assertEqual(sorted({p.code for p in broken.problems()}), ["P20"])


class ProblemsTest(_support.TempCase):
    def load_with(self, local, manifest_packs=None):
        root = _support.bare_topic(self.tmp, "p%d" % len(os.listdir(self.tmp)))
        store.write_json(os.path.join(root, "packs", "local.pack.json"), local)
        repo = store.Repo.open(root)
        if manifest_packs is not None:
            repo.manifest["packs"] = manifest_packs
        return packs.load(repo)

    def messages(self, reg):
        self.assertTrue(all(p.code in ("P20", "P01") for p in reg.problems()))
        return " | ".join(p.message for p in reg.problems())

    def test_duplicate_kind_and_relation(self):
        local = copy.deepcopy(GARDEN)
        local["kinds"]["role"] = {"label": "Role again"}
        local["relations"]["owns"] = {"from": "*", "to": "*"}
        reg = self.load_with(local)
        text = self.messages(reg)
        self.assertIn("duplicate kind 'role'", text)
        self.assertIn("duplicate relation 'owns'", text)
        self.assertEqual(reg.label("role"), "Role")  # first declaration kept

    def test_schema_and_reference_problems(self):
        local = copy.deepcopy(GARDEN)
        local["kinds"]["Bad"] = {"label": "x"}
        local["kinds"]["source"] = {"label": "reserved"}
        local["kinds"]["plot"]["fields"]["depth"] = {"type": "number", "exclusiveMinimum": 0}
        local["kinds"]["plot"]["expected"].append("missing_field")
        local["kinds"]["crop"]["expects"] = [{"rel": "nope", "dir": "in", "min": 1, "ask": "?"}]
        local["kinds"]["crop"]["dimension"] = "soil"
        local["relations"]["grown_in"]["to"] = ["nowhere"]
        text = self.messages(self.load_with(local))
        for needle in ("'Bad'", "'source'", "exclusiveMinimum", "'missing_field' is not in fields",
                       "unknown relation 'nope'", "dimension 'soil'", "kind 'nowhere' is not declared"):
            self.assertIn(needle, text)

    def test_manifest_problems(self):
        reg = self.load_with(GARDEN, ["discovery", "local", "nope", "local"])
        text = self.messages(reg)
        self.assertIn("packs must start with core", text)
        self.assertIn("unknown pack 'nope'", text)
        self.assertIn("listed twice", text)
        self.assertIn("role", reg.kinds())  # core and discovery still load

    def test_missing_and_broken_local_pack(self):
        root = _support.bare_topic(self.tmp, "broken")
        os.remove(os.path.join(root, "packs", "local.pack.json"))
        reg = packs.load(store.Repo.open(root))
        self.assertIn("missing", self.messages(reg))
        with open(os.path.join(root, "packs", "local.pack.json"), "w") as fh:
            fh.write("{nope")
        reg = packs.load(store.Repo.open(root))
        self.assertEqual([p.code for p in reg.problems()], ["P01"])

    def test_field_errors(self):
        fields = GARDEN["kinds"]["plot"]["fields"]
        self.assertEqual(packs.field_errors({"area_m2": 3}, fields), [])
        self.assertEqual(len(packs.field_errors({"area_m2": -1, "colour": "red"}, fields)), 2)


class ChangeRulesTest(unittest.TestCase):
    def test_removal_in_use_refused(self):
        new = copy.deepcopy(GARDEN)
        del new["kinds"]["plot"]
        del new["relations"]["tends"]
        problems = packs.check_change(GARDEN, new, used_kinds={"plot", "crop"},
                                      used_rels={"tends": {("role", "crop")}})
        self.assertEqual([p.code for p in problems], ["P20", "P20"])
        self.assertIn("kind plot is in use", problems[0].message)
        self.assertIn("relation tends is in use", problems[1].message)
        self.assertEqual(packs.check_change(GARDEN, new, used_kinds={"crop"}, used_rels={}), [])

    def test_narrowing_in_use_refused(self):
        new = copy.deepcopy(GARDEN)
        new["relations"]["tends"]["from"] = ["person"]
        new["kinds"]["plot"]["fields"]["area_m2"] = {"type": "integer"}
        problems = packs.check_change(GARDEN, new, used_kinds={"plot"}, used_rels={"tends": [("role", "crop")]})
        text = " | ".join(p.message for p in problems)
        self.assertIn("cannot be narrowed", text)
        self.assertIn("field area_m2 cannot be changed", text)
        widened = copy.deepcopy(GARDEN)
        widened["relations"]["tends"]["from"] = "*"
        widened["kinds"]["plot"]["fields"]["soil"] = {"type": "string"}
        self.assertEqual(packs.check_change(GARDEN, widened, {"plot"}, {"tends": [("role", "crop")]}), [])

    def test_apply_op_is_additive(self):
        pack = packs.empty_local_pack()
        pack = packs.apply_op(pack, {"op": "add_kind", "name": "crop", "kind": {"label": "Crop"}})
        pack = packs.apply_op(pack, {"op": "add_field", "kind": "crop", "field": "season",
                                     "schema": {"type": "string"}})
        pack = packs.apply_op(pack, {"op": "add_relation", "name": "grown_with",
                                     "relation": {"from": ["crop"], "to": ["crop"], "symmetric": True}})
        pack = packs.apply_op(pack, {"op": "map_kinds", "a": "garden/crop", "b": "kitchen/ingredient"})
        pack = packs.apply_op(pack, {"op": "map_kinds", "a": "kitchen/ingredient", "b": "garden/crop"})
        self.assertEqual(pack["kind_map"], [{"a": "garden/crop", "b": "kitchen/ingredient"}])
        self.assertEqual(pack["kinds"]["crop"]["fields"], {"season": {"type": "string"}})
        self.assertEqual(packs.check_pack(pack), [])
        refusals = [
            {"op": "add_kind", "name": "crop", "kind": {"label": "Again"}},
            {"op": "add_kind", "name": "e", "kind": {"label": "Reserved"}},
            {"op": "add_kind", "name": "herb", "kind": {"label": "Herb", "surprise": 1}},
            {"op": "add_relation", "name": "grown_with", "relation": {"from": "*", "to": "*"}},
            {"op": "add_field", "kind": "crop", "field": "season", "schema": {"type": "integer"}},
            {"op": "add_field", "kind": "herb", "field": "x", "schema": {"type": "string"}},
            {"op": "add_field", "kind": "crop", "field": "depth", "schema": {"minProperties": 1}},
        ]
        for op in refusals:
            with self.assertRaises(errors.Refused, msg=str(op)):
                packs.apply_op(pack, op)
        with self.assertRaises(errors.UsageError):
            packs.apply_op(pack, {"op": "add_node"})
        self.assertEqual(packs.empty_local_pack()["kinds"], {})


if __name__ == "__main__":
    unittest.main()
