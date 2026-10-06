"""Review round 7 of kit 0.2.0: setup's commit takes only the files setup wrote.

A file that was uncommitted before setup ran stays out of every commit setup makes, also inside the topic folders
(a ``sources/notes.txt`` in a template checkout), and is named in the commit step's detail. A rerun after a failed
commit (no git identity, a failing pre-commit hook, a signing failure) still commits what the earlier run wrote:
``.onto/setup-state.json`` records those paths and their hashes, and a file the user changed since then is theirs.
This holds for ``--here`` and ``--new``. A file setup wrote whose credential-like text stopped the commit is recorded
as ``held``: the user was told to take the text out, so the rerun commits the edited file once the scan is clean.
"""

from __future__ import annotations

import json
import os
import stat
from unittest import mock

from tests import _support
from tests.test_setup import GIT_CONFIG, NO_IDENTITY, SetupCase, config_env
from ontokit import onboard

ARGS = ("--yes", "--launch", "none", "--plugin", "skip")
SIGN_FAILS = GIT_CONFIG + (("commit.gpgsign", "true"), ("gpg.program", "false"))


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def step(obj, sid):
    return next(s for s in obj["steps"] if s["id"] == sid)


def tracked(root):
    """The files in the last commit (not the index: a failed commit leaves what it added staged)."""
    return set(_support.git(root, "ls-tree", "-r", "--name-only", "HEAD").splitlines())


def dirty(root):
    """Every uncommitted path: changed tracked files and untracked ones."""
    changed = _support.git(root, "diff", "--name-only", "HEAD").splitlines()
    others = _support.git(root, "ls-files", "--others", "--exclude-standard").splitlines()
    return sorted(set(p for p in changed + others if p))


class PreexistingFilesTest(SetupCase):
    def test_a_preexisting_file_in_a_topic_folder_stays_out_of_the_start_commit(self):
        clone = self.clone_template("here-notes")
        write(os.path.join(clone, "sources", "notes.txt"), "my own notes\n")
        code, obj, err = self.setup("--here", *ARGS, "--title", "Garden plots", cwd=clone)
        self.assertEqual(code, 0, (obj, err))
        commit = step(obj, "commit")
        self.assertEqual(commit["status"], "done", commit)
        self.assertEqual(_support.git(clone, "log", "-1", "--format=%s"), "Start garden-plots")
        self.assertIn("ontology.json", tracked(clone))
        self.assertNotIn("sources/notes.txt", tracked(clone))
        self.assertIn("not committed: 1 file setup did not write (sources/notes.txt)", commit["detail"])
        self.assertEqual(dirty(clone), ["sources/notes.txt"])

    def test_a_preexisting_change_to_a_tracked_topic_file_stays_out(self):
        # a template that already ships a topic folder file, changed by the user before setup ran
        clone = self.clone_template("here-tracked")
        write(os.path.join(clone, "graph", "README.md"), "kit text\n")
        _support.commit_all(clone, "kit graph readme")
        write(os.path.join(clone, "graph", "README.md"), "my edit\n")
        code, obj, err = self.setup("--here", *ARGS, "--title", "Garden plots", cwd=clone)
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(step(obj, "commit")["status"], "done")
        self.assertEqual(_support.git(clone, "show", "HEAD:graph/README.md"), "kit text")
        self.assertEqual(dirty(clone), ["graph/README.md"])


