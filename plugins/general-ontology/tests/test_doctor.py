"""``onto doctor`` (SPEC A2) and the one hook line for its cheap failing checks: read-only, ``ok``/``warn``/``fail``
with a fix line each, exit 1 only when a check fails, ``$HOME`` respected, bounded walks, never the network."""

from __future__ import annotations

import json
import os
import time
from unittest import mock

from tests import _support
from tests.test_setup import GIT_CONFIG, SPECIAL, SYSTEM_PATH, git_env, write_stub
from ontokit import doctor, hook


def by_id(obj):
    return {c["id"]: c for c in obj["checks"]}


class DoctorCase(_support.TempCase):
    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        patcher = mock.patch.dict(os.environ, git_env(self.home))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.place = os.path.join(self.tmp, SPECIAL)
        os.makedirs(self.place)
        self.root = _support.make_topic(self.place)
        _support.git_init(self.root)
        for key, value in GIT_CONFIG:
            _support.git(self.root, "config", key, value)
        with open(os.path.join(self.root, ".gitignore"), "w", encoding="utf-8") as fh:
            fh.write("inbox/\n.onto/\n")
        _support.commit_all(self.root, "topic")

    def doctor(self, repo=None):
        code, out, err = _support.run_cli(["doctor", "--json"], repo=repo or self.root)
        return code, json.loads(out)


