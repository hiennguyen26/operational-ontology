"""store (discovery, IO, lock, path guard, hashes, stamps), records, secrets and migrate."""

from __future__ import annotations

import fcntl
import json
import os
import time
import unittest
from unittest import mock

from tests import _support
from ontokit import FORMAT, __version__, errors, migrate, secrets, store, util


class DiscoverTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        self.a = _support.bare_topic(self.tmp, "alpha")
        self.b = _support.bare_topic(self.tmp, "beta")
        self.plain = os.path.join(self.tmp, "plain", "deep")
        os.makedirs(self.plain)
        self.inside = os.path.join(self.a, "graph")

    def test_order(self):
        env = {"ONTO_REPO": self.b, "CLAUDE_PROJECT_DIR": self.b}
        self.assertEqual(store.discover(self.a, cwd=self.plain, env=env).root, self.a)  # explicit first
        self.assertEqual(store.discover(None, cwd=self.inside, env=env).root, self.b)  # then $ONTO_REPO
        env = {"CLAUDE_PROJECT_DIR": os.path.join(self.b, "packs")}
        self.assertEqual(store.discover(None, cwd=self.inside, env=env).root, self.a)  # then the cwd walk
        self.assertEqual(store.discover(None, cwd=self.plain, env=env).root, self.b)  # then the project dir walk

    def test_placeholders_count_as_unset(self):
        for value in ("", "  ", "$ONTO_REPO", "${ONTO_REPO}"):
            self.assertTrue(store.is_placeholder(value), value)
            env = {"ONTO_REPO": value, "CLAUDE_PROJECT_DIR": "${CLAUDE_PROJECT_DIR}"}
            self.assertEqual(store.discover(None, cwd=self.inside, env=env).root, self.a)
        self.assertFalse(store.is_placeholder("/some/path"))
        self.assertEqual(store.discover("$ONTO_REPO", cwd=self.inside, env={}).root, self.a)

    def test_given_path_that_is_not_a_repo_is_an_error(self):
        with self.assertRaises(errors.DataError) as ctx:
            store.discover(self.plain, cwd=self.inside, env={})
        self.assertIn("run onto init", ctx.exception.message)
        with self.assertRaises(errors.DataError):
            store.discover(None, cwd=self.inside, env={"ONTO_REPO": self.plain})

    def test_nothing_found(self):
        with self.assertRaises(errors.DataError) as ctx:
            store.discover(None, cwd=self.plain, env={})
        self.assertIn("no ontology.json", ctx.exception.message)
        self.assertEqual(ctx.exception.to_json()["needs"], "ontology.json")
        self.assertIsNone(store.find_root(None, cwd=self.plain, env={}))

    def test_repo_object(self):
        repo = store.discover(self.a, env={})
        self.assertEqual(repo.ns, "alpha")
        self.assertEqual(repo.path("graph/nodes.jsonl"), os.path.join(self.a, "graph", "nodes.jsonl"))
        self.assertEqual(repo.policy["stage_done_at"], 0.6)
        self.assertEqual(repo.manifest["kit"], __version__)
        self.assertEqual(repo.manifest["format"], FORMAT)
        with open(repo.path("ontology.json"), "w") as fh:
            fh.write("{broken")
        with self.assertRaises(errors.DataError):
            store.Repo.open(self.a)


