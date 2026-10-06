"""gitutil: refs are checked before git runs, batch reads, counts, dirty paths, and nothing is ever written."""

from __future__ import annotations

import os
import unittest

from tests import _support
from ontokit import errors, gitutil


class GitUtilTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, "repo")
        _support.git_init(self.root)
        self.write("a.txt", "alpha\n")
        self.write("docs/b.md", "beta\n")
        self.first = _support.commit_all(self.root, "first")
        _support.tag(self.root, "v1")
        self.write("a.txt", "alpha two\n")
        self.second = _support.commit_all(self.root, "second")

    def write(self, rel, text):
        path = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_resolve_ref(self):
        self.assertEqual(gitutil.resolve_ref(self.root, "v1"), self.first)
        self.assertEqual(gitutil.resolve_ref(self.root, self.second[:7]), self.second)
        self.assertEqual(gitutil.resolve_ref(self.root, self.second), self.second)
        for bad in ("--upload-pack=x", "-h", "HEAD", "main", "v1~1", "v1^{tree}", "../x", "", None, "V1",
                    "abc", "v1 ", "v1\n"):
            with self.assertRaises(errors.GitError, msg=repr(bad)):
                gitutil.resolve_ref(self.root, bad)
        with self.assertRaises(errors.GitError):
            gitutil.resolve_ref(self.root, "v9")
        with self.assertRaises(errors.GitError):
            gitutil.resolve_ref(self.root, "deadbeef" * 5)

    def test_read_files_in_one_batch(self):
        files = gitutil.read_files(self.root, self.first, ["a.txt", "docs/b.md", "missing.txt", "../etc", "/abs"])
        self.assertEqual(files, {"a.txt": b"alpha\n", "docs/b.md": b"beta\n"})
        self.assertEqual(gitutil.read_files(self.root, self.second, ["a.txt"]), {"a.txt": b"alpha two\n"})
        self.assertEqual(gitutil.read_files(self.root, self.second, ["docs"]), {})  # a tree, not a file
        with self.assertRaises(errors.GitError):
            gitutil.read_files(self.root, "--output=x", ["a.txt"])
        listed = gitutil.ref_files(self.root, self.first, "docs")
        self.assertEqual(listed, [("docs/b.md", b"beta\n")])
        oid = gitutil.git(self.root, "rev-parse", "%s:a.txt" % self.first)
        self.assertEqual(gitutil.blobs(self.root, [oid, "0" * 40]), [b"alpha\n", None])

    def test_counts_head_and_tags(self):
        self.assertEqual(gitutil.head(self.root), self.second)
        self.assertEqual(gitutil.commits_ahead(self.root, self.first), 1)
        self.assertEqual(gitutil.commits_ahead(self.root, self.second), 0)
        self.assertIsNone(gitutil.commits_ahead(self.root, "--all"))
        self.assertIsNone(gitutil.commits_ahead(self.root, "deadbeef" * 5))
        self.assertEqual(gitutil.branch(self.root), "main")
        self.assertEqual(gitutil.tags(self.root), ["v1"])
        self.assertIsNone(gitutil.head(self.tmp))
        self.assertEqual(gitutil.git(self.tmp, "rev-parse", "HEAD"), "")
        ok, out, err = gitutil.git_ok(self.root, "rev-parse", "HEAD")
        self.assertEqual((ok, out), (True, self.second))
        ok, _out, err = gitutil.git_ok(self.root, "rev-parse", "--verify", "nope")
        self.assertFalse(ok)

    def test_dirty_paths_and_commit_set(self):
        self.assertEqual(gitutil.dirty_paths(self.root), [])
        _support.git(self.root, "mv", "docs/b.md", "docs/renamed file.md")
        self.write("new.txt", "n\n")
        self.write("a.txt", "changed\n")
        with open(os.path.join(self.root, ".gitignore"), "w") as fh:
            fh.write("ignored.txt\n")
        self.write("ignored.txt", "i\n")
        self.assertEqual(gitutil.dirty_paths(self.root),
                         [".gitignore", "a.txt", "docs/b.md", "docs/renamed file.md", "new.txt"])
        self.assertEqual(gitutil.commit_set(self.root),
                         [".gitignore", "a.txt", "docs/renamed file.md", "new.txt"])

    def test_nothing_written(self):
        self.write("a.txt", "dirty\n")  # a dirty tree makes status want to refresh the index
        before = _support.snapshot(os.path.join(self.root, ".git"))
        gitutil.resolve_ref(self.root, "v1")
        gitutil.read_files(self.root, self.first, ["a.txt"])
        gitutil.commits_ahead(self.root, self.first)
        gitutil.head(self.root)
        gitutil.dirty_paths(self.root)
        gitutil.commit_set(self.root)
        gitutil.ref_files(self.root, self.first, "")
        gitutil.tags(self.root)
        self.assertEqual(_support.snapshot(os.path.join(self.root, ".git")), before)


if __name__ == "__main__":
    unittest.main()