class DoctorTest(DoctorCase):
    def test_a_clean_topic(self):
        code, obj = self.doctor()
        checks = by_id(obj)
        self.assertEqual(obj["where"], "topic")
        for cid in ("python", "git", "where", "cloud_sync", "dataless", "conflict_copies", "stale_locks", "validate",
                    "pending", "uncommitted", "checkpoint", "plugin", "git_rules"):
            self.assertIn(cid, checks)
        self.assertEqual(checks["python"]["status"], "ok")
        self.assertEqual(checks["git_rules"]["status"], "warn")  # no .gitattributes merge rules
        self.assertEqual(checks["plugin"]["status"], "warn")  # not wired
        self.assertEqual(checks["kit"]["status"], "warn")  # no vendored kit
        self.assertEqual(code, 0)
        self.assertEqual(obj["exit_code"], 0)
        for c in obj["checks"]:
            self.assertIn(c["status"], ("ok", "warn", "fail"))
            if c["status"] != "ok":
                self.assertTrue(c["fix"], c)
        code, out, _err = _support.run_cli(["doctor"], repo=self.root)
        self.assertIn("doctor: topic at", out)
        self.assertIn("fix:", out)

    def test_git_identity(self):
        self.assertEqual(by_id(self.doctor()[1])["git_identity"]["status"], "ok")
        from tests.test_setup import NO_IDENTITY, config_env

        with mock.patch.dict(os.environ, config_env(self.home, NO_IDENTITY)):
            _support.git(self.root, "config", "--unset", "user.name")
            _support.git(self.root, "config", "--unset", "user.email")
            code, obj = self.doctor()
        ident = by_id(obj)["git_identity"]
        self.assertEqual(ident["status"], "warn")
        self.assertIn("git config --global user.name", ident["fix"])
        self.assertEqual(code, 0)  # a warning: reading works without it

    def test_it_writes_nothing(self):
        before = _support.snapshot(self.root, skip=(".git",))
        self.doctor()
        self.assertEqual(_support.snapshot(self.root, skip=(".git",)), before)

    def test_cloud_sync_follows_home(self):
        self.assertIsNone(doctor.cloud_synced(self.root, {"HOME": self.home}))
        icloud = os.path.join(self.home, "Library", "Mobile Documents")
        self.assertIn("iCloud Drive", doctor.cloud_synced(os.path.join(icloud, "x", "t"), {"HOME": self.home}))
        storage = os.path.join(self.home, "Library", "CloudStorage", "Drive-x", "t")
        self.assertIn("cloud storage", doctor.cloud_synced(storage, {"HOME": self.home}))
        desktop = os.path.join(self.home, "Desktop", "t")
        self.assertIsNone(doctor.cloud_synced(desktop, {"HOME": self.home}))
        os.makedirs(os.path.join(icloud, "com~apple~CloudDocs", "Desktop"))
        self.assertIn("Desktop and Documents", doctor.cloud_synced(desktop, {"HOME": self.home}))
        self.assertIsNone(doctor.cloud_synced(os.path.join(self.home, "Ontologies", "t"), {"HOME": self.home}))

    def test_an_evicted_file_fails(self):
        real = os.lstat

        def fake(path):
            st = real(path)
            if os.path.basename(path) == "ontology.json":
                return mock.Mock(st_flags=doctor.DATALESS, st_mode=st.st_mode)
            return st

        with mock.patch.object(doctor, "_lstat", fake):
            code, obj = self.doctor()
            self.assertEqual(hook.doctor_line(self.root),
                             "Setup problem: the dataless check fails; run onto doctor for the fix.")
        self.assertEqual(by_id(obj)["dataless"]["status"], "fail")
        self.assertIn("ontology.json", by_id(obj)["dataless"]["detail"])
        self.assertEqual(code, 1)

    def test_conflict_copies(self):
        # a copy counts only next to its original (review round 4: "Chapter 1.md" alone is the user's file)
        for name in ("notes.md", "notes 2.md"):
            with open(os.path.join(self.root, name), "w") as fh:
                fh.write("")
        code, obj = self.doctor()
        self.assertEqual(by_id(obj)["conflict_copies"]["status"], "warn")
        self.assertIn("notes 2.md", by_id(obj)["conflict_copies"]["detail"])
        os.unlink(os.path.join(self.root, "notes 2.md"))
        os.unlink(os.path.join(self.root, "notes.md"))
        heads = os.path.join(self.root, ".git", "refs", "heads")
        with open(os.path.join(heads, "main 2"), "w") as fh:
            fh.write(_support.git(self.root, "rev-parse", "HEAD") + "\n")
        code, obj = self.doctor()
        self.assertEqual(by_id(obj)["conflict_copies"]["status"], "fail")
        self.assertIn("refs/heads/main 2", by_id(obj)["conflict_copies"]["detail"])
        self.assertIn("do not delete", by_id(obj)["conflict_copies"]["fix"])
        self.assertEqual(code, 1)

    def test_stale_locks(self):
        with open(os.path.join(self.root, ".git", "refs", "heads", "main.lock"), "w"):
            pass
        index_lock = os.path.join(self.root, ".git", "index.lock")
        with open(index_lock, "w") as fh:
            fh.write("x")
        self.assertEqual(doctor.stale_locks(self.root), ["refs/heads/main.lock"])
        old = time.time() - 3600
        os.utime(index_lock, (old, old))
        self.assertEqual(doctor.stale_locks(self.root), ["refs/heads/main.lock", "index.lock"])
        code, obj = self.doctor()
        self.assertEqual(by_id(obj)["stale_locks"]["status"], "warn")

    def test_plugin_wiring(self):
        claude_dir = os.path.join(self.root, ".claude")
        os.makedirs(claude_dir)
        settings = {"extraKnownMarketplaces": {"general-ontology": {"source": {
            "source": "github", "repo": "owner/kit", "ref": "general-ontology"}}},
            "enabledPlugins": {"general-ontology@general-ontology": True}}
        with open(os.path.join(claude_dir, "settings.json"), "w") as fh:
            json.dump(settings, fh)
        self.assertEqual(by_id(self.doctor()[1])["plugin"]["status"], "ok")
        settings["extraKnownMarketplaces"]["general-ontology"]["source"] = {"source": "directory", "path": "/x"}
        with open(os.path.join(claude_dir, "settings.json"), "w") as fh:
            json.dump(settings, fh)
        plugin = by_id(self.doctor()[1])["plugin"]
        self.assertEqual(plugin["status"], "warn")
        self.assertIn("marketplace remove general-ontology", plugin["fix"])

    def test_claude_listing_a_folder_source_is_a_warning(self):
        bin_dir = os.path.join(self.tmp, "bin")
        stub = write_stub(bin_dir)
        with open(stub, "w") as fh:
            fh.write("#!/bin/sh\nprintf '%s\\n' '[{\"name\": \"general-ontology\", \"source\": {\"source\": "
                     "\"directory\", \"path\": \"/somewhere\"}}]'\n")
        os.makedirs(os.path.join(self.root, ".claude"))
        with open(os.path.join(self.root, ".claude", "settings.local.json"), "w") as fh:
            json.dump({"enabledPlugins": {"general-ontology@general-ontology": True}}, fh)
        with mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + SYSTEM_PATH}):
            plugin = by_id(self.doctor()[1])["plugin"]
        self.assertEqual(plugin["status"], "warn")
        self.assertIn("local folder", plugin["detail"])

    def test_a_template_checkout_and_neither(self):
        template = os.path.join(self.place, "template")
        os.makedirs(os.path.join(template, "plugins", "general-ontology", "ontokit"))
        code, out, _err = _support.run_cli(["doctor", "--json", "--repo", template])
        obj = json.loads(out)
        self.assertEqual(obj["where"], "template")
        self.assertNotIn("plugin", by_id(obj))
        elsewhere = os.path.join(self.place, "elsewhere")
        os.makedirs(elsewhere)
        old = os.getcwd()
        os.chdir(elsewhere)
        try:
            code, out, _err = _support.run_cli(["doctor", "--json"])
        finally:
            os.chdir(old)
        obj = json.loads(out)
        self.assertEqual(obj["where"], "neither")
        self.assertEqual(by_id(obj)["where"]["status"], "warn")
        self.assertEqual(code, 0)