class IoTest(_support.TempCase):
    def test_write_json_is_canonical_and_atomic(self):
        path = os.path.join(self.tmp, "x", "a.json")
        store.write_json(path, {"b": 1, "a": [1, 2]})
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), util.canonical_bytes({"a": [1, 2], "b": 1}))
        self.assertEqual(os.listdir(os.path.dirname(path)), ["a.json"])  # no temp file left
        self.assertEqual(store.read_json(path), {"a": [1, 2], "b": 1})
        self.assertEqual(store.read_json(os.path.join(self.tmp, "none.json"), None), None)
        with self.assertRaises(FileNotFoundError):
            store.read_json(os.path.join(self.tmp, "none.json"))

    def test_failed_write_keeps_old_bytes(self):
        path = os.path.join(self.tmp, "a.json")
        store.write_json(path, {"v": 1})
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.write_json(path, {"v": 2})
        self.assertEqual(store.read_json(path), {"v": 1})
        self.assertEqual(os.listdir(self.tmp), ["a.json"])

    def test_jsonl_round_trip(self):
        path = os.path.join(self.tmp, "g", "nodes.jsonl")
        store.write_jsonl(path, [{"id": "b:2", "x": 1}, {"id": "a:1", "y": "é"}])
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), '{"id":"a:1","y":"é"}\n{"id":"b:2","x":1}\n')
        rows, problems = store.read_jsonl(path)
        self.assertEqual([r["id"] for r in rows], ["a:1", "b:2"])
        self.assertEqual(problems, [])
        store.write_jsonl(path, [{"at": "2", "kind": "a"}, {"at": "1", "kind": "b"}], key=("at", "kind"))
        self.assertEqual([r["at"] for r in store.read_jsonl(path)[0]], ["1", "2"])
        store.write_jsonl(path, [])
        self.assertEqual(os.path.getsize(path), 0)

    def test_read_jsonl_reports_bad_lines(self):
        path = os.path.join(self.tmp, "bad.jsonl")
        with open(path, "wb") as fh:
            fh.write(b'{"id":"a:1"}\n\n[1,2]\n{broken\n\xff\xfe\n{"id":"b:2"}')
        rows, problems = store.read_jsonl(path)
        self.assertEqual([r["id"] for r in rows], ["a:1", "b:2"])
        self.assertEqual([line for line, _msg in problems], [3, 4, 5])
        numbered, _ = store.read_jsonl_lines(path)
        self.assertEqual([n for n, _row in numbered], [1, 6])
        self.assertEqual(store.read_jsonl(os.path.join(self.tmp, "missing.jsonl")), ([], []))

    def test_writes_keep_the_file_mode(self):
        # regression: mkstemp makes 0600 files, so every atomic write used to leave topic files unreadable to others
        path = os.path.join(self.tmp, "m", "a.json")
        store.write_json(path, {"v": 1})
        self.assertEqual(os.stat(path).st_mode & 0o777, store._DEFAULT_MODE)
        self.assertNotEqual(store._DEFAULT_MODE & 0o044, 0)  # group and others can read under a normal umask
        os.chmod(path, 0o640)
        store.write_json(path, {"v": 2})
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)
        root = _support.bare_topic(self.tmp, "modes")
        from ontokit import sources

        entry, _dup = sources.add(store.Repo.open(root), "a note\n", "note", "Note")
        self.assertEqual(os.stat(sources.text_path(store.Repo.open(root), entry["id"])).st_mode & 0o777,
                         store._DEFAULT_MODE)

    def test_append_repairs_missing_newline(self):
        path = os.path.join(self.tmp, "log.jsonl")
        with open(path, "w") as fh:
            fh.write('{"id":1}')
        store.append_jsonl(path, {"id": 2, "a": "b"})
        store.append_jsonl(path, {"id": 3})
        with open(path) as fh:
            self.assertEqual(fh.read(), '{"id":1}\n{"a":"b","id":2}\n{"id":3}\n')
        fresh = os.path.join(self.tmp, "new", "log.jsonl")
        store.append_jsonl(fresh, {"id": 1})
        self.assertEqual(store.read_jsonl(fresh)[0], [{"id": 1}])


class LockTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        self.root = _support.bare_topic(self.tmp, "lock")
        self.lock_path = os.path.join(self.root, ".onto", "lock")

    def test_lock_is_reentrant_and_released(self):
        with store.write_lock(self.root):
            with store.write_lock(self.root, timeout=0):
                pass
        with store.write_lock(self.root, timeout=0):
            pass

    def test_lock_busy(self):
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            started = time.monotonic()
            with self.assertRaises(errors.LockBusy):
                with store.write_lock(self.root, timeout=0.2):
                    pass
            self.assertGreaterEqual(time.monotonic() - started, 0.15)
        finally:
            os.close(fd)
        with store.write_lock(self.root, timeout=0):
            pass

    def test_fallback_lock_busy_and_stale(self):
        with mock.patch.object(store, "_USE_FLOCK", False):
            os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
            with open(self.lock_path, "w") as fh:
                fh.write("99999")
            with self.assertRaises(errors.LockBusy) as ctx:
                with store.write_lock(self.root, timeout=0.1):
                    pass
            self.assertIn("99999", ctx.exception.message)
            old = time.time() - store.LOCK_STALE_SECONDS - 5
            os.utime(self.lock_path, (old, old))
            with store.write_lock(self.root, timeout=0.1):
                with open(self.lock_path) as fh:
                    self.assertEqual(fh.read(), str(os.getpid()))
            self.assertFalse(os.path.exists(self.lock_path))


class GuardTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        self.root = _support.bare_topic(self.tmp, "guard")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, ".ssh"))
        self.patch = mock.patch.dict(os.environ, {"HOME": self.home})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def write(self, *parts):
        path = os.path.join(*parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("data\n")
        return path

    def test_accepts_inbox_and_repo_files(self):
        note = self.write(self.root, "inbox", "notes.md")
        self.assertEqual(store.guard_input_path(note, self.root), os.path.realpath(note))
        self.assertEqual(store.guard_input_path(os.path.join(self.root, "inbox"), store.Repo.open(self.root)),
                         os.path.realpath(os.path.join(self.root, "inbox")))

    def test_refusals(self):
        refused = [
            self.write(self.home, ".ssh", "config"),
            self.write(self.home, ".ssh", "keys", "plain.txt"),
            self.write(self.root, "inbox", ".netrc"),
            self.write(self.root, "inbox", ".env.local"),
            self.write(self.root, "inbox", "id_rsa.pub"),
            self.write(self.root, "inbox", "server.pem"),
            self.write(self.root, "inbox", "credentials.json"),
            self.write(self.root, "inbox", ".hidden-notes.md"),
            self.write(self.root, ".git", "notes.md"),
        ]
        for path in refused:
            with self.assertRaises(errors.Refused, msg=path):
                store.guard_input_path(path, self.root)
            if ".git" not in path:
                with self.assertRaises(errors.Refused, msg=path):
                    store.guard_input_path(path, self.root, allow_any=True)

    def test_outside_repo_needs_allow_any(self):
        outside = self.write(self.tmp, "elsewhere", "notes.md")
        with self.assertRaises(errors.Refused):
            store.guard_input_path(outside, self.root)
        with self.assertRaises(errors.Refused):
            store.guard_input_path(outside, None)
        self.assertEqual(store.guard_input_path(outside, self.root, allow_any=True), os.path.realpath(outside))
        link = os.path.join(self.root, "inbox", "link.md")
        os.makedirs(os.path.dirname(link), exist_ok=True)
        os.symlink(outside, link)
        with self.assertRaises(errors.Refused):
            store.guard_input_path(link, self.root)
        with self.assertRaises(errors.UsageError):
            store.guard_input_path(os.path.join(self.root, "inbox", "missing.md"), self.root)


class HashTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        self.root = _support.bare_topic(self.tmp, "hash")

    def test_data_hash_is_stable_and_order_free(self):
        first = store.data_hash(self.root)
        store.clear_cache()
        self.assertEqual(store.data_hash(self.root), first)
        other = _support.bare_topic(os.path.join(self.tmp, "copy"), "hash")
        self.assertEqual(store.data_hash(other), first)  # same bytes, other folder
        def content(rel):
            path = os.path.join(self.root, *rel.split("/"))
            if not os.path.exists(path):
                return b""
            with open(path, "rb") as fh:
                return fh.read()

        manual = util.sha256_hex(b"".join(rel.encode() + b"\0" + content(rel) + b"\0"
                                          for rel in sorted(store.DATA_FILES)))
        self.assertEqual(first, manual)

    def test_cache_invalidates_on_write(self):
        path = os.path.join(self.root, "graph", "nodes.jsonl")
        before_stamp, before_hash = store.data_stamp(self.root), store.data_hash(self.root)
        store.write_jsonl(path, [{"id": "a:1"}])
        after_stamp, after_hash = store.data_stamp(self.root), store.data_hash(self.root)
        self.assertNotEqual(before_stamp, after_stamp)
        self.assertNotEqual(before_hash, after_hash)
        sha1 = store.file_sha256(path)
        store.write_jsonl(path, [{"id": "a:2"}])  # same size
        self.assertNotEqual(store.data_stamp(self.root), after_stamp)
        self.assertNotEqual(store.file_sha256(path), sha1)
        self.assertNotEqual(store.data_hash(self.root), after_hash)

    def test_decisions_and_exports_count(self):
        base = store.data_hash(self.root)
        dec = os.path.join(self.root, "ledger", "decisions", "dec-20260928-x-abcd.json")
        store.write_json(dec, {"id": "dec-20260928-x-abcd"})
        self.assertIn("ledger/decisions/dec-20260928-x-abcd.json", store.data_files(self.root))
        self.assertNotEqual(store.data_hash(self.root), base)
        stamp = store.data_stamp(self.root)
        store.write_json(os.path.join(self.root, "imports", "garden", "export.json"), {"meta": {}})
        self.assertNotEqual(store.data_stamp(self.root), stamp)
        self.assertIn("imports/garden/export.json", [s[0] for s in store.data_stamp(self.root)])


class PathProblemsTest(_support.TempCase):
    def test_allow_list_junk_and_links(self):
        root = _support.bare_topic(self.tmp, "paths")
        ok = ["graph/nodes.jsonl", "sources/src-0123456789ab.txt", "sources/src-0123456789ab.orig.pdf",
              "proposals/pending/prop-20260928-abcdef.json", "ledger/decisions/dec-20260928-rota-3f2a.json",
              "imports/garden/export.json", "build/export.json", "build/index.html", "MANIFEST.json"]
        bad = ["graph/notes.txt", "sources/handbook.md", "graph/.DS_Store", "graph/nodes 2.jsonl",
               "packs/__pycache__/x.pyc", "proposals/prop-20260928-abcdef.json"]
        for rel in ok + bad:
            path = os.path.join(root, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write("x")
        os.symlink(os.path.join(root, "graph", "nodes.jsonl"), os.path.join(root, "graph", "edges.jsonl"))
        with open(os.path.join(root, "README.md"), "w") as fh:
            fh.write("kit file at the root is not topic data")
        problems = store.path_problems(root)
        files = sorted(p.file for p in problems)
        self.assertEqual(files, sorted(bad + ["graph/edges.jsonl"]))
        self.assertTrue(all(p.code == "P19" for p in problems))
        link = [p for p in problems if p.file == "graph/edges.jsonl"][0]
        self.assertIn("symbolic link", link.message)


class VersionStampTest(_support.TempCase):
    def test_unreleased_and_released(self):
        root = _support.bare_topic(self.tmp, "stamp")
        repo = store.Repo.open(root)
        stamp = store.version_stamp(repo)
        self.assertEqual(stamp["version"], "unreleased")
        self.assertIsNone(stamp["matches_release"])
        self.assertEqual(stamp["ns"], "stamp")
        self.assertEqual(stamp["kit"], __version__)
        self.assertFalse(stamp["kit_mismatch"])
        self.assertEqual(stamp["imports"], [])
        self.assertIsNone(stamp["richness"])
        self.assertEqual(set(stamp), {"ns", "version", "data_hash", "matches_release", "changes_after", "imports",
                                      "kit", "repo_kit", "kit_mismatch", "richness"})
        store.append_jsonl(repo.path("ledger/changes.jsonl"), {"id": "chg-20260928-000001", "type": "apply"})
        store.write_json(repo.path("MANIFEST.json"), {"version": "v1", "data_hash": store.data_hash(root),
                                                      "last_change": "chg-20260928-000001"})
        stamp = store.version_stamp(repo, richness={"score": 41, "band": "sketch", "change_text": ""})
        self.assertEqual((stamp["version"], stamp["matches_release"], stamp["changes_after"]), ("v1", True, 0))
        self.assertEqual(stamp["richness"]["score"], 41)
        store.write_jsonl(repo.path("graph/nodes.jsonl"), [{"id": "a:1"}])
        store.append_jsonl(repo.path("ledger/changes.jsonl"), {"id": "chg-20260928-000002", "type": "apply"})
        store.append_jsonl(repo.path("ledger/changes.jsonl"), {"id": "chg-20260928-000003", "type": "checkpoint"})
        stamp = store.version_stamp(repo)
        self.assertEqual((stamp["matches_release"], stamp["changes_after"]), (False, 1))

    def test_import_pins_and_kit_mismatch(self):
        root = _support.bare_topic(self.tmp, "pins")
        export = os.path.join(root, "imports", "garden", "export.json")
        store.write_json(export, {"meta": {}})
        lock = {"format": 1, "imports": [
            {"ns": "garden", "ref": "v1", "commit": "a1b2c3d" + "0" * 33, "export_sha256": store.file_sha256(export)},
            {"ns": "kitchen", "ref": "v2", "commit": "9e8f7a6" + "0" * 33, "export_sha256": "0" * 64}]}
        store.write_json(os.path.join(root, "imports", "lock.json"), lock)
        manifest = store.read_json(os.path.join(root, "ontology.json"))
        manifest["kit"] = "0.4.0"  # a kit newer than this one
        store.write_json(os.path.join(root, "ontology.json"), manifest)
        stamp = store.version_stamp(store.Repo.open(root))
        self.assertEqual(stamp["imports"], [{"ns": "garden", "ref": "v1", "commit7": "a1b2c3d", "ok": True},
                                            {"ns": "kitchen", "ref": "v2", "commit7": "9e8f7a6", "ok": False}])
        self.assertTrue(stamp["kit_mismatch"])
        self.assertEqual(stamp["repo_kit"], "0.4.0")


class SecretsTest(_support.TempCase):
    def test_every_kind_has_a_fake_and_is_found(self):
        for kind in secrets.KINDS:
            sample = _support.fake_secret(kind)
            self.assertIn(kind, [k for k, _m in secrets.scan_str("x " + sample + " y")], kind)
            self.assertIn(kind, [k for k, _m in secrets.scan_bytes(("x " + sample).encode())], kind)
        self.assertEqual(secrets.scan_str("task-list-abcdefghijklmnopqrstuvwxyz"), [])

    def test_scan_paths_prints_prefix_only(self):
        folder = os.path.join(self.tmp, "scan")
        os.makedirs(os.path.join(folder, ".git"))
        with open(os.path.join(folder, "a.txt"), "w") as fh:
            fh.write("token " + _support.fake_secret("github"))
        with open(os.path.join(folder, ".git", "b.txt"), "w") as fh:
            fh.write(_support.fake_secret("aws"))
        count, hits = secrets.scan_paths([folder])
        self.assertEqual(count, 1)
        self.assertEqual([(os.path.basename(f), k, len(p)) for f, k, p in hits], [("a.txt", "github", 6)])

    def test_denylist(self):
        deny = os.path.join(self.tmp, "deny.txt")
        with open(deny, "w") as fh:
            fh.write("# comment\n\n\\bacme\\b\nwidget[-_ ]?co\n(unbalanced\n")
        patterns = secrets.load_denylist(deny)
        self.assertEqual(len(patterns), 3)
        folder = os.path.join(self.tmp, "tree")
        os.makedirs(os.path.join(folder, "sub"))
        os.makedirs(os.path.join(folder, ".git"))
        with open(os.path.join(folder, "sub", "a.md"), "w") as fh:
            fh.write("clean line\nMade by ACME for Widget Co\nthe (unbalanced part\n")
        with open(os.path.join(folder, "acme-notes.md"), "w") as fh:
            fh.write("clean\n")
        with open(os.path.join(folder, ".git", "c"), "w") as fh:
            fh.write("acme\n")
        hits = secrets.scan_denylist([folder], patterns)
        simple = sorted((os.path.relpath(f, folder), line, term) for f, line, term in hits)
        self.assertEqual(simple, [("acme-notes.md", 0, "\\bacme"), ("sub/a.md", 2, "\\bacme"),
                                  ("sub/a.md", 2, "widget"), ("sub/a.md", 3, "\\(unba")])
        self.assertEqual(secrets.scan_denylist([folder], []), [])


class MigrateTest(_support.TempCase):
    def test_current_repo_needs_nothing(self):
        repo = store.Repo.open(_support.bare_topic(self.tmp, "mig"))
        self.assertEqual(migrate.needed(repo), [])
        self.assertEqual(migrate.run(repo), [])
        self.assertFalse(os.path.exists(repo.path("ledger/changes.jsonl")))

    def test_newer_repo_is_refused(self):
        repo = store.Repo.open(_support.bare_topic(self.tmp, "new"))
        repo.manifest["format"] = FORMAT + 1
        with self.assertRaises(errors.DataError) as ctx:
            migrate.needed(repo)
        self.assertIn("upgrade the kit", ctx.exception.message)

    def test_kit_stamp_and_steps(self):
        root = _support.bare_topic(self.tmp, "old")
        manifest = store.read_json(os.path.join(root, "ontology.json"))
        manifest["kit"] = "0.0.9"
        store.write_json(os.path.join(root, "ontology.json"), manifest)
        repo = store.Repo.open(root)
        self.assertEqual(migrate.run(repo, check=True), ["stamp kit %s (was 0.0.9)" % __version__])
        self.assertEqual(store.read_json(repo.path("ontology.json"))["kit"], "0.0.9")
        lines = migrate.run(repo)
        self.assertEqual(lines, ["stamp kit %s (was 0.0.9)" % __version__])
        self.assertEqual(store.read_json(repo.path("ontology.json"))["kit"], __version__)
        rows, _ = store.read_jsonl(repo.path("ledger/changes.jsonl"))
        self.assertEqual([r["type"] for r in rows], ["migrate"])
        repo.manifest["format"] = 0
        with self.assertRaises(errors.DataError):
            migrate.needed(repo)
        calls = []
        nxt = FORMAT + 1
        with mock.patch.object(migrate, "FORMAT", nxt), \
                mock.patch.dict(migrate.MIGRATIONS, {nxt: lambda r: calls.append(r.root)}):
            repo.manifest["format"] = FORMAT
            store.write_json(repo.path("ontology.json"), repo.manifest)
            self.assertEqual(migrate.needed(repo), [nxt])
            self.assertEqual(migrate.run(repo), ["migrate to format %d" % nxt])
        self.assertEqual(calls, [root])
        self.assertEqual(store.read_json(repo.path("ontology.json"))["format"], nxt)
        with mock.patch.object(migrate, "FORMAT", nxt + 1):
            with self.assertRaises(errors.DataError):  # no step to format nxt + 1
                migrate.needed(store.Repo.open(root))


class RecordsModuleTest(unittest.TestCase):
    def test_check_returns_paths(self):
        from ontokit import records

        errs = records.check({"id": "x"}, "gap")
        self.assertIn("$: 'field' is a required property", errs)
        self.assertTrue(records.is_valid({"field": "attrs.x", "note": ""}, "gap"))
        with open(records.RECORDS_SCHEMA, encoding="utf-8") as fh:
            self.assertIn("$defs", json.load(fh))


if __name__ == "__main__":
    unittest.main()