class CommitRerunTest(SetupCase):
    """A rerun after each kind of commit failure commits setup's own files and still leaves the user's out."""

    def hooks(self):
        folder = os.path.join(self.tmp, "hooks")
        path = write(os.path.join(folder, "pre-commit"), "#!/bin/sh\necho 'hook says no' >&2\nexit 1\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return folder

    def failing(self, kind):
        """(env, patches) under which setup's commit fails the ``kind`` way after the preflight passed."""
        if kind == "identity":
            # git accepted the identity at the preflight, then had none at the commit
            return config_env(self.home, NO_IDENTITY), [mock.patch.object(onboard, "identity_at", return_value=None)]
        if kind == "hook":
            pairs = tuple(p for p in GIT_CONFIG if p[0] != "core.hooksPath") + (("core.hooksPath", self.hooks()),)
            return config_env(self.home, pairs), []
        return config_env(self.home, SIGN_FAILS), []

    def run_failing(self, kind, *argv, cwd=None):
        env, patches = self.failing(kind)
        with mock.patch.dict(os.environ, env):
            for p in patches:
                p.start()
            try:
                code, obj, err = self.setup(*argv, cwd=cwd)
            finally:
                for p in patches:
                    p.stop()
        self.assertEqual(code, 1, (kind, obj, err))
        self.assertEqual(step(obj, "commit")["status"], "failed", (kind, obj))
        return obj

    def check_rerun(self, kind, mode):
        if mode == "here":
            target = self.clone_template("rerun-%s-%s" % (kind, mode))
            write(os.path.join(target, "sources", "notes.txt"), "my own notes\n")
            argv, cwd = ("--here",) + ARGS + ("--title", "Garden", "--name", "garden"), target
        else:
            target = os.path.join(self.place, "rerun-%s-%s" % (kind, mode))
            argv, cwd = ARGS + ("--title", "Garden", "--name", "garden", "--new", target), None
        self.run_failing(kind, *argv, cwd=cwd)
        state = onboard.read_state(target)
        self.assertIn("ontology.json", state.get("wrote") or {}, state)
        self.assertNotIn("sources/notes.txt", state.get("wrote") or {})
        if mode == "new":
            # dropped in the folder between the runs: the user's, not setup's
            write(os.path.join(target, "sources", "notes.txt"), "my own notes\n")
        code, obj, err = self.setup(*argv, cwd=cwd)
        self.assertEqual(code, 0, (kind, mode, obj, err))
        commit = step(obj, "commit")
        self.assertEqual(commit["status"], "done", (kind, mode, commit))
        self.assertEqual(_support.git(target, "log", "-1", "--format=%s"), "Start garden")
        self.assertIn("ontology.json", tracked(target))
        self.assertNotIn("sources/notes.txt", tracked(target))
        self.assertIn("sources/notes.txt", commit["detail"])
        self.assertEqual(dirty(target), ["sources/notes.txt"])

    def test_a_rerun_after_a_failed_commit_commits_setups_files(self):
        for kind in ("identity", "hook", "sign"):
            for mode in ("here", "new"):
                with self.subTest(kind=kind, mode=mode):
                    self.check_rerun(kind, mode)

    def test_a_file_setup_wrote_and_the_user_changed_since_stays_out(self):
        target = os.path.join(self.place, "changed")
        argv = ARGS + ("--new", target)
        self.run_failing("sign", *argv)
        wrote = onboard.read_state(target).get("wrote") or {}
        mine = sorted(p for p in wrote if p != "ontology.json" and not p.startswith(".git"))
        self.assertTrue(mine, wrote)
        edited = mine[0]
        with open(os.path.join(target, *edited.split("/")), "a", encoding="utf-8") as fh:
            fh.write("\nthe user's own line\n")
        code, obj, err = self.setup(*argv)
        self.assertEqual(code, 0, (obj, err))
        commit = step(obj, "commit")
        self.assertEqual(commit["status"], "done", commit)
        self.assertIn("ontology.json", tracked(target))
        self.assertNotIn(edited, tracked(target))
        self.assertIn(edited, commit["detail"])
        self.assertIn("changed since setup wrote", commit["detail"])
        self.assertEqual(dirty(target), [edited])

    def test_a_rerun_after_a_failed_record_commit_commits_it(self):
        target = os.path.join(self.place, "record")
        code, obj, err = self.setup(*ARGS, "--new", target)
        self.assertEqual(code, 0, (obj, err))
        head = _support.git(target, "rev-parse", "HEAD")
        argv = ARGS + ("--new", target, "--packs", "assessment")
        self.run_failing("sign", *argv)
        self.assertEqual(_support.git(target, "rev-parse", "HEAD"), head)
        code, obj, err = self.setup(*argv)
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(step(obj, "packs")["status"], "already")
        self.assertEqual(step(obj, "commit")["status"], "done", obj)
        self.assertEqual(_support.git(target, "log", "-1", "--format=%s"), "Record the setup of record")
        self.assertIn("assessment", _support.git(target, "show", "HEAD:ontology.json"))
        self.assertEqual(dirty(target), [])


class CredentialTest(SetupCase):
    def test_a_credential_in_a_file_setup_wrote_stops_the_commit(self):
        clone = self.clone_template("cred")
        head = _support.git(clone, "rev-parse", "HEAD")
        secret = _support.fake_secret("slack")
        real = onboard.Setup.do_packs

        def packs(setup):
            write(os.path.join(clone, "ledger", "scratch.md"), "token %s\n" % secret)
            return real(setup)

        with mock.patch.object(onboard.Setup, "do_packs", packs):
            code, obj, err = self.setup("--here", *ARGS, "--title", "Garden plots", cwd=clone)
        self.assertEqual(code, 0, (obj, err))
        commit = step(obj, "commit")
        self.assertEqual(commit["status"], "skipped", commit)
        self.assertIn("ledger/scratch.md", commit["detail"])
        self.assertIn("credential-like", commit["detail"])
        self.assertNotIn(secret, json.dumps(obj))
        self.assertEqual(_support.git(clone, "rev-parse", "HEAD"), head)

    def test_a_rerun_commits_the_file_once_the_credential_is_out(self):
        clone = self.clone_template("cred-rerun")
        secret = _support.fake_secret("slack")
        scratch = os.path.join(clone, "ledger", "scratch.md")
        real = onboard.Setup.do_packs

        def packs(setup):
            if not os.path.exists(scratch):  # setup writes it once; the rerun finds the user's cleaned copy
                write(scratch, "token %s\n" % secret)
            return real(setup)

        argv = ("--here",) + ARGS + ("--title", "Garden plots")
        with mock.patch.object(onboard.Setup, "do_packs", packs):
            code, obj, err = self.setup(*argv, cwd=clone)
            self.assertEqual(code, 0, (obj, err))
            self.assertEqual(step(obj, "commit")["status"], "skipped", obj)
            self.assertEqual(onboard.read_state(clone).get("held"), ["ledger/scratch.md"])
            # still holding the secret: the rerun stops again and keeps the file setup's
            code, obj, err = self.setup(*argv, cwd=clone)
            self.assertEqual(step(obj, "commit")["status"], "skipped", obj)
            self.assertIn("credential-like", step(obj, "commit")["detail"])
            self.assertEqual(onboard.read_state(clone).get("held"), ["ledger/scratch.md"])
            # the user takes the secret out as told and runs setup again
            write(scratch, "token removed\n")
            code, obj, err = self.setup(*argv, cwd=clone)
        self.assertEqual(code, 0, (obj, err))
        commit = step(obj, "commit")
        self.assertEqual(commit["status"], "done", commit)
        self.assertNotIn("not committed", commit["detail"])
        self.assertIn("ledger/scratch.md", tracked(clone))
        self.assertIn("ontology.json", tracked(clone))
        self.assertEqual(dirty(clone), [])
        state = onboard.read_state(clone)
        self.assertEqual((state.get("wrote"), state.get("held")), ({}, []))

    def test_a_held_file_never_carries_the_users_preexisting_files(self):
        clone = self.clone_template("cred-mine")
        write(os.path.join(clone, "sources", "notes.txt"), "my own notes\n")
        secret = _support.fake_secret("slack")
        scratch = os.path.join(clone, "ledger", "scratch.md")
        real = onboard.Setup.do_packs

        def packs(setup):
            if not os.path.exists(scratch):
                write(scratch, "token %s\n" % secret)
            return real(setup)

        argv = ("--here",) + ARGS + ("--title", "Garden plots")
        with mock.patch.object(onboard.Setup, "do_packs", packs):
            self.setup(*argv, cwd=clone)
            write(scratch, "token removed\n")
            code, obj, err = self.setup(*argv, cwd=clone)
        self.assertEqual(code, 0, (obj, err))
        commit = step(obj, "commit")
        self.assertEqual(commit["status"], "done", commit)
        self.assertIn("ledger/scratch.md", tracked(clone))
        self.assertNotIn("sources/notes.txt", tracked(clone))
        self.assertIn("sources/notes.txt", commit["detail"])
        self.assertEqual(dirty(clone), ["sources/notes.txt"])