class DoctorDetailTest(DoctorCase):
    def test_git_rules_fail_when_inbox_or_onto_is_not_ignored(self):
        with open(os.path.join(self.root, ".gitignore"), "w", encoding="utf-8") as fh:
            fh.write("inbox/\n")
        code, obj = self.doctor()
        rules = by_id(obj)["git_rules"]
        self.assertEqual(rules["status"], "fail")
        self.assertIn(".onto/", rules["detail"])
        self.assertIn(".gitignore", rules["fix"])
        self.assertEqual(code, 1)

    def vendor(self, version):
        folder = os.path.join(self.root, "plugins", "general-ontology", "ontokit")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "__init__.py"), "w", encoding="utf-8") as fh:
            fh.write('__version__ = "%s"\n' % version)

    def test_kit_skew(self):
        from ontokit import __version__

        self.vendor(__version__)
        manifest_path = os.path.join(self.root, "ontology.json")
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        manifest["kit"] = __version__
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        self.assertEqual(by_id(self.doctor()[1])["kit"]["status"], "ok")
        self.vendor("0.0.1")
        kit = by_id(self.doctor()[1])["kit"]
        self.assertEqual(kit["status"], "warn")
        self.assertIn("running kit %s, vendored kit 0.0.1" % __version__, kit["detail"])
        self.assertIn("ontology.json written by kit %s" % __version__, kit["detail"])
        self.assertIn("onto migrate --check", kit["fix"])

    def test_a_template_clone_that_skipped_step_one_warns(self):
        template = os.path.join(self.place, "template")
        os.makedirs(os.path.join(template, "plugins", "general-ontology", "ontokit"))
        with open(os.path.join(template, "plugins", "general-ontology", "ontokit", "__init__.py"), "w") as fh:
            fh.write("")
        _support.git_init(template)
        for key, value in GIT_CONFIG:
            _support.git(template, "config", key, value)
        _support.commit_all(template, "kit")
        _support.git(template, "branch", "-m", "general-ontology")
        code, out, _err = _support.run_cli(["doctor", "--json", "--repo", template])
        where = by_id(json.loads(out))["where"]
        self.assertEqual(where["status"], "warn")
        self.assertIn("branch general-ontology", where["detail"])
        self.assertIn("step 1 not done", where["detail"])
        self.assertIn("onto setup", where["fix"])
        _support.git(template, "checkout", "-q", "-b", "main")
        _support.git(template, "remote", "add", "kit", "https://example.invalid/kit.git")
        code, out, _err = _support.run_cli(["doctor", "--json", "--repo", template])
        where = by_id(json.loads(out))["where"]
        self.assertEqual(where["status"], "ok")
        self.assertIn("kit https://example.invalid/kit.git", where["detail"])

    def test_topic_counts_and_the_checkpoint_age(self):
        with open(os.path.join(self.root, "graph", "nodes.jsonl"), "a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        with open(os.path.join(self.root, "notes.md"), "w", encoding="utf-8") as fh:
            fh.write("x\n")
        with open(os.path.join(self.root, "ledger", "changes.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "checkpoint", "at": "2026-09-01T00:00:00Z", "id": "chg-20260901-aaaaaa",
                                 "summary": "checkpoint"}) + "\n")
        with mock.patch("ontokit.util.now", return_value=__import__("ontokit").util.parse_ts(
                "2026-09-11T00:00:00Z")):
            code, obj = self.doctor()
        checks = by_id(obj)
        self.assertEqual(checks["validate"]["status"], "fail")
        self.assertNotIn("0 validate problems", checks["validate"]["detail"])
        self.assertEqual(checks["pending"]["detail"], "1 pending proposal")
        self.assertEqual(checks["uncommitted"]["status"], "warn")
        self.assertIn("uncommitted topic file", checks["uncommitted"]["detail"])
        self.assertEqual(checks["checkpoint"]["detail"], "last checkpoint 2026-09-01 (10 days ago)")
        self.assertEqual(code, 1)
        with mock.patch("ontokit.pipeline.pending", return_value=list(range(30))):
            pending = by_id(self.doctor()[1])["pending"]
        self.assertEqual(pending["status"], "warn")
        self.assertIn("over max_pending", pending["detail"])

    def listing_stub(self, listing):
        bin_dir = os.path.join(self.tmp, "bin")
        stub = write_stub(bin_dir)
        with open(stub, "w") as fh:
            fh.write("#!/bin/sh\nprintf '%%s\\n' '%s'\n" % json.dumps(listing))
        return bin_dir

    def test_the_plugin_check_names_the_listed_source(self):
        bin_dir = self.listing_stub([{"name": "general-ontology", "source": {"source": "github",
                                                                             "repo": "owner/kit"}}])
        os.makedirs(os.path.join(self.root, ".claude"))
        with open(os.path.join(self.root, ".claude", "settings.local.json"), "w") as fh:
            json.dump({"enabledPlugins": {"general-ontology@general-ontology": True}}, fh)
        with mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + SYSTEM_PATH}):
            plugin = by_id(self.doctor()[1])["plugin"]
        self.assertEqual(plugin["status"], "ok")
        self.assertIn("claude lists it from github owner/kit", plugin["detail"])
        self.assertEqual(doctor.source_text({"source": "git", "url": "https://me:tok@example.invalid/k.git",
                                             "ref": "general-ontology"}),
                         "git https://example.invalid/k.git (ref general-ontology)")

    def test_a_template_checkout_gets_the_machine_marketplace_check(self):
        template = os.path.join(self.place, "template")
        os.makedirs(os.path.join(template, "plugins", "general-ontology", "ontokit"))
        bin_dir = self.listing_stub([{"name": "general-ontology", "source": {"source": "directory",
                                                                             "path": "/somewhere"}}])
        with mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + SYSTEM_PATH}):
            code, out, _err = _support.run_cli(["doctor", "--json", "--repo", template])
        plugin = by_id(json.loads(out))["plugin"]
        self.assertEqual(plugin["status"], "warn")
        self.assertIn("local folder (directory /somewhere)", plugin["detail"])
        self.assertIn("marketplace remove general-ontology", plugin["fix"])
        bin_dir = self.listing_stub([])
        with mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + SYSTEM_PATH}):
            code, out, _err = _support.run_cli(["doctor", "--json", "--repo", template])
        self.assertEqual(by_id(json.loads(out))["plugin"]["status"], "ok")


