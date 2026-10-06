"""Kit 0.3.0 upgrade (SPEC-harness H7): the data format and the core pack are unchanged, so ``onto migrate`` on a
topic the 0.2.0 kit stamped has one step, the stamp. ``--check`` lists it and writes nothing, ``migrate`` changes
only ``ontology.json`` (its ``kit``) and appends one ``migrate`` change, a second run is ``up to date``, and
``onto validate`` no longer warns W07 (written by an older kit)."""

from __future__ import annotations

import json
import os
import unittest

from tests import _support
from ontokit import FORMAT, __version__, migrate, store

STAMP = "stamp kit 0.3.0 (was 0.2.0)"


class FromKit020Test(_support.TempCase):
    def setUp(self):
        super().setUp()
        self.root = _support.make_topic(os.path.join(self.tmp, "My topic’s folder"))
        path = os.path.join(self.root, store.MANIFEST)
        store.write_json(path, dict(store.read_json(path), kit="0.2.0"))  # as the 0.2.0 kit stamped it
        self.before = _support.snapshot(self.root)

    def test_the_kit_is_0_3_0_with_the_same_data_format(self):
        self.assertEqual(__version__, "0.3.0")
        self.assertEqual(store.read_json(os.path.join(self.root, store.MANIFEST))["format"], FORMAT)
        self.assertNotIn("0.3.0", migrate.KIT_STEPS)  # no core pack name was added, so no clash can arise

    def test_check_lists_only_the_stamp_and_writes_nothing(self):
        repo = store.Repo.open(self.root)
        self.assertEqual(migrate.kit_steps(repo), [])
        self.assertEqual(migrate.needed(repo), [])
        self.assertEqual(migrate.run(repo, check=True), [STAMP])
        code, out, err = _support.run_cli(["migrate", "--check"], repo=self.root)
        self.assertEqual(code, 0, out + err)
        self.assertIn(STAMP, out)
        self.assertEqual(_support.snapshot(self.root), self.before)

    def test_migrate_stamps_and_changes_only_the_manifest_and_the_change_log(self):
        code, out, err = _support.run_cli(["validate"], repo=self.root)
        self.assertIn("W07", out + err)
        code, out, err = _support.run_cli(["migrate"], repo=self.root)
        self.assertEqual(code, 0, out + err)
        self.assertIn(STAMP, out)
        after = _support.snapshot(self.root, skip=(".onto",))
        changed = sorted(rel for rel in set(after) | set(self.before) if after.get(rel) != self.before.get(rel))
        self.assertEqual(changed, sorted([store.MANIFEST, "ledger/changes.jsonl"]))
        manifest = store.read_json(os.path.join(self.root, store.MANIFEST))
        self.assertEqual((manifest["kit"], manifest["format"]), ("0.3.0", FORMAT))
        with open(os.path.join(self.root, "ledger", "changes.jsonl"), encoding="utf-8") as fh:
            last = json.loads(fh.read().splitlines()[-1])
        self.assertIn(STAMP, json.dumps(last, ensure_ascii=False))
        code, out, err = _support.run_cli(["migrate"], repo=self.root)
        self.assertEqual(code, 0, out + err)
        self.assertIn("up to date", out)
        code, out, err = _support.run_cli(["validate"], repo=self.root)
        self.assertNotIn("W07", out + err)


if __name__ == "__main__":
    unittest.main()
