"""Review round 4 of kit 0.2.0: one regression test per confirmed finding that a code change fixed.

Release: the commit-metadata scan refuses only a push, and the kit's own history (on ``kit``) is never scanned.
Setup: the answers file's values are checked before anything is written; a failed plugin install leaves a ready
topic that opens with ``--plugin-dir``; a folder marketplace stops a project install too; prompts ask again; an
OS-written ``.DS_Store`` counts as empty; ``--here`` needs a clone and makes ``main`` from any branch; remote URLs
with a line break or a leading ``-`` are refused; skipped setup questions are recorded; the kept answers file is
redacted. Doctor: conflict copies need their original, stale ``HEAD.lock`` counts, an evicted graph file skips the
topic checks, cloud folders match without case on macOS and Windows, the branch fix names ``HEAD`` when the remote
copy is older, a local kit remote is not a warning, a ZIP template is. The interview: answer text from a file, a
retried answer with a direction mark, ``next --about`` takes nodes only, drafted links come back as questions,
``onto log`` names the question. Decisions: W10 names the active end of a supersede chain. Review: a proposal that
would be refused for a P23 says so before any verdict.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests import _support
from tests.test_setup import KIT_URL, REPO_ROOT, SYSTEM_PATH, SetupCase, decisions, git_env, make_template, \
    write_stub
from ontokit import cli, commands, doctor, gitutil, graph, interview, onboard, release, store, validate
from ontokit.errors import UsageError


def clear():
    graph.clear_cache()
    store.clear_cache()


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False))
    return path


def step(obj, sid):
    return [s for s in obj.get("steps") or [] if s["id"] == sid][0]


def node_rows(root):
    rows, _bad = store.read_jsonl(os.path.join(root, "graph", "nodes.jsonl"))
    return rows


def run(root, *args):
    code, out, err = _support.run_cli(list(args), repo=root)
    return code, out, err


def run_json(root, *args):
    code, out, err = _support.run_cli(list(args) + ["--json"], repo=root)
    return code, (json.loads(out) if out.strip() else {}), err


# release -------------------------------------------------------------------------------------------------------
class ReleaseCommitScanTest(_support.TempCase):
    def test_a_denylisted_author_blocks_only_a_push(self):
        root = _support.init_topic(self.tmp, "clinic", "Clinic ops")
        write(os.path.join(root, ".gitignore"), "inbox/\n.onto/\n")
        _support.git_init(root)
        subprocess.run(["git", "-C", root, "-c", "user.name=Ann Smith", "-c", "user.email=ann@example.invalid",
                        "add", "-A"], check=True, env=dict(os.environ, **_support.GIT_ENV))
        subprocess.run(["git", "-C", root, "-c", "user.name=Ann Smith", "-c", "user.email=ann@example.invalid",
                        "commit", "-q", "-m", "Start"], check=True, env=dict(os.environ, **_support.GIT_ENV))
        deny = write(os.path.join(self.tmp, "deny.txt"), "Ann Smith\n")
        local = release.plan(store.Repo.open(root), "first", denylist=deny, write=True)
        self.assertEqual(local["exit_code"], 0, local["failures"])
        self.assertEqual(local["scan"]["hits"], [])
        pushed = release.plan(store.Repo.open(root), "first", denylist=deny, push=True)
        self.assertEqual(pushed["exit_code"], 2)
        self.assertTrue(any("before a push" in f for f in pushed["failures"]), pushed["failures"])

    def test_the_kit_history_on_the_kit_remote_is_never_scanned(self):
        kit = os.path.join(self.tmp, "kit")
        _support.git_init(kit)
        write(os.path.join(kit, "README.md"), "kit\n")
        subprocess.run(["git", "-C", kit, "-c", "user.name=Kit Author Zed", "-c", "user.email=zed@example.invalid",
                        "add", "-A"], check=True, env=dict(os.environ, **_support.GIT_ENV))
        subprocess.run(["git", "-C", kit, "-c", "user.name=Kit Author Zed", "-c", "user.email=zed@example.invalid",
                        "commit", "-q", "-m", "kit by zed"], check=True, env=dict(os.environ, **_support.GIT_ENV))
        root = os.path.join(self.tmp, "topic")
        _support.git(self.tmp, "clone", "-q", kit, root)
        _support.git(root, "remote", "rename", "origin", "kit")  # what onto setup does
        for key, value in (("user.name", "Onto Test"), ("user.email", "test@example.invalid"),
                           ("commit.gpgsign", "false"), ("core.hooksPath", os.devnull)):
            _support.git(root, "config", key, value)
        from ontokit import mutate

        mutate.init_topic(root, "topic", "topic", "Topic")
        write(os.path.join(root, ".gitignore"), "inbox/\n.onto/\n")
        _support.commit_all(root, "Start topic")
        deny = write(os.path.join(self.tmp, "deny.txt"), "Kit Author Zed\nzed\n")
        report = release.plan(store.Repo.open(root), "first", denylist=deny, push=True)
        self.assertEqual(report["scan"]["hits"], [], report["scan"]["hits"])


# setup: the answers file ------------------------------------------------------------------------------------------
class AnswerValuesTest(SetupCase):
    def refused(self, data, *extra):
        target = os.path.join(self.place, "topic-a")
        code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target,
                                    "--answers", json.dumps(data, ensure_ascii=False), *extra)
        self.assertEqual(code, 2, (obj, err))
        self.assertFalse(os.path.exists(target), "nothing is written before the values are checked")
        return obj["message"]

    def test_values_decide_and_answer_refuse_fail_before_anything_is_written(self):
        message = self.refused({"answers": [{"q": "q.nope.unknown", "text": "hello"}]})
        self.assertIn("unknown question q.nope.unknown", message)
        message = self.refused({"decisions": [{"question": "Who reviews?", "chosen": "x", "decided_by": "boss"}]})
        self.assertIn("decided_by must be one of user, owner, team, agent", message)
        message = self.refused({"decisions": [{"question": "Color?", "options": ["a=Alpha", "b=Beta"],
                                               "chosen": "c"}]})
        self.assertIn("chosen 'c' is not one of the options (a, b)", message)
        token = _support.fake_secret("github")
        message = self.refused({"setup": {"summary": "About bees %s" % token}})
        self.assertIn("setup.summary holds credential-like text (github)", message)
        self.assertNotIn(token, message)
        message = self.refused({"setup": {"summary": "A plan for keeping ‮bees‬ in the city."}})
        self.assertIn("setup.summary", message)
        self.assertIn("U+202E", message)
        message = self.refused({"decisions": [{"question": "Who keeps it?", "chosen": "solo",
                                               "rationale": "see %s" % token}]})
        self.assertIn("decisions[0].rationale holds credential-like text", message)
        message = self.refused({"setup": {"summary": "Call (555) 555-0199 about it."}}, "--personal", "refuse")
        self.assertIn("personal data the policy refuses (phone)", message)

    def test_good_values_still_go_through(self):
        target = os.path.join(self.place, "topic-b")
        data = {"answers": [{"q": "q.frame.goal", "text": "Decide which beds to rotate"}],
                "decisions": [{"question": "Color?", "options": ["a=Alpha", "b=Beta"], "chosen": "b",
                               "recommended": "a", "decided_by": "team"}]}
        code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target,
                                    "--answers", json.dumps(data))
        self.assertEqual(code, 0, (obj, err))

    def test_a_refused_kit_answer_names_its_problems(self):
        text = onboard._message({"message": "answer refused: 1 problem; nothing was written", "problems": [
            {"n": 1, "code": "P02", "message": "$.ops[0].set.summary: holds U+202E"}]})
        self.assertIn("P02 $.ops[0].set.summary: holds U+202E", text)


class LocationAndSkipsTest(SetupCase):
    def test_a_new_flag_that_overrides_setup_new_is_noted_and_not_recorded(self):
        typed = os.path.join(self.place, "choir-onto")
        given = os.path.join(self.place, "choir")
        data = {"setup": {"title": "Choir", "new": typed, "plugin": "skip"}}
        code, obj, err = self.setup("--launch", "none", "--new", given, "--answers", json.dumps(data))
        self.assertEqual(code, 0, (obj, err))
        self.assertIn("overrides the answers file's setup.new", step(obj, "preflight")["detail"])
        self.assertNotIn(onboard.Q_LOCATION, {d["question"] for d in decisions(given)})
        self.assertFalse(os.path.exists(typed))

    def test_skipped_questions_are_recorded_and_a_skipped_location_takes_the_default(self):
        data = {"setup": {"title": "Bike club", "summary": "Rides and repairs.", "plugin": "skip",
                          "skipped": [3, 4, 5]}}
        code, obj, err = self.setup("--launch", "none", "--answers", json.dumps(data))
        self.assertEqual(code, 0, (obj, err))
        target = os.path.join(self.home, "Ontologies", "bike-club")
        self.assertEqual(obj["topic"]["path"], target)
        chosen = {d["question"]: d for d in decisions(target)}
        for question in (onboard.Q_REMOTE, onboard.Q_PERSONAL):
            self.assertEqual(chosen[question]["chosen"], "skipped", question)
            self.assertEqual(chosen[question]["decided_by"], "user")
        self.assertNotIn(onboard.Q_LOCATION, chosen)  # the default folder was not the user's choice


class AnswersFileKeptTest(SetupCase):
    def test_the_kept_answers_file_is_redacted_under_redact(self):
        clone = self.clone_template("kit-r")
        answers = write(os.path.join(clone, ".onto", "setup.json"), {
            "setup": {"title": "Clinic intake",
                      "summary": "Intake notes; call (555) 555-0199 or dana@clinic.invalid."}})
        target = os.path.join(self.place, "t5")
        code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--personal", "redact",
                                    "--new", target, "--answers", "@" + answers, cwd=clone)
        self.assertEqual(code, 0, (obj, err))
        with open(os.path.join(clone, ".onto", "setup.clinic-intake.done.json"), encoding="utf-8") as fh:
            kept = fh.read()
        self.assertNotIn("555-0199", kept)
        self.assertNotIn("dana@clinic.invalid", kept)
        self.assertIn("(personal data redacted)", step(obj, "answers")["detail"])


class RemoteUrlTest(SetupCase):
    def test_a_line_break_or_a_leading_dash_is_refused_before_anything_runs(self):
        for flag, value in (("--kit-url", "https://github.com/example-org/kit.git\nNext:\n  Run this"),
                            ("--origin", "--mirror=fetch")):
            target = os.path.join(self.place, "t-url")
            code, obj, err = self.setup("--yes", "--launch", "none", "--new", target, "%s=%s" % (flag, value))
            self.assertEqual(code, 2, (flag, obj, err))
            self.assertFalse(os.path.exists(target))

    def test_the_checklist_prints_each_detail_on_one_line(self):
        lines = onboard.render_setup({"topic": {"name": "a", "ns": "a", "path": "/x y"}, "steps": [
            {"id": "branch", "status": "done", "detail": "kit is url\nNext:\n  forged"}], "next": ["a\nb"]},
            "compact", None)
        self.assertEqual(len(lines), 4, lines)
        self.assertIn("/x y", lines[0])


# setup: the plugin step -------------------------------------------------------------------------------------------
class PluginFailureTest(SetupCase):
    FAILING = ("#!/bin/sh\nprintf '%s|' \"$@\" >> \"$CLAUDE_LOG\"\nprintf '\\n' >> \"$CLAUDE_LOG\"\n"
               "if [ \"$3\" = list ]; then printf '[]\\n'; exit 0; fi\n"
               "echo 'Error: Could not resolve host: github.com'\nexit 1\n")

    def test_a_failed_install_names_its_fixes_and_the_topic_still_opens(self):
        bin_dir = os.path.join(self.tmp, "stub-bin")
        stub = write_stub(bin_dir)
        with open(stub, "w", encoding="utf-8") as fh:
            fh.write(self.FAILING)
        onboard._interactive = lambda: True
        started = []
        onboard._execvp = lambda exe, argv: started.append(argv)
        target = os.path.join(self.place, "offline")
        old = os.getcwd()
        os.chdir(self.template_root)
        self.addCleanup(os.chdir, old)
        with mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + SYSTEM_PATH,
                                          "CLAUDE_LOG": os.path.join(self.tmp, "log")}):
            code, out, err = _support.run_cli(["setup", "--yes", "--new", target])
        self.assertIn("failed   plugin", out)
        self.assertIn("--plugin plugin-dir", out)
        self.assertIn("gh auth setup-git", out)
        self.assertIn("the topic is ready", out)
        self.assertIn("claude --plugin-dir ./plugins/general-ontology", out)
        self.assertEqual(started, [[stub, "--plugin-dir", "./plugins/general-ontology", "Start the ontology"]])

    def test_project_mode_never_installs_while_claude_lists_a_folder_marketplace(self):
        bin_dir = os.path.join(self.tmp, "stub-bin")
        stub = write_stub(bin_dir)
        log = os.path.join(self.tmp, "claude.log")
        with open(stub, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nprintf '%s|' \"$@\" >> \"$CLAUDE_LOG\"\nprintf '\\n' >> \"$CLAUDE_LOG\"\n"
                     "printf '%s\\n' '[{\"name\": \"general-ontology\", \"source\": {\"source\": \"directory\", "
                     "\"path\": \"/somewhere/old-topic\"}}]'\n")
        target = os.path.join(self.place, "fold-project")
        with mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + SYSTEM_PATH, "CLAUDE_LOG": log}):
            code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", "project", "--new", target)
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(step(obj, "plugin")["status"], "skipped")
        self.assertIn("local folder", step(obj, "plugin")["detail"])
        with open(log, encoding="utf-8") as fh:
            self.assertNotIn("install", fh.read())
        self.assertIn("--plugin-dir ./plugins/general-ontology", " ".join(obj["next"]))


# setup: prompts, folders and branches -----------------------------------------------------------------------------
class PromptAgainTest(SetupCase):
    def test_a_bad_name_is_asked_again_and_a_typed_name_goes_under_ontologies(self):
        onboard._interactive = lambda: True
        replies = iter(["new", "Rose growing", "Rose Garden", "", "", "herbs"])
        asked = []
        with mock.patch.object(onboard, "_input", side_effect=lambda q: (asked.append(q), next(replies))[1]):
            code, obj, err = self.setup("--launch", "none", "--plugin", "skip")
        self.assertEqual(code, 0, (obj, err))
        self.assertIn("Please use", asked[3])
        self.assertIn("[rose-garden]", asked[3])
        self.assertIn("[rose-garden]", asked[4])  # the namespace comes from the slug, first letter kept
        self.assertEqual(obj["topic"]["name"], "rose-garden")
        self.assertEqual(obj["topic"]["path"], os.path.join(self.home, "Ontologies", "herbs"))

    def test_a_typed_folder_inside_the_kit_is_asked_again(self):
        onboard._interactive = lambda: True
        inside = os.path.join(self.template_root, "herbs")
        outside = os.path.join(self.place, "herbs-out")
        replies = iter(["new", "Herb garden", "", "", inside, outside])
        asked = []
        with mock.patch.object(onboard, "_input", side_effect=lambda q: (asked.append(q), next(replies))[1]):
            code, obj, err = self.setup("--launch", "none", "--plugin", "skip")
        self.assertEqual(code, 0, (obj, err))
        self.assertIn("Pick another folder", asked[-1])
        self.assertEqual(obj["topic"]["path"], outside)


class FolderAndBranchTest(SetupCase):
    def test_a_folder_holding_only_ds_store_counts_as_empty(self):
        target = os.path.join(self.place, "dotonly")
        write(os.path.join(target, ".DS_Store"), "x")
        code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target)
        self.assertEqual(code, 0, (obj, err))
        self.assertTrue(os.path.isfile(os.path.join(target, "ontology.json")))
        busy = os.path.join(self.place, "busy")
        write(os.path.join(busy, "notes.md"), "x")
        code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", busy)
        self.assertEqual(code, 1)
        self.assertIn("(it holds notes.md)", step(obj, "preflight")["detail"])

    def test_here_on_another_branch_makes_main_without_an_upstream(self):
        clone = self.clone_template("here1")
        _support.git(clone, "checkout", "-q", "-b", "agent/work", "--track", "origin/general-ontology")
        _support.git(clone, "branch", "-D", "general-ontology")
        code, obj, err = self.setup("--here", "--yes", "--launch", "none", "--plugin", "skip", cwd=clone)
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(_support.git(clone, "symbolic-ref", "--short", "HEAD"), "main")
        proc = subprocess.run(["git", "-C", clone, "rev-parse", "--abbrev-ref", "main@{upstream}"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ, **_support.GIT_ENV))
        self.assertNotEqual(proc.returncode, 0, proc.stdout)

    def test_a_zip_download_is_refused_by_here_and_doctor_says_clone(self):
        zipped = os.path.join(self.place, "zip")
        os.makedirs(zipped)
        archive = subprocess.run(["git", "-C", self.template_root, "archive", "general-ontology"],
                                 stdout=subprocess.PIPE, check=True).stdout
        subprocess.run(["tar", "-x", "-C", zipped], input=archive, check=True)
        code, obj, err = self.setup("--here", "--yes", "--launch", "none", cwd=zipped)
        self.assertEqual(code, 2, (obj, err))
        self.assertIn("not a git repo", obj["message"])
        where = [c for c in doctor.run_checks(zipped, dict(os.environ))["checks"] if c["id"] == "where"][0]
        self.assertEqual(where["status"], "warn")
        self.assertIn("git clone", where["fix"])


@unittest.skipIf(os.name == "nt", "a POSIX launcher")
class LauncherSymlinkTest(SetupCase):
    def test_new_topic_runs_through_a_symlink(self):
        link_dir = os.path.join(self.tmp, "user bin")
        os.makedirs(link_dir)
        os.symlink(os.path.join(self.template_root, "new-topic"), os.path.join(link_dir, "new-topic"))
        for shell in ("sh", "bash", "zsh"):
            if not shutil.which(shell):
                continue
            proc = subprocess.run([shell, "-c", 'PATH="$1:$PATH" new-topic --help', "_", link_dir],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=git_env(self.home))
            self.assertEqual(proc.returncode, 0, (shell, proc.stderr))
            self.assertIn(b"usage: onto setup", proc.stdout)


# doctor --------------------------------------------------------------------------------------------------------
class DoctorChecksTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        self.root = _support.init_topic(self.tmp, "garden", "Garden")
        write(os.path.join(self.root, ".gitignore"), "inbox/\n.onto/\n")
        _support.git_init(self.root)
        _support.commit_all(self.root, "start")

    def checks(self):
        return {c["id"]: c for c in doctor.run_checks(self.root, dict(os.environ, HOME=self.tmp))["checks"]}

    def test_user_files_with_numbers_are_not_conflict_copies(self):
        write(os.path.join(self.root, "inbox", "Budget 2025.xlsx"), "x")
        write(os.path.join(self.root, "Chapter 1.md"), "x")
        self.assertEqual(self.checks()["conflict_copies"]["status"], "ok")
        clear()
        self.assertEqual([p for p in store.path_problems(self.root) if p.file == "Chapter 1.md"], [])
        write(os.path.join(self.root, "Chapter.md"), "x")  # now it is a copy of something
        self.assertEqual(self.checks()["conflict_copies"]["status"], "warn")

    def test_a_stale_head_lock_is_reported(self):
        for name in ("HEAD.lock", "packed-refs.lock"):
            path = write(os.path.join(self.root, ".git", name), "")
            old = time.time() - 3600
            os.utime(path, (old, old))
        self.assertEqual(doctor.stale_locks(self.root), ["HEAD.lock", "packed-refs.lock"])
        self.assertEqual(self.checks()["stale_locks"]["status"], "warn")

    def test_an_evicted_graph_file_skips_the_topic_checks(self):
        real = os.lstat

        class Flags(object):
            def __init__(self, st):
                self.st_flags = doctor.DATALESS
                self._st = st

            def __getattr__(self, name):
                return getattr(self._st, name)

        def fake(path):
            st = real(path)
            return Flags(st) if path.endswith(os.path.join("graph", "edges.jsonl")) else st

        with mock.patch.object(doctor, "_lstat", fake):
            checks = self.checks()
        self.assertEqual(checks["dataless"]["status"], "fail")
        self.assertNotIn("validate", checks)
        self.assertNotIn("pending", checks)

    def test_cloud_folders_match_without_case_where_the_file_system_ignores_it(self):
        home = os.path.join(self.tmp, "home4")
        os.makedirs(os.path.join(home, "Library", "Mobile Documents", "com~apple~CloudDocs", "Desktop"))
        os.makedirs(os.path.join(home, "Desktop"))
        env = {"HOME": home}
        with mock.patch.object(doctor, "_CASE_FOLD", True):
            self.assertIsNotNone(doctor.cloud_synced(os.path.join(home, "desktop", "lc"), env))
            self.assertIsNotNone(doctor.cloud_synced(os.path.join(home, "LIBRARY", "CloudStorage", "x"), env))
        with mock.patch.object(doctor, "_CASE_FOLD", False):
            self.assertIsNone(doctor.cloud_synced(os.path.join(home, "LIBRARY", "CloudStorage", "x"), env))

    def test_a_local_kit_remote_is_not_a_plugin_warning(self):
        _support.git(self.root, "remote", "add", "kit", os.path.join(self.tmp, "kit checkout"))
        check = self.checks()["plugin"]
        self.assertEqual(check["status"], "ok", check)
        self.assertIn("kit checkout", check["detail"])
        self.assertIn("--plugin-dir", check["fix"])

    def test_an_identity_git_would_guess_is_not_one(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        bare = os.path.join(self.tmp, "bare")
        os.makedirs(bare)
        subprocess.run(["git", "-C", bare, "init", "-q"], check=True, env=env)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNotNone(gitutil.identity_problem(bare))
            env2 = dict(env, GIT_AUTHOR_NAME="A", GIT_AUTHOR_EMAIL="a@example.invalid", GIT_COMMITTER_NAME="A",
                        GIT_COMMITTER_EMAIL="a@example.invalid")
            with mock.patch.dict(os.environ, env2, clear=True):
                self.assertIsNone(gitutil.identity_problem(bare))


class BranchFixTest(SetupCase):
    def test_an_older_remote_copy_of_the_template_branch_is_not_suggested(self):
        clone = self.clone_template("kit2")
        init = os.path.join(clone, "plugins", "general-ontology", "ontokit", "__init__.py")
        with open(init, encoding="utf-8") as fh:
            text = fh.read()
        old = text.replace('__version__ = "%s"' % onboard.__version__, '__version__ = "0.1.0"')
        with open(init, "w", encoding="utf-8") as fh:
            fh.write(old)
        _support.commit_all(clone, "older kit")
        _support.git(clone, "update-ref", "refs/remotes/origin/general-ontology", "HEAD")
        with open(init, "w", encoding="utf-8") as fh:
            fh.write(text)
        _support.commit_all(clone, "newer kit")
        _support.git(clone, "checkout", "-q", "-b", "agent/new")
        _support.git(clone, "branch", "-D", "general-ontology")
        fix = doctor.missing_branch_fix(clone)
        self.assertIn("branch general-ontology HEAD", fix)
        self.assertIn("older", fix)


# the interview -------------------------------------------------------------------------------------------------
class InterviewTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        clear()
        self.root = _support.init_topic(self.tmp, "farm", "Farm")

    def test_answer_text_from_a_file_keeps_dollars_and_backticks(self):
        words = "A price list: we charge $5 a packet, see `echo INJECTED`"
        path = write(os.path.join(self.root, ".onto", "answer.txt"), words + "\n")
        code, obj, err = run_json(self.root, "answer", "q.frame.deliverable", "--text-file", path)
        self.assertEqual(code, 0, err)
        with open(os.path.join(self.root, "sources", obj["answer"]["src"] + ".txt"), encoding="utf-8") as fh:
            self.assertIn(words, fh.read())
        stdin = io.StringIO("Only the north beds.")
        out, errs = io.StringIO(), io.StringIO()
        code = cli.main(["answer", "q.frame.scope", "--text-file", "-", "--repo", self.root], stdout=out,
                        stderr=errs, stdin=stdin)
        self.assertEqual(code, 0, errs.getvalue())
        code, _out, err = run(self.root, "answer", "q.frame.you", "words", "--text-file", path)
        self.assertEqual(code, 2, err)
        self.assertIn("not both", err)

    def test_the_cli_hints_never_put_the_words_on_a_shell_line(self):
        code, out, err = run(self.root, "next")
        self.assertIn("--text-file .onto/answer.txt", out)
        self.assertNotIn("<the user's words>", out)

    def test_a_retried_answer_with_a_direction_mark_writes_nothing(self):
        ops = [{"op": "add_node", "ref": "$f", "node": {"kind": "role", "name": "Farmer ‏שדה",
                                                         "summary": "Runs the farm."},
                "basis": "stated", "conf": 0.9, "prov": [{"quote": "I run the farm", "by": "user"}]}]
        path = write(os.path.join(self.root, ".onto", "ops.json"), ops)
        for _ in range(2):
            code, out, err = run(self.root, "answer", "q.people.key", "I run the farm.", "--ops", "@" + path,
                                 "--apply")
            self.assertEqual(code, 0, out + err)
        clear()
        roles = [r["id"] for r in node_rows(self.root) if r["id"].startswith("role:")]
        self.assertEqual(len(roles), 1, roles)
        rows, _bad = store.read_jsonl(os.path.join(self.root, "interview", "log.jsonl"))
        self.assertEqual(len([r for r in rows if r.get("q") == "q.people.key"]), 1)

    def test_next_about_takes_a_node_id_only(self):
        code, obj, err = run_json(self.root, "status")
        onto = graph.Ontology.load(self.root)
        src = sorted(onto.sources)[0]
        with self.assertRaises(UsageError):
            interview.next_questions(onto, 3, about=src)
        code, out, err = run(self.root, "next", "--about", src)
        self.assertEqual(code, 2, out + err)
        self.assertIn("about takes a node id", err)

    def test_drafted_links_are_counted_asked_about_and_confirmed(self):
        ops = [{"op": "add_node", "ref": "$f", "basis": "stated", "node": {"kind": "role", "name": "Farmer",
                                                                           "summary": "Runs the farm."},
                "prov": [{"quote": "I run the farm"}]},
               {"op": "add_edge", "basis": "inferred", "edge": {"src": "$f", "rel": "part_of", "dst": "topic:farm"},
                "prov": [{"quote": "I run the farm"}]}]
        path = write(os.path.join(self.root, ".onto", "ops.json"), ops)
        code, out, err = run(self.root, "answer", "q.frame.you", "I run the farm.", "--ops", "@" + path, "--apply")
        self.assertEqual(code, 0, out + err)
        clear()
        code, out, err = run(self.root, "status")
        self.assertIn("(1 drafts, 0 bridges)", out)
        code, obj, err = run_json(self.root, "next", "--about", "role:farmer", "--n", "5")
        asked = [q for q in obj["questions"] if q["id"].startswith("q.gap.draft_link.")
                 and q["id"].endswith("@role:farmer")]  # round 5: the id names the set of drafted links
        self.assertEqual(len(asked), 1, [q["id"] for q in obj["questions"]])
        self.assertIn("part of Farm", asked[0]["ask"])
        confirm = [{"op": "add_edge", "basis": "stated", "edge": {"src": "role:farmer", "rel": "part_of",
                                                                  "dst": "topic:farm"},
                    "prov": [{"quote": "Yes, that is right"}]}]
        path = write(os.path.join(self.root, ".onto", "ops.json"), confirm)
        code, out, err = run(self.root, "answer", asked[0]["id"], "Yes, that is right.", "--ops",
                             "@" + path, "--apply")
        self.assertEqual(code, 0, out + err)
        clear()
        code, out, err = run(self.root, "status")
        self.assertIn("(0 drafts, 0 bridges)", out)

    def test_the_log_names_the_question_and_what_it_added(self):
        ops = [{"op": "add_node", "ref": "$f", "basis": "stated", "node": {"kind": "role", "name": "Farmer"},
                "prov": [{"quote": "I run the farm"}]},
               {"op": "add_edge", "basis": "stated", "edge": {"src": "$f", "rel": "part_of", "dst": "topic:farm"},
                "prov": [{"quote": "I run the farm"}]}]
        path = write(os.path.join(self.root, ".onto", "ops.json"), ops)
        self.assertEqual(run(self.root, "answer", "q.frame.you", "I run the farm.", "--ops", "@" + path,
                             "--apply")[0], 0)
        code, out, err = run(self.root, "log")
        self.assertIn("q.frame.you, added Farmer: 2 ops applied", out)


# decisions -----------------------------------------------------------------------------------------------------
class SupersedeChainTest(_support.TempCase):
    def test_w10_names_the_active_end_of_the_chain_and_its_advice_works(self):
        root = _support.init_topic(self.tmp, "shop", "Shop")

        def decide(*args):
            code, obj, err = run_json(root, "decide", *args)
            self.assertEqual(code, 0, (obj, err))
            return obj["decision"]["id"]

        a = decide("--question", "Sell online?", "--chosen", "yes", "--scope", "shop/")
        b = decide("--question", "Which platform?", "--chosen", "a stall site", "--scope", "shop/", "--narrows", a)
        c = decide("--question", "Sell online?", "--chosen", "only locally", "--scope", "shop/", "--supersedes", a)
        e = decide("--question", "Sell online?", "--chosen", "a little", "--scope", "shop/", "--supersedes", c)
        clear()
        code, out, err = run(root, "validate")
        self.assertIn("narrows %s, which is superseded by %s; record a decision that supersedes %s and narrows %s"
                      % (a, c, b, e), out)
        decide("--question", "Which platform now?", "--chosen", "a market page", "--scope", "shop/", "--supersedes",
               b, "--narrows", e)
        clear()
        code, out, err = run(root, "validate")
        self.assertNotIn("W10", out)


# review --------------------------------------------------------------------------------------------------------
class CalibrationPreviewTest(_support.TempCase):
    def test_propose_and_review_say_a_p23_before_any_verdict(self):
        root = _support.init_topic(self.tmp, "clinic", "Clinic")
        self.assertEqual(run(root, "pack", "add", "assessment")[0], 0)
        code, obj, err = run_json(root, "ingest", "--body",
                                  "Crowding at the door risk. Owner: clinic manager. Approved.", "--title", "Risk memo")
        self.assertEqual(code, 0, err)
        src = obj["sources"][0]["id"] if obj.get("sources") else obj["source"]["id"]
        prop = {"source": src, "summary": "risk", "ops": [
            {"op": "add_node", "ref": "$r", "basis": "stated", "node": {
                "kind": "risk", "name": "Crowding at the door", "attrs": {
                    "statement": "Crowding at the door", "owner": "Clinic manager", "status": "approved",
                    "ratings": ["safety.impact=high", "safety.inherent=high", "safety.residual=low"]}},
             "prov": [{"src": src, "loc": "L1-L1", "quote": "Crowding at the door risk", "by": "agent"}]},
            {"op": "add_edge", "basis": "stated", "edge": {"src": "$r", "rel": "threatens", "dst": "topic:clinic"},
             "prov": [{"src": src, "loc": "L1-L1", "quote": "Crowding at the door risk", "by": "agent"}]}]}
        path = write(os.path.join(root, ".onto", "prop.json"), prop)
        code, out, err = run(root, "propose", "--proposal", "@" + path)
        self.assertEqual(code, 0, out + err)
        self.assertIn("would be refused if accepted: P23", out)
        self.assertIn("attrs: status=approved", out)
        self.assertNotIn("fast triage", out)
        pid = [w for w in out.split() if w.startswith("prop-")][0]
        code, out, err = run(root, "review", pid)
        self.assertIn("would be refused if accepted: P23", out)
        self.assertIn("leaves attrs.status as it is", out)


# the viewer ------------------------------------------------------------------------------------------------------
class ViewerNoscriptTest(unittest.TestCase):
    def test_the_noscript_text_names_the_decisions_folder(self):
        from ontokit import ledger

        with open(os.path.join(_support.PLUGIN_DIR, "ontokit", "viewer", "template.html"), encoding="utf-8") as fh:
            text = fh.read()
        noscript = text.split("<noscript>", 1)[1].split("</noscript>", 1)[0]
        self.assertIn("decisions are in %s/." % ledger.DECISIONS_DIR, noscript)


# the docs ------------------------------------------------------------------------------------------------------
class DocsTest(unittest.TestCase):
    def read(self, *parts):
        with open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as fh:
            return " ".join(fh.read().split())

    def test_the_docs_keep_the_users_words_off_the_shell_and_name_the_commit_checks(self):
        agents = self.read("AGENTS.md")
        self.assertNotIn('onto answer <q> "<text>"', agents)
        self.assertIn("--text-file .onto/answer.txt", agents)
        # The long answer-recording rules moved to the kit docs, so the negative needle covers each of them too.
        docs = os.path.join(REPO_ROOT, "plugins", "general-ontology", "docs")
        names = sorted(f for f in os.listdir(docs) if f.endswith(".md"))
        self.assertIn("interviewing.md", names)
        for name in names:
            self.assertNotIn('onto answer <q> "<text>"', self.read(docs, name), name)
        self.assertIn("--text-file .onto/answer.txt", self.read(docs, "interviewing.md"))
        skill = self.read("plugins", "general-ontology", "skills", "onto-interview", "SKILL.md")
        self.assertNotIn('O answer <q> "<text>"', skill)
        self.assertNotIn("--new <their folder>", skill)
        claude = self.read("CLAUDE.md")
        self.assertIn("onto scan .", claude)
        self.assertIn("setup.new", claude)
        self.assertIn("run `onto scan .`", agents)
        erase = self.read("plugins", "general-ontology", "skills", "onto", "SKILL.md")
        self.assertIn("is refused until it is settled", erase)
        kit = self.read("plugins", "general-ontology", "README.md")
        self.assertNotIn("needs no topic and no network", kit)


if __name__ == "__main__":
    unittest.main()