class HookDoctorLineTest(DoctorCase):
    def test_a_cheap_failure_adds_one_line_within_the_limits(self):
        self.assertIsNone(hook.doctor_line(self.root))
        plain = hook.lines_for(self.root, {})
        self.assertEqual(len(plain), 4)
        with open(os.path.join(self.root, ".git", "refs", "heads", "main 2"), "w") as fh:
            fh.write("x\n")
        lines = hook.lines_for(self.root, {})
        self.assertEqual(len(lines), 5)
        self.assertEqual(lines[1], "Setup problem: the conflict_copies check fails; run onto doctor for the fix.")
        self.assertEqual(lines[:1] + lines[2:], plain)
        self.assertLessEqual(len("\n".join(lines)) + 1, hook.MAX_CHARS)
        with mock.patch.object(doctor.shutil, "which", return_value=None):
            self.assertEqual(hook.doctor_line(self.root),
                             "Setup problem: the git and conflict_copies checks fail; run onto doctor for the fix.")
        with mock.patch.object(hook, "interrupted_line", return_value="Warning: a write stopped half way."):
            self.assertEqual(len(hook.lines_for(self.root, {})), 5)  # already 5 lines: no room, nothing dropped

    def test_a_checkpoint_resume_hint_and_a_doctor_line_fit_together(self):
        """The merged lines: the resume hint on the Next: line and a doctor line, within 5 lines and 600 chars."""
        code, out, err = _support.run_cli(["log", "--checkpoint", "--done", "covered the frame", "--next",
                                           "ask about the beds"], repo=self.root)
        self.assertEqual(code, 0, out + err)
        plain = hook.lines_for(self.root, {})
        self.assertEqual(len(plain), 4)
        self.assertTrue(plain[-1].startswith("Next:"), plain[-1])
        self.assertIn(hook.RESUME_HINT, plain[-1])
        real = os.lstat

        def evicted_index(path):
            st = real(path)
            if os.path.basename(path) == "index" and os.path.basename(os.path.dirname(path)) == ".git":
                return mock.Mock(st_flags=doctor.DATALESS, st_mode=st.st_mode)
            return st

        cases = (("conflict_copies", None), ("dataless", evicted_index))
        for check, fake in cases:
            with self.subTest(check=check):
                copy = os.path.join(self.root, ".git", "refs", "heads", "main 2")
                if fake is None:
                    with open(copy, "w") as fh:
                        fh.write("x\n")
                    lines = hook.lines_for(self.root, {})
                    os.remove(copy)
                else:
                    with mock.patch.object(doctor, "_lstat", fake):
                        lines = hook.lines_for(self.root, {})
                self.assertLessEqual(len(lines), hook.MAX_LINES)
                self.assertEqual(len(lines), 5)
                self.assertLessEqual(len("\n".join(lines)) + 1, hook.MAX_CHARS)
                self.assertEqual(lines[1], "Setup problem: the %s check fails; run onto doctor for the fix." % check)
                self.assertEqual(lines[:1] + lines[2:], plain)
                self.assertTrue(lines[-1].startswith("Next:"), lines[-1])
                self.assertTrue(lines[-1].endswith("resume from the last checkpoint (onto log --last)."), lines[-1])

    def test_the_doctor_line_never_pushes_out_a_topic_line(self):
        with open(os.path.join(self.root, ".git", "refs", "heads", "main 2"), "w") as fh:
            fh.write("x\n")
        width = 140  # four lines this wide fit within MAX_CHARS, with no room left for the doctor line
        long_lines = [(prefix + "x" * width)[:width] for prefix in ("Version line ", "Progress ", "Pending ",
                                                                    "Next: use the onto skill ")]
        self.assertLessEqual(4 * width + 4, hook.MAX_CHARS)
        self.assertGreater(4 * width + 4 + len(hook.DOCTOR_LINE), hook.MAX_CHARS)
        with mock.patch.object(hook, "topic_lines", return_value=long_lines):
            lines = hook.lines_for(self.root, {})
        self.assertEqual(lines, long_lines)  # the doctor line did not fit, so it was left out
        self.assertTrue(lines[-1].startswith("Next:"))
        self.assertLessEqual(len("\n".join(lines)) + 1, hook.MAX_CHARS)
