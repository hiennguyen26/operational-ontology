"""Kit 0.2.0 upgrade (SPEC C7): a topic the 0.1.0 kit wrote passes ``onto migrate --check``, ``onto migrate`` and
``onto validate`` on this kit, and ``migrate`` refuses, writing nothing, when the topic's local pack already uses a
name the 0.2.0 core pack added (kind ``premise``, alias ``assumption``, relation ``rests_on``, its questions).

The 0.1.0 kit is the one of the release commit ``BASE_COMMIT``, taken with ``git archive`` from this repo into a
folder whose name has a space and a curly apostrophe. Without git or that commit (a shallow clone) the tests that
need it skip; the clash tests run on the ``mini`` fixture stamped back to 0.1.0.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import unittest

from tests import _support
from ontokit import __version__, migrate, store

BASE_COMMIT = "56d75c7634c847edbd036a702de699eac76fb08d"  # general-ontology kit 0.1.0
REPO_ROOT = os.path.dirname(os.path.dirname(_support.PLUGIN_DIR))
SPECIAL = "My topic’s folder"
KIT_PARTS = ("plugins/general-ontology/ontokit", "plugins/general-ontology/bin")


def old_kit(dest):
    """Extract the 0.1.0 kit into ``dest``; returns its ``bin/onto`` or raises ``SkipTest``."""
    env = dict(os.environ, **_support.GIT_ENV)
    try:
        proc = subprocess.run(["git", "-C", REPO_ROOT, "archive", "--format=tar", BASE_COMMIT] + list(KIT_PARTS),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise unittest.SkipTest("git is not usable here: %s" % exc)
    if proc.returncode != 0:
        raise unittest.SkipTest("the 0.1.0 commit is not in this clone: %s" % proc.stderr.decode("utf-8", "replace"))
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
        if hasattr(tarfile, "data_filter"):  # Python 3.12+: extract as plain data, as 3.14 does by default
            tar.extractall(dest, filter="data")
        else:
            tar.extractall(dest)
    launcher = os.path.join(dest, "plugins", "general-ontology", "bin", "onto")
    with open(os.path.join(dest, "plugins", "general-ontology", "ontokit", "__init__.py"), encoding="utf-8") as fh:
        if '__version__ = "0.1.0"' not in fh.read():
            raise AssertionError("the base commit is not kit 0.1.0")
    return launcher


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


class FromKit010Test(_support.TempCase):
    def setUp(self):
        super().setUp()
        place = os.path.join(self.tmp, SPECIAL)
        self.onto010 = old_kit(os.path.join(place, "kit 0.1.0"))
        self.root = os.path.join(place, "garden")
        _support.git_init(self.root)
        self.old("init", "--name", "community-garden", "--ns", "garden", "--title", "Community garden")
        self.old("answer", "q.frame.you", "I coordinate the volunteers who tend the beds.")
        self.old("decide", "--question", "Which beds grow vegetables?", "--chosen", "The south beds")
        self.old("log", "--checkpoint", "--done", "framed the garden", "--next", "list the plots")

    def old(self, *args):
        env = dict(os.environ, **_support.GIT_ENV)
        proc = subprocess.run([sys.executable, self.onto010] + list(args), cwd=self.root, env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        self.assertEqual(proc.returncode, 0, (args, proc.stdout, proc.stderr))
        return proc.stdout.decode("utf-8")

    def manifest(self):
        with open(os.path.join(self.root, store.MANIFEST), encoding="utf-8") as fh:
            return json.load(fh)

    def test_a_010_topic_migrates_cleanly(self):
        self.assertEqual(self.manifest()["kit"], "0.1.0")
        code, out, err = _support.run_cli(["validate"], self.root)
        self.assertEqual(code, 0, out + err)
        self.assertIn("W07", out)  # the new kit only warns about the older stamp
        code, out, err = _support.run_cli(["migrate", "--check"], self.root)
        self.assertEqual(code, 0, out + err)
        self.assertIn("stamp kit %s (was 0.1.0)" % __version__, out)
        self.assertEqual(self.manifest()["kit"], "0.1.0")  # --check writes nothing
        code, out, err = _support.run_cli(["migrate"], self.root)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.manifest()["kit"], __version__)
        self.assertEqual(self.manifest()["format"], 1)
        rows, bad = store.read_jsonl(os.path.join(self.root, "ledger", "changes.jsonl"))
        self.assertEqual(bad, [])
        self.assertEqual(rows[-1]["type"], "migrate")
        code, out, err = _support.run_cli(["validate"], self.root)
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("W07", out)
        self.assertNotIn("P20", out)
        code, out, _err = _support.run_cli(["migrate", "--check"], self.root)
        self.assertEqual((code, out.strip().splitlines()[-1]), (0, "up to date: nothing to migrate"))

    def test_after_the_upgrade_the_new_features_work(self):
        self.assertEqual(_support.run_cli(["migrate"], self.root)[0], 0)
        code, out, err = _support.run_cli(["pack", "add", "assessment"], self.root)
        self.assertEqual(code, 0, out + err)
        code, out, err = _support.run_cli(["log", "--last"], self.root)
        self.assertEqual(code, 0, out + err)
        self.assertIn("list the plots", out)
        code, out, err = _support.run_cli(["validate"], self.root)
        self.assertEqual(code, 0, out + err)

    def test_a_local_premise_kind_is_folded_into_the_core_kind(self):
        # round 2: no pack op renames or removes a local kind, so migrate folds the clash itself (it used to refuse
        # and point at a proposal that cannot exist)
        path = os.path.join(self.root, "packs", "local.pack.json")
        with open(path, encoding="utf-8") as fh:
            pack = json.load(fh)
        pack["kinds"]["premise"] = {"label": "Premise", "plural": "premises", "description": "A local premise.",
                                    "dimension": "constraints", "fields": {}}
        store.write_json(path, pack)
        self.assertNotIn("P20", self.old("validate"))  # the 0.1.0 kit accepts it
        self.assertIn("P20 duplicate kind 'premise'", _support.run_cli(["validate"], self.root)[1])  # 0.2.0 does not
        before = {rel: read_bytes(os.path.join(self.root, rel))
                  for rel in (store.MANIFEST, "ledger/changes.jsonl", "packs/local.pack.json")}
        code, out, err = _support.run_cli(["migrate", "--check"], self.root)
        self.assertEqual(code, 0, out + err)
        self.assertIn("fold the local kind premise into the core kind premise", out)
        self.assertEqual({rel: read_bytes(os.path.join(self.root, rel)) for rel in before}, before)  # --check
        code, out, err = _support.run_cli(["migrate"], self.root)
        self.assertEqual(code, 0, out + err)
        with open(path, encoding="utf-8") as fh:
            self.assertNotIn("premise", json.load(fh)["kinds"])
        code, out, err = _support.run_cli(["validate"], self.root)
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("P20", out)
        self.assertEqual(self.manifest()["kit"], __version__)


class ClashTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        self.root = _support.make_topic(os.path.join(self.tmp, SPECIAL))
        path = os.path.join(self.root, store.MANIFEST)
        store.write_json(path, dict(store.read_json(path), kit="0.1.0"))  # as the 0.1.0 kit stamped it
        self.repo = store.Repo.open(self.root)

    def edit_local(self, kinds=None, relations=None):
        path = self.repo.path(migrate.LOCAL_PACK)
        with open(path, encoding="utf-8") as fh:
            pack = json.load(fh)
        pack["kinds"].update(kinds or {})
        pack["relations"].update(relations or {})
        store.write_json(path, pack)

    def test_no_clash_only_stamps(self):
        self.assertEqual(migrate.kit_steps(self.repo), ["0.2.0"])
        self.assertEqual(migrate.clashes(self.repo, ["0.2.0"]), [])
        self.assertEqual(migrate.run(self.repo, check=True), ["stamp kit %s (was 0.1.0)" % __version__])

    def test_each_kind_of_clash_is_named(self):
        self.edit_local(kinds={"plot": {"label": "Plot", "plural": "plots", "description": "A bed.",
                                        "dimension": "vocabulary", "fields": {}, "aliases": ["assumption"]}},
                        relations={"rests_on": {"description": "local", "from": "*", "to": ["plot"],
                                                "symmetric": False}})
        # round 7: a local question folds only when it means what the core one means (the same ask, fills and
        # options); one that asks something else is refused (tests/test_migrate_meaning.py)
        bank = os.path.join(_support.PLUGIN_DIR, "ontokit", "packs", "core.questions.jsonl")
        with open(bank, encoding="utf-8") as fh:
            core = next(q for q in map(json.loads, fh) if q["id"] == "q.constraints.premises")
        with open(self.repo.path(migrate.LOCAL_QUESTIONS), "a", encoding="utf-8") as fh:
            # it states the core stage and dimension: one that leaves them out is asked in the deepening stage and
            # in no dimension, so it is asked another way (tests/test_migrate_meaning.py, QuestionRulesTest)
            fh.write(json.dumps({"id": "q.constraints.premises", "ask": core["ask"].lower(), "fills": core["fills"],
                                 "stage": core["stage"], "dimension": core["dimension"],
                                 "why": "Local wording."}) + "\n")
        found = migrate.clashes(self.repo, ["0.2.0"])
        self.assertEqual(len(found), 3, found)
        self.assertIn("the alias assumption of its kind premise, and alias assumption of kind plot", found[0])
        self.assertIn("the relation rests_on", found[1])
        self.assertIn("the question q.constraints.premises", found[2])
        # round 2: each clash is folded into the core pack (it used to be refused with no way out)
        lines = migrate.run(self.repo, check=True)
        self.assertIn("drop the alias assumption from the local kind plot (the core kind premise has it)", lines)
        self.assertIn("fold the local relation rests_on into the core relation rests_on", lines)
        self.assertIn("drop the local question q.constraints.premises: the core question q.constraints.premises "
                      "takes its place (its priority 0 gives way to the core priority %d, which changes where the "
                      "interview asks it)" % core["priority"], lines)
        self.assertEqual(store.Repo.open(self.root).manifest["kit"], "0.1.0")
        migrate.run(store.Repo.open(self.root))
        repo = store.Repo.open(self.root)
        self.assertEqual(repo.manifest["kit"], __version__)
        self.assertEqual(migrate.clashes(repo, ["0.2.0"]), [])

    def test_a_topic_already_on_020_is_not_checked(self):
        self.edit_local(kinds={"premise": {"label": "Premise", "plural": "premises", "description": "Local.",
                                           "dimension": "constraints", "fields": {}}})
        manifest = dict(self.repo.manifest, kit="0.2.0")
        store.write_json(self.repo.path(store.MANIFEST), manifest)
        repo = store.Repo.open(self.root)
        self.assertNotIn("0.2.0", migrate.kit_steps(repo))
        self.assertEqual(migrate.run(repo, check=True), [] if __version__ == "0.2.0" else
                         ["stamp kit %s (was 0.2.0)" % __version__])


if __name__ == "__main__":
    unittest.main()
