"""Review round 3 of kit 0.2.0: one regression test per confirmed finding that a code change fixed.

Setup and doctor act on the folder they run in, never on a topic ``$ONTO_REPO`` names elsewhere (the handoff
included). Setup's first commit takes only the topic's own files: other files in the folder (a ``.env``, notes,
something staged before) stay out and are named, and a credential in a file to commit stops it. Doctor outside a
topic or a template checkout walks nothing. A second one-click run makes a new topic instead of reopening the first
one, while an unfinished run is still resumed. In the viewer, focus goes back to the last page link used.
"""

from __future__ import annotations

import json
import os
import shutil
import unittest
from unittest import mock

from tests import _support
from tests.test_setup import SetupCase
from ontokit import doctor, handoff, onboard


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def step(obj, sid):
    return next(s for s in obj["steps"] if s["id"] == sid)


def read_manifest(root):
    with open(os.path.join(root, "ontology.json"), encoding="utf-8") as fh:
        return json.load(fh)


def committed_files(root):
    return _support.git(root, "show", "--name-only", "--format=", "HEAD").split("\n")


class OntoRepoElsewhereTest(SetupCase):
    def setUp(self):
        super().setUp()
        self.other = os.path.join(self.place, "tomato-beds")
        code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", self.other)
        self.assertEqual(code, 0, (obj, err))
        self.other_head = _support.git(self.other, "rev-parse", "HEAD")
        self.clone = self.clone_template("h3")

    def test_setup_here_sets_up_this_clone_not_the_onto_repo_topic(self):
        with mock.patch.dict(os.environ, {"ONTO_REPO": self.other}):
            code, obj, err = self.setup("--here", "--yes", "--launch", "none", "--plugin", "skip", "--packs",
                                        "assessment", cwd=self.clone)
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(os.path.realpath(obj["topic"]["path"]), os.path.realpath(self.clone))
        self.assertEqual(_support.git(self.clone, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertTrue(os.path.isfile(os.path.join(self.clone, "ontology.json")))
        self.assertEqual(_support.git(self.other, "rev-parse", "HEAD"), self.other_head)
        self.assertNotIn("assessment", json.dumps(read_manifest(self.other)))

    def test_doctor_reports_the_template_checkout_it_runs_in(self):
        old = os.getcwd()
        os.chdir(self.clone)
        try:
            with mock.patch.dict(os.environ, {"ONTO_REPO": self.other}):
                code, out, err = _support.run_cli(["doctor", "--json"])
        finally:
            os.chdir(old)
        obj = json.loads(out)
        self.assertEqual(obj["where"], "template", (obj, err))
        self.assertEqual(os.path.realpath(obj["root"]), os.path.realpath(self.clone))

    def test_the_handoff_never_sends_setup_or_doctor_to_the_onto_repo_topic(self):
        env = {"ONTO_REPO": self.other, "CLAUDE_PROJECT_DIR": self.other}
        for argv in (["setup", "--yes"], ["--json", "doctor"]):
            self.assertIsNone(handoff.target(argv, "onto", cwd=self.clone, env=env), argv)
        # every other command still finds the topic through $ONTO_REPO
        launcher = handoff.target(["status"], "onto", cwd=self.clone, env=env)
        self.assertEqual(os.path.realpath(launcher), os.path.realpath(
            os.path.join(self.other, "plugins", "general-ontology", "bin", "onto")))


class FirstCommitTest(SetupCase):
    def test_other_files_in_the_folder_stay_out_of_the_start_commit(self):
        clone = self.clone_template("h2")
        write(os.path.join(clone, ".env"), "AWS_ACCESS_KEY_ID=%s\n" % _support.fake_secret("aws"))
        write(os.path.join(clone, "notes.txt"), "my own notes\n")
        write(os.path.join(clone, "staged.txt"), "staged before setup\n")
        _support.git(clone, "add", "staged.txt")
        code, obj, err = self.setup("--here", "--yes", "--launch", "none", "--plugin", "skip", "--title",
                                    "Garden plots", cwd=clone)
        self.assertEqual(code, 0, (obj, err))
        commit = step(obj, "commit")
        self.assertEqual(commit["status"], "done", commit)
        self.assertIn("not committed: 3 files setup did not write", commit["detail"])
        self.assertEqual(_support.git(clone, "log", "-1", "--format=%s"), "Start garden-plots")
        files = committed_files(clone)
        self.assertIn("ontology.json", files)
        for name in (".env", "notes.txt", "staged.txt"):
            self.assertNotIn(name, files)
        status = _support.git(clone, "status", "--porcelain")
        for name in (".env", "notes.txt", "staged.txt"):
            self.assertIn(name, status)
        # the topic itself is fully committed
        self.assertEqual(sorted(line[3:] for line in status.splitlines()), [".env", "notes.txt", "staged.txt"])

    def test_a_credential_in_a_file_to_commit_stops_the_commit(self):
        # round 7: a file in a topic folder that was there before setup ran is the user's, so it stays out of the
        # commit (and is never read for the scan); a credential in a file setup wrote still stops the commit
        # (test_review_round7.CredentialTest)
        clone = self.clone_template("h5")
        write(os.path.join(clone, "ledger", "scratch.md"), "token %s\n" % _support.fake_secret("slack"))
        code, obj, err = self.setup("--here", "--yes", "--launch", "none", "--plugin", "skip", "--title",
                                    "Garden plots", cwd=clone)
        self.assertEqual(code, 0, (obj, err))
        commit = step(obj, "commit")
        self.assertEqual(commit["status"], "done", commit)
        self.assertIn("not committed: 1 file setup did not write (ledger/scratch.md)", commit["detail"])
        self.assertNotIn(_support.fake_secret("slack"), json.dumps(obj))
        self.assertNotIn("ledger/scratch.md", committed_files(clone))
        self.assertIn("ledger/scratch.md", _support.git(clone, "status", "--porcelain", "--untracked-files=all"))


class DoctorOutsideTest(_support.TempCase):
    def test_doctor_in_home_walks_no_files(self):
        home = os.path.join(self.tmp, "home")
        for i in range(3):
            for j in range(20):
                write(os.path.join(home, "Desktop", "d%d" % i, "f%d" % j), "x")
        write(os.path.join(home, "Desktop", "notes 2.txt"), "a conflict copy name")
        seen = []
        real = doctor._lstat

        def spy(path):
            seen.append(path)
            return real(path)

        env = {"HOME": home, "PATH": os.environ.get("PATH", "")}
        with mock.patch.object(doctor, "_lstat", side_effect=spy):
            obj = doctor.run_checks(home, env)
        self.assertEqual(obj["where"], "neither")
        # only the cheap look at the folder's own ontology.json and git index (evicted_core), no walk below it
        self.assertEqual([p for p in seen if os.path.dirname(p) != home and ".git" not in p], [], seen)
        self.assertLessEqual(len(seen), 3, seen)
        checks = {c["id"]: c for c in obj["checks"]}
        self.assertEqual(checks["dataless"]["status"], "ok")
        self.assertIn("not checked", checks["dataless"]["detail"])
        self.assertEqual(checks["conflict_copies"]["status"], "ok")


class FreeNameTest(SetupCase):
    def test_a_second_default_run_makes_a_new_topic(self):
        args = ("--yes", "--launch", "none", "--plugin", "skip")
        code, obj, err = self.setup(*args)
        self.assertEqual(code, 0, (obj, err))
        first = os.path.join(self.home, "Ontologies", "my-topic")
        self.assertEqual(obj["topic"]["path"], first)
        head = _support.git(first, "rev-parse", "HEAD")
        code, obj, err = self.setup(*args)
        self.assertEqual(code, 0, (obj, err))
        second = os.path.join(self.home, "Ontologies", "my-topic-2")
        self.assertEqual(obj["topic"]["path"], second)
        self.assertEqual(obj["topic"]["name"], "my-topic-2")
        self.assertEqual(step(obj, "init")["status"], "done")
        self.assertIn("~/Ontologies/my-topic already holds a topic", step(obj, "preflight")["detail"])
        self.assertEqual(_support.git(first, "rev-parse", "HEAD"), head)
        # an unfinished run is resumed, not passed over
        state = onboard.read_state(second)
        state.pop("done")
        with open(os.path.join(second, *onboard.STATE_REL), "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        code, obj, err = self.setup(*args)
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(obj["topic"]["path"], second)
        self.assertEqual(step(obj, "init")["status"], "already")
        # an explicit folder never moves
        code, obj, err = self.setup(*args, "--new", first)
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(obj["topic"]["path"], first)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PeekOpenerTest(_support.TempCase):
    def test_focus_goes_back_to_the_last_page_link_used(self):
        from tests.test_build_release import ViewerDomTest
        from ontokit import build, store

        case = ViewerDomTest("test_untrusted_edges_are_marked")
        case.tmp = self.tmp
        root = _support.make_topic(self.tmp, "mini", "mini")
        build.write(store.Repo.open(root), html=True)
        steps = [{"peek": "#node/role:plot-coordinator"}, {"peek": "#node/role:bed-steward"}, {"key": "Escape"}]
        _load, first, second, closed = case.page(root, "#kind/role", steps, prefer_view=True)
        self.assertEqual(first["peek"][0], "Plot coordinator")
        self.assertEqual(second["peek"][0], "Bed steward")
        self.assertIsNone(closed["peek"])
        self.assertEqual(closed["focus_href"], "#node/role:bed-steward")
        # a link inside the panel still hands focus back to the page link it came from
        steps = [{"peek": "#node/role:plot-coordinator"}, {"peek": "#node/dataset:harvest-log"}, {"key": "Escape"}]
        closed = case.page(root, "#kind/role", steps)[-1]
        self.assertEqual(closed["focus_href"], "#node/role:plot-coordinator")


if __name__ == "__main__":
    unittest.main()
