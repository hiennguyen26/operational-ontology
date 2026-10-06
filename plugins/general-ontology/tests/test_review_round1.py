"""Review round 1 of kit 0.2.0: one regression test per confirmed finding.

Setup (``onto setup``, the launchers, ``onto doctor``): credentials in a kit or origin URL never reach the committed
``.claude/settings.json`` or the claude command line; every answers-file value is type-checked before anything is
written; a broken ``ontology.json`` is a failing topic for doctor; git missing is named; the clone source is never a
topic or an older kit and the target is never inside it; tags stay behind; a relative folder means the caller's
folder; the plugin step follows the recorded decision; a skipped goal can be answered later and becomes a goal node;
setup's later commits never sweep in the user's files; the answers file is moved aside after a successful run; a
choice passed now wins over the file; a local origin is named by its last folder only; Windows runs Claude Code as a
child. Interview and ledger: a narrowing that inverts the chain is refused; the duplicate risk question closes; the
ask is never cut; a P23 shows in the preview and a refused answer leaves nothing behind; a new W09 is named.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import unittest
from unittest import mock

from tests import _support
from tests.test_setup import (KIT_URL, NO_ANSWERS, SPECIAL, SYSTEM_PATH, SetupCase, decisions, make_template,
                              read_json, write_stub)
from ontokit import doctor, graph, interview, mcp_server, onboard, pipeline, store
from ontokit.errors import Refused


def clear():
    graph.clear_cache()
    store.clear_cache()


def token():
    return _support.fake_secret("github")


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(data if isinstance(data, str) else json.dumps(data))
    return path


def log_rows(root):
    rows, _bad = store.read_jsonl(os.path.join(root, "interview", "log.jsonl"))
    return rows


def claude_calls(log):
    if not os.path.exists(log):
        return []
    with open(log, encoding="utf-8") as fh:
        return [line.rstrip("|\n").split("|") for line in fh if line.strip()]


# credentials in URLs ------------------------------------------------------------------------------------------
class CredentialUrlTest(SetupCase):
    def test_a_token_in_the_templates_remote_never_reaches_settings_or_claude(self):
        secret = token()
        for url in ("https://alice:%s@github.com/acme/kit.git" % secret, "https://%s@github.com/acme/kit.git" % secret):
            source = self.credentialed_template_for(url)
            target = os.path.join(self.place, "garden-%d" % len(url))
            code, obj, err = self.setup("--yes", "--launch", "none", "--new", target, cwd=source)
            self.assertEqual(code, 0, (obj, err))
            settings = read_json(os.path.join(target, ".claude", "settings.json"))
            self.assertEqual(settings["extraKnownMarketplaces"]["general-ontology"]["source"],
                             {"source": "github", "repo": "acme/kit", "ref": "general-ontology"})
            self.assertEqual(_support.git(target, "log", "-1", "--format=%s"), "Wire the general-ontology plugin")
            self.assertNotIn(secret, _support.git(target, "log", "-p", "--all"))
            self.assertEqual(_support.git(target, "remote", "get-url", "kit"), "https://github.com/acme/kit.git")
            branch = [s for s in obj["steps"] if s["id"] == "branch"][0]
            self.assertIn("holds a credential", branch["detail"])
            self.assertNotIn(secret, json.dumps(obj))
            code, out, _err = _support.run_cli(["doctor"], repo=target)
            self.assertNotIn(secret, out)

    def credentialed_template_for(self, url):
        name = "tokened-%d" % (len(os.listdir(self.place)) + 1)
        folder = self.clone_template(name)
        _support.git(folder, "remote", "set-url", "origin", url)
        return folder

    def test_claude_gets_the_url_without_the_token(self):
        secret = token()
        source = self.credentialed_template_for("https://%s@github.com/acme/kit.git" % secret)
        bin_dir = os.path.join(self.tmp, "stub-bin")
        write_stub(bin_dir)
        log = os.path.join(self.tmp, "claude.log")
        target = os.path.join(self.place, "garden")
        with mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + SYSTEM_PATH, "CLAUDE_LOG": log}):
            code, obj, err = self.setup("--yes", "--launch", "none", "--new", target, cwd=source)
        self.assertEqual(code, 0, (obj, err))
        calls = claude_calls(log)
        self.assertEqual(calls[0][1:], ["plugin", "marketplace", "list", "--json"])  # the folder-source check
        self.assertEqual(calls[1][1:], ["plugin", "marketplace", "add",
                                        "https://github.com/acme/kit.git#general-ontology", "--scope", "project"])
        self.assertNotIn(secret, json.dumps(calls))

    def test_a_token_only_flag_url_is_refused_before_anything_is_written(self):
        secret = token()
        target = os.path.join(self.place, "garden")
        for flag in ("--kit-url", "--origin"):
            for url in ("https://%s@github.com/acme/kit.git" % secret, "https://me:%s@example.invalid/x.git" % secret,
                        "https://example.invalid/x.git?access=%s" % secret):
                code, obj, _err = self.setup("--yes", "--launch", "none", "--new", target, flag, url)
                self.assertNotEqual(code, 0, (flag, url))
                self.assertIn("holds", obj.get("message", ""))
                self.assertNotIn(secret, json.dumps(obj))
                self.assertFalse(os.path.exists(target), (flag, url))
        # a login in an ssh URL is not a secret
        code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target, "--origin",
                                    "ssh://git@example.invalid/me/garden.git")
        self.assertEqual(code, 0, (obj, err))

    def test_doctor_fails_on_a_credential_in_the_committed_settings(self):
        target = os.path.join(self.place, "garden")
        self.assertEqual(self.setup("--yes", "--launch", "none", "--new", target)[0], 0)
        path = os.path.join(target, ".claude", "settings.json")
        data = read_json(path)
        data["extraKnownMarketplaces"]["general-ontology"]["source"] = {
            "source": "git", "url": "https://%s@git.example.invalid/kit.git" % token()}
        write(path, data)
        result = doctor.run_checks(target, dict(os.environ, HOME=self.home))
        plugin = [c for c in result["checks"] if c["id"] == "plugin"][0]
        self.assertEqual(plugin["status"], "fail")
        self.assertIn("revoke", plugin["fix"])
        self.assertEqual(result["exit_code"], 1)

    def test_the_url_helpers(self):
        secret = token()
        for url in ("https://u:p@example.invalid/x", "https://%s@github.com/o/r.git" % secret,
                    "HTTPS://someone@example.invalid/x", "https://example.invalid/x?t=%s" % secret):
            self.assertTrue(doctor.url_credential(url), url)
        for url in ("https://github.com/o/r.git", "git@github.com:o/r.git", "ssh://git@example.invalid/o/r", "o/r",
                    "/srv/kit", None, ""):
            self.assertIsNone(doctor.url_credential(url), url)
        self.assertEqual(doctor.strip_credentials("https://a:b@github.com/o/r.git"), "https://github.com/o/r.git")
        self.assertEqual(doctor.strip_credentials("ssh://git:pw@example.invalid/o/r"), "ssh://git@example.invalid/o/r")
        self.assertEqual(onboard.remote_text(os.path.join(self.home, "Backups", "jane-laptop", "bk.git")),
                         "a folder named bk.git")
        self.assertEqual(onboard.remote_text("git@example.invalid:me/x.git"), "example.invalid:me/x.git")


# the answers file -----------------------------------------------------------------------------------------------
class AnswersFileTest(SetupCase):
    def test_every_value_is_type_checked_before_anything_is_written(self):
        target = os.path.join(self.place, "garden")
        bad_files = (
            {"answers": [{"q": "q.frame.you", "status": "skipped", "text": 5}]},
            {"answers": [{"q": "q.frame.you", "status": "later", "text": ["a"]}]},
            {"decisions": [{"question": "Q?", "chosen": 3}]},
            {"decisions": [{"question": "Q?", "chosen": "a", "chosen_text": 7}]},
            {"decisions": [{"question": "Q?", "chosen": "a", "rationale": {"x": 1}}]},
            {"decisions": [{"question": "Q?", "chosen": "a", "options": [1, 2]}]},
            {"decisions": [{"question": "Q?", "chosen": "a", "scope": [3]}]},
            {"setup": {"title": 5}},
            {"setup": {"new": "relative/folder"}},
            {"setup": {"plugin": "maybe"}},
            {"setup": {"when": "now"}},
        )
        for data in bad_files:
            code, obj, _err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target,
                                         "--answers", json.dumps(data))
            self.assertEqual(code, 2, (data, obj))
            self.assertIn("--answers", obj.get("message", ""), data)
            self.assertFalse(os.path.exists(target), data)

    def test_run_onto_fails_a_step_instead_of_stopping_the_run(self):
        obj = onboard.run_onto(self.tmp, ["answer", "--json", "--", "q.frame.you", 5])
        self.assertEqual(obj["exit_code"], 2)
        self.assertIn("not text", obj["message"])

    def test_the_setup_object_resumes_a_stopped_interview(self):
        target = os.path.join(self.place, "kept")
        data = {"setup": {"title": "Seed library", "new": target, "personal": "keep", "plugin": "skip"},
                "answers": [{"q": "q.frame.goal", "text": "Lend seeds and get them back each autumn."}]}
        code, obj, err = self.setup("--yes", "--launch", "none", "--answers", json.dumps(data))
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(obj["topic"], {"path": target, "name": "seed-library", "ns": "seed-library",
                                        "title": "Seed library"})
        manifest = read_json(os.path.join(target, "ontology.json"))
        self.assertEqual(set(manifest["policy"]["personal"].values()), {"keep"})
        chosen = {d["question"]: d["chosen"] for d in decisions(target)}
        self.assertEqual(chosen[onboard.Q_PERSONAL], "keep")
        self.assertEqual(chosen[onboard.Q_PLUGIN], "skip")
        # the command line wins over the file
        other = os.path.join(self.place, "other")
        code, obj, err = self.setup("--yes", "--launch", "none", "--new", other, "--title", "Tool shed",
                                    "--answers", json.dumps(data))
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual((obj["topic"]["path"], obj["topic"]["title"]), (other, "Tool shed"))

    def test_a_successful_run_moves_the_answers_file_aside(self):
        clone = self.clone_template()
        answers = write(os.path.join(clone, ".onto", "setup.json"),
                        {"answers": [{"q": "q.frame.goal", "text": "Share the tools fairly."}]})
        busy = os.path.join(self.place, "busy")
        write(os.path.join(busy, "notes.md"), "x\n")
        code, _obj, _err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", busy, "--answers",
                                      "@" + answers, cwd=clone)
        self.assertEqual(code, 1)
        self.assertTrue(os.path.isfile(answers), "a failed run keeps the file to resume from")
        target = os.path.join(self.place, "garden")
        code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target, "--answers",
                                    "@" + answers, cwd=clone)
        self.assertEqual(code, 0, (obj, err))
        self.assertFalse(os.path.exists(answers))
        self.assertTrue(os.path.isfile(os.path.join(clone, ".onto", "setup.garden.done.json")))
        self.assertIn("moved setup.json", [s for s in obj["steps"] if s["id"] == "answers"][0]["detail"])


# the goal ---------------------------------------------------------------------------------------------------------
class GoalTest(SetupCase):
    def test_the_goal_answer_becomes_a_goal_node_part_of_the_topic(self):
        target = os.path.join(self.place, "garden")
        goal = "Decide which beds to rotate each season, so the soil stays healthy and every plot gets a turn."
        code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target, "--answers",
                                    json.dumps({"answers": [{"q": "q.frame.goal", "text": goal}]}))
        self.assertEqual(code, 0, (obj, err))
        clear()
        onto = graph.Ontology.load(store.Repo.open(target))
        goals = [nid for nid in onto.local_nodes(active_only=True) if onto.kind_of(nid) == "goal"]
        self.assertEqual(len(goals), 1, goals)
        node = onto.node(goals[0])
        self.assertEqual(node["summary"], goal)
        self.assertLessEqual(len(node["name"]), onboard.GOAL_NAME_MAX)
        self.assertEqual([e["other"] for e in onto.edges_of(goals[0], "out", rels=["part_of"])], ["topic:garden"])
        self.assertEqual(_support.run_cli(["validate"], repo=target)[0], 0)
        self.assertEqual(_support.git(target, "status", "--porcelain"), "")

    def test_a_skipped_goal_answered_later_is_recorded(self):
        target = os.path.join(self.place, "garden")
        base = ("--yes", "--launch", "none", "--plugin", "skip", "--new", target, "--answers")
        skipped = json.dumps({"answers": [{"q": "q.frame.goal", "status": "skipped"}]})
        self.assertEqual(self.setup(*base, skipped)[0], 0)
        self.assertEqual(self.setup(*base, skipped)[1]["steps"][5]["status"], "already")
        answered = json.dumps({"answers": [{"q": "q.frame.goal", "text": "Decide what to plant each spring"}]})
        code, obj, err = self.setup(*base, answered)
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual([s for s in obj["steps"] if s["id"] == "answers"][0]["status"], "done")
        self.assertEqual([r["status"] for r in log_rows(target) if r["q"] == "q.frame.goal"], ["skipped", "answered"])
        # an answer is never undone by a later skip
        code, obj, err = self.setup(*base, skipped)
        self.assertEqual([s for s in obj["steps"] if s["id"] == "answers"][0]["status"], "already")
        self.assertEqual([r["status"] for r in log_rows(target) if r["q"] == "q.frame.goal"], ["skipped", "answered"])


# the plugin step and doctor follow the recorded choice -----------------------------------------------------------
class RecordedPluginTest(SetupCase):
    def make(self, mode):
        target = os.path.join(self.place, "garden-" + mode)
        code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", mode, "--new", target, *NO_ANSWERS)
        self.assertEqual(code, 0, (obj, err))
        return target

    def rerun(self, target, *extra):
        bin_dir = os.path.join(self.tmp, "stub-bin")
        write_stub(bin_dir)
        log = os.path.join(self.tmp, "claude-%s.log" % os.path.basename(target))
        with mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + SYSTEM_PATH, "CLAUDE_LOG": log}):
            code, obj, err = self.setup("--launch", "none", "--answers", json.dumps(
                {"decisions": [{"question": "Who waters the beds?", "chosen": "me"}]}), *extra, cwd=target)
        self.assertEqual(code, 0, (obj, err))
        return obj, [c for c in claude_calls(log) if c[1:3] != ["plugin", "marketplace"] or c[3] != "list"]

    def test_a_rerun_without_the_flag_keeps_the_recorded_mode(self):
        for mode in ("skip", "plugin-dir"):
            target = self.make(mode)
            obj, calls = self.rerun(target)
            self.assertEqual(calls, [], mode)
            plugin = [s for s in obj["steps"] if s["id"] == "plugin"][0]
            self.assertEqual(plugin["status"], "skipped")
            self.assertIn("recorded setup decision", plugin["detail"])
            self.assertFalse(os.path.exists(os.path.join(target, ".claude", "settings.json")))
            if mode == "plugin-dir":
                self.assertIn("--plugin-dir ./plugins/general-ontology", " ".join(obj["next"]))
            check = [c for c in doctor.run_checks(target, dict(os.environ, HOME=self.home))["checks"]
                     if c["id"] == "plugin"][0]
            self.assertEqual(check["status"], "ok", (mode, check))
            self.assertIn("recorded setup decision", check["detail"])

    def test_a_committed_topic_without_a_choice_is_left_as_it_is(self):
        target = os.path.join(self.place, "scripted")
        self.assertEqual(self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target)[0], 0)
        self.assertEqual(decisions(target), [])
        obj, calls = self.rerun(target)
        self.assertEqual(calls, [])
        plugin = [s for s in obj["steps"] if s["id"] == "plugin"][0]
        self.assertEqual(plugin["status"], "skipped")
        self.assertIn("pass --plugin project", plugin["detail"])
        check = [c for c in doctor.run_checks(target, dict(os.environ, HOME=self.home))["checks"]
                 if c["id"] == "plugin"][0]
        self.assertEqual(check["status"], "warn")
        self.assertIn("--plugin project", check["fix"])
        # asked explicitly, it wires
        obj, calls = self.rerun(target, "--plugin", "project")
        self.assertEqual([c[1:3] for c in calls], [["plugin", "marketplace"], ["plugin", "install"]])

    def test_a_fallback_to_plugin_dir_is_not_recorded_as_the_choice(self):
        target = os.path.join(self.place, "garden")
        code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", "project", "--new", target, *NO_ANSWERS,
                                    cwd=self.bare_template)
        self.assertEqual(code, 0, (obj, err))
        self.assertIn("local folder", [s for s in obj["steps"] if s["id"] == "plugin"][0]["detail"])
        self.assertNotIn(onboard.Q_PLUGIN, [d["question"] for d in decisions(target)])


# choices, commits and remotes -------------------------------------------------------------------------------------
class ChoicesAndCommitsTest(SetupCase):
    def test_a_flag_wins_over_the_files_decision_for_the_same_question(self):
        target = os.path.join(self.place, "garden")
        later = json.dumps({"decisions": [{"question": onboard.Q_PACKS, "chosen": "later"}]})
        base = ("--yes", "--launch", "none", "--plugin", "skip", "--new", target, "--answers", later)
        self.assertEqual(self.setup(*base)[0], 0)
        code, obj, err = self.setup(*base, "--packs", "assessment")
        self.assertEqual(code, 0, (obj, err))
        packs = [d for d in decisions(target) if d["question"] == onboard.Q_PACKS]
        self.assertEqual(sorted((d["chosen"], d["status"]) for d in packs),
                         [("assessment", "active"), ("later", "superseded")])
        self.assertIn("assessment", read_json(os.path.join(target, "ontology.json"))["packs"])

    def test_later_commits_never_sweep_in_the_users_files(self):
        target = os.path.join(self.place, "garden")
        self.assertEqual(self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target)[0], 0)
        write(os.path.join(target, "my-private-notes.txt"), "private draft\n")
        head = _support.git(target, "rev-parse", "HEAD")
        answers = json.dumps({"answers": [{"q": "q.frame.you", "text": "I am a bed steward"}]})
        code, obj, err = self.setup("--launch", "none", "--plugin", "skip", "--answers", answers, cwd=target)
        self.assertEqual(code, 0, (obj, err))
        commit = [s for s in obj["steps"] if s["id"] == "commit"][0]
        self.assertEqual(commit["status"], "skipped")
        self.assertIn("my-private-notes.txt", commit["detail"])
        self.assertEqual(_support.git(target, "rev-parse", "HEAD"), head)
        # with only setup's own writes uncommitted, it commits them, and only them
        os.unlink(os.path.join(target, "my-private-notes.txt"))
        _support.commit_all(target, "the answer")
        write(os.path.join(target, "inbox", "raw.txt"), "raw\n")  # ignored, never committed
        answers = json.dumps({"answers": [{"q": "q.frame.goal", "text": "Grow enough to share."}]})
        code, obj, err = self.setup("--launch", "none", "--plugin", "skip", "--answers", answers, cwd=target)
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(_support.git(target, "log", "-1", "--format=%s"), "Record the setup of garden")
        self.assertEqual(_support.git(target, "status", "--porcelain"), "")

    def test_a_local_origin_is_recorded_by_its_last_folder_only(self):
        target = os.path.join(self.place, "garden")
        origin = os.path.join(self.home, "Backups", "jane-doe-laptop", "garden.git")
        code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target, "--origin",
                                    origin, *NO_ANSWERS)
        self.assertEqual(code, 0, (obj, err))
        remote = [d for d in decisions(target) if d["question"] == onboard.Q_REMOTE][0]
        self.assertEqual(remote["chosen_text"], "a folder named garden.git")
        self.assertNotIn("jane-doe", json.dumps(decisions(target)))

    def test_the_topic_gets_no_tags_from_the_checkout(self):
        source = self.clone_template("tagged")
        _support.git(source, "tag", "-a", "v4", "-m", "v4: another project's release")
        target = os.path.join(self.place, "garden")
        code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target, cwd=source)
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(_support.git(target, "tag", "-l"), "")

    def test_a_title_with_a_control_character_is_refused_before_the_clone(self):
        target = os.path.join(self.place, "garden")
        code, obj, _err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target, "--title",
                                     "Garden\x1b[31mRED\x07")
        self.assertEqual(code, 2, obj)
        self.assertIn("control character", obj["message"])
        self.assertFalse(os.path.exists(target))


# the clone source and the target ----------------------------------------------------------------------------------
class SourceAndTargetTest(SetupCase):
    def test_git_missing_is_named(self):
        empty = os.path.join(self.tmp, "no-git-bin")
        os.makedirs(empty)
        with mock.patch.dict(os.environ, {"PATH": empty}):
            code, obj, _err = self.setup("--yes", "--launch", "none", "--new", os.path.join(self.place, "g"))
        self.assertEqual(code, 2, obj)
        self.assertIn("git is not on PATH", obj["message"])

    def test_an_older_kit_on_the_template_branch_is_refused(self):
        source = self.clone_template("old-kit")
        init = os.path.join(source, "plugins", "general-ontology", "ontokit", "__init__.py")
        with open(init, encoding="utf-8") as fh:
            text = fh.read()
        write(init, doctor.VERSION_RE.sub('__version__ = "0.1.0"', text))
        _support.commit_all(source, "an old kit")
        target = os.path.join(self.place, "garden")
        code, obj, _err = self.setup("--yes", "--launch", "none", "--new", target, cwd=source)
        self.assertEqual(code, 2, obj)
        self.assertIn("holds kit 0.1.0, older than this kit", obj["message"])
        self.assertFalse(os.path.exists(target))

    def test_never_from_inside_a_topic(self):
        """A topic's own new-topic (its vendored kit) never clones the topic's frozen general-ontology branch."""
        topic = os.path.join(self.place, "garden")
        self.assertEqual(self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", topic)[0], 0)
        vendored = os.path.join(topic, "plugins", "general-ontology", "ontokit")
        with mock.patch.object(onboard.handoff, "RUNNING_KIT", vendored):
            code, obj, _err = self.setup("--yes", "--launch", "none", "--new", os.path.join(self.place, "g2"),
                                         cwd=topic)
        self.assertEqual(code, 2, obj)
        self.assertIn("not from inside a topic", obj["message"])

    def test_the_target_is_never_the_checkout_or_inside_it(self):
        source = self.clone_template("kit")
        remote = _support.git(source, "remote", "get-url", "origin")
        for new in (".", "garden", os.path.join(source, "sub", "garden")):
            code, obj, _err = self.setup("--yes", "--launch", "none", "--kit-url", "acme/go", "--new", new, cwd=source)
            self.assertEqual(code, 2, (new, obj))
            self.assertIn("template checkout", obj["message"])
        self.assertEqual(_support.git(source, "symbolic-ref", "--short", "HEAD"), "general-ontology")
        self.assertEqual(_support.git(source, "remote", "get-url", "origin"), remote)
        self.assertFalse(os.path.exists(os.path.join(source, "ontology.json")))

    def test_new_topic_reads_a_relative_folder_from_where_it_was_run(self):
        caller = os.path.join(self.place, "p sh")
        os.makedirs(caller)
        ran = 0
        for shell in ("sh", "bash", "zsh"):
            exe = shutil.which(shell, path=SYSTEM_PATH)
            if not exe:
                continue
            ran += 1
            name = "garden-%s" % shell
            proc = subprocess.run([exe, os.path.join(self.template_root, "new-topic"), "--yes", "--launch", "none",
                                   "--plugin", "skip", "--new", name], cwd=caller, env=dict(self.env),
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=300)
            self.assertEqual(proc.returncode, 0, (shell, proc.stderr.decode("utf-8", "replace")))
            self.assertTrue(os.path.isfile(os.path.join(caller, name, "ontology.json")), shell)
            self.assertFalse(os.path.exists(os.path.join(self.template_root, name)), shell)
        self.assertGreater(ran, 0)
        self.assertEqual(_support.git(self.template_root, "status", "--porcelain"), "")


# doctor -----------------------------------------------------------------------------------------------------------
class DoctorBrokenTopicTest(SetupCase):
    def test_a_conflicted_ontology_json_is_a_failing_topic(self):
        target = os.path.join(self.place, "garden")
        self.assertEqual(self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target)[0], 0)
        path = os.path.join(target, "ontology.json")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        write(path, "<<<<<<< HEAD\n%s=======\n%s>>>>>>> other\n" % (text, text))
        result = doctor.run_checks(target, dict(os.environ, HOME=self.home))
        self.assertEqual(result["where"], "topic")
        self.assertEqual(result["exit_code"], 1)
        where = [c for c in result["checks"] if c["id"] == "where"][0]
        self.assertEqual(where["status"], "fail")
        self.assertIn("merge conflict", where["detail"])
        code, obj, _err = self.setup("--launch", "none", "--plugin", "skip", cwd=target)
        self.assertEqual(code, 2, obj)
        self.assertIn("cannot be read", obj["message"])

    def test_conflict_copies_in_both_places_are_both_named(self):
        target = os.path.join(self.place, "garden")
        self.assertEqual(self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target)[0], 0)
        shutil.copy(os.path.join(target, ".git", "HEAD"), os.path.join(target, ".git", "refs", "heads", "main 2"))
        shutil.copy(os.path.join(target, "graph", "nodes.jsonl"), os.path.join(target, "graph", "nodes 2.jsonl"))
        result = doctor.run_checks(target, dict(os.environ, HOME=self.home))
        copies = [c for c in result["checks"] if c["id"] == "conflict_copies"][0]
        self.assertEqual(copies["status"], "fail")
        self.assertIn("graph/nodes 2.jsonl", copies["detail"])
        self.assertIn("keep one", copies["fix"])
        os.unlink(os.path.join(target, ".git", "refs", "heads", "main 2"))
        code, out, _err = _support.run_cli(["validate"], repo=target)
        self.assertIn("graph/nodes 2.jsonl", out)
        self.assertIn("compare it with its original", out)
        self.assertNotIn("delete it;", out)
        self.assertNotIn("a junk file; delete it", out)


# Windows ----------------------------------------------------------------------------------------------------------
class WindowsLaunchTest(SetupCase):
    def test_claude_runs_as_a_child_and_its_exit_code_is_returned(self):
        bin_dir = os.path.join(self.tmp, "stub-bin")
        stub = write_stub(bin_dir)
        onboard._interactive = lambda: True
        self.addCleanup(setattr, onboard, "_windows", onboard._windows)
        self.addCleanup(setattr, onboard, "_call", onboard._call)
        onboard._windows = lambda: True
        calls = []
        onboard._call = lambda argv, cwd=None: (calls.append((argv, cwd, os.environ.get(onboard.CALLER_CWD))), 7)[1]
        onboard._execvp = lambda *a: self.fail("exec on Windows")
        target = os.path.join(self.place, "garden")
        old = os.getcwd()
        os.chdir(self.template_root)
        self.addCleanup(os.chdir, old)
        with mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + SYSTEM_PATH, onboard.CALLER_CWD: self.tmp,
                                          "CLAUDE_LOG": os.path.join(self.tmp, "log")}):
            code, _out, err = _support.run_cli(["setup", "--yes", "--plugin", "plugin-dir", "--new", target])
        self.assertEqual(code, 7, err)
        self.assertEqual(calls, [([stub, "--plugin-dir", "./plugins/general-ontology", "Start the ontology"], target,
                                  None)])


# the ledger ---------------------------------------------------------------------------------------------------------
class NarrowingChainTest(_support.TempCase):
    def test_a_replacement_cannot_narrow_its_own_narrower(self):
        root = _support.init_topic(self.tmp, "garden")

        def decide(*args):
            code, out, err = _support.run_cli(["decide", "--json"] + list(args), repo=root)
            return code, (json.loads(out) if out.strip() else {}), err

        code, a, err = decide("--question", "Which beds?", "--chosen", "south")
        self.assertEqual(code, 0, err)
        a = a["decision"]["id"]
        b = decide("--question", "Which south beds?", "--chosen", "south-1", "--narrows", a)[1]["decision"]["id"]
        c = decide("--question", "Which south-1 rows?", "--chosen", "row 1", "--narrows", b)[1]["decision"]["id"]
        code, obj, _err = decide("--question", "Which south beds?", "--chosen", "south-2", "--supersedes", b,
                                 "--narrows", c)
        self.assertNotEqual(code, 0, obj)
        self.assertIn("narrow %s" % a, obj.get("message", ""))
        # the way it suggests works, and W10's advice then settles
        code, obj, err = decide("--question", "Which south beds?", "--chosen", "south-2", "--supersedes", b,
                                "--narrows", a)
        self.assertEqual(code, 0, err)
        d = obj["decision"]["id"]
        code, obj, err = decide("--question", "Which south-1 rows?", "--chosen", "row 2", "--supersedes", c,
                                "--narrows", d)
        self.assertEqual(code, 0, err)
        code, out, _err = _support.run_cli(["validate"], repo=root)
        self.assertNotIn("W10", out)


# the interview ------------------------------------------------------------------------------------------------------
class InterviewFixesTest(_support.TempCase):
    def setUp(self):
        super().setUp()
        clear()
        parent = os.path.join(self.tmp, SPECIAL)
        os.makedirs(parent)
        self.root = _support.init_topic(parent, "clinic", "Patients' experiences of a clinic waiting room today")
        self.repo = store.Repo.open(self.root)

    def cli(self, *args):
        clear()
        code, out, err = _support.run_cli(list(args), repo=self.root)
        clear()
        return code, out, err

    def test_the_ask_is_never_cut(self):
        code, out, _err = self.cli("next", "--n", "5")
        self.assertEqual(code, 0)
        asks = {q["id"]: q["ask"] for q in json.loads(self.cli("--json", "next", "--n", "5")[1])["questions"]}
        long = [a for a in asks.values() if len(a) > 100]
        self.assertTrue(long, asks)
        for ask in long:
            self.assertIn(json.dumps(ask, ensure_ascii=False), out)

    def test_the_constraints_risk_question_closes_once_the_risk_question_is_answered(self):
        self.assertEqual(self.cli("pack", "add", "assessment")[0], 0)
        listed = [q["id"] for q in json.loads(self.cli("--json", "next", "--stage", "5", "--n", "20")[1])["questions"]]
        self.assertIn("q.constraints.risks", listed)
        text = "A late frost could kill the seedlings."
        code, out, err = self.cli("answer", "q.risks.what", text, "--apply", "--ops", json.dumps([
            {"op": "add_node", "ref": "$frost", "basis": "stated", "prov": [{"quote": text}],
             "node": {"kind": "risk", "name": "Late frost", "attrs": {"statement": text}}}]))
        self.assertEqual(code, 0, out + err)
        listed = [q["id"] for q in json.loads(self.cli("--json", "next", "--stage", "5", "--n", "20")[1])["questions"]]
        self.assertNotIn("q.constraints.risks", listed)

    def risk_topic(self):
        self.assertEqual(self.cli("pack", "add", "assessment")[0], 0)
        text = "The waiting room is too loud, and that could breach the privacy rules."
        ops = [{"op": "add_node", "ref": "$noise", "basis": "stated", "prov": [{"quote": text}],
                "node": {"id": "risk:noise", "kind": "risk", "name": "Loud waiting room", "attrs": {
                    "statement": text, "status": "draft",
                    "ratings": ["regulatory.impact=high", "regulatory.inherent=high", "regulatory.residual=moderate"]}}}]
        code, out, err = self.cli("answer", "q.risks.what", text, "--apply", "--ops", json.dumps(ops))
        self.assertEqual(code, 0, out + err)
        return out

    def test_a_new_draft_calibration_warning_is_named_in_the_answer(self):
        out = self.risk_topic()
        self.assertIn("W09", out)
        self.assertIn("no implemented or partial control mitigates it", out)

    def test_a_p23_shows_in_the_preview_and_a_refused_answer_leaves_nothing(self):
        self.risk_topic()
        text = "The clinic manager owns it, and she has approved these ratings."
        ops = json.dumps([{"op": "update_node", "id": "risk:noise", "basis": "stated", "prov": [{"quote": text}],
                           "reason": "the clinic manager owns the risk and approved it",
                           "set": {"attrs.owner": "Clinic manager", "attrs.status": "approved"}}])
        before = _support.snapshot(self.root, skip=(".onto",))
        code, out, err = self.cli("--json", "answer", "q.risks.owner", text, "--apply", "--ops", ops)
        obj = json.loads(out)
        self.assertEqual(code, 1, obj)
        self.assertEqual(obj.get("error"), "refused")
        self.assertIn("P23", json.dumps(obj["problems"]))
        self.assertIsNotNone(obj.get("preview"))
        self.assertEqual(_support.snapshot(self.root, skip=(".onto",)), before)
        # confirmed anyway: refused, and nothing is kept (no log line, no pending proposal, no source)
        code, out, err = self.cli("--json", "answer", "q.risks.owner", text, "--apply", "--confirm", "--ops", ops)
        obj = json.loads(out)
        self.assertEqual(code, 1, obj)
        self.assertIn("nothing was written", obj["message"])
        self.assertIn("P23", json.dumps(obj["problems"]))
        clear()
        self.assertEqual(_support.snapshot(self.root, skip=(".onto",)), before)
        self.assertEqual(pipeline.pending(store.Repo.open(self.root)), [])
        self.assertNotIn("q.risks.owner", [r["q"] for r in log_rows(self.root)])
        listed = [q["id"] for q in json.loads(self.cli("--json", "next", "--n", "40")[1])["questions"]]
        self.assertIn("q.risks.owner", listed)
        # with the risk kept draft the same owner answer applies
        draft = json.dumps([{"op": "update_node", "id": "risk:noise", "basis": "stated",
                             "prov": [{"quote": "The clinic manager owns it"}],
                             "reason": "the clinic manager owns the risk",
                             "set": {"attrs.owner": "Clinic manager"}}])
        code, out, err = self.cli("answer", "q.risks.owner", text, "--apply", "--confirm", "--ops", draft)
        self.assertEqual(code, 0, out + err)

    def test_an_apply_preview_that_would_be_refused_offers_no_confirm(self):
        self.assertEqual(self.cli("pack", "add", "assessment")[0], 0)
        text = "The waiting room is too loud; the clinic manager approved these ratings."
        ops = [{"op": "add_node", "ref": "$noise", "basis": "stated", "prov": [{"quote": text}],
                "node": {"id": "risk:noise", "kind": "risk", "name": "Loud waiting room", "attrs": {
                    "statement": text, "status": "approved", "owner": "Clinic manager",
                    "ratings": ["regulatory.impact=high", "regulatory.inherent=high", "regulatory.residual=moderate"]}}}]
        code, out, err = self.cli("--json", "answer", "q.risks.what", text, "--ops", json.dumps(ops))
        self.assertEqual(code, 0, out + err)
        pid = json.loads(out)["proposal"]["id"]
        clear()
        server = mcp_server.Server("full", repo=self.root, cwd=self.tmp, env={}, err=io.StringIO())
        out = _support.mcp_call(server, "onto_apply", id=pid, accept="1")["content"][0]["text"]
        self.assertIn("refused if confirmed", out)
        self.assertIn("P23", out)
        nxt = out.split("Next:", 1)[1]
        self.assertNotIn("confirm=true", nxt)
        self.assertNotIn("--confirm", nxt)
        self.assertIn("do not confirm", nxt)

    def test_a_refused_apply_preview_for_a_missing_measure_offers_rating_it(self):
        self.assertEqual(self.cli("pack", "add", "assessment")[0], 0)
        text = "Flooding could hit the store room; the clinic manager approved this rating."
        ops = [{"op": "add_node", "ref": "$flood", "basis": "stated", "prov": [{"quote": text}],
                "node": {"id": "risk:flood", "kind": "risk", "name": "Flooding", "attrs": {
                    "statement": text, "status": "approved", "owner": "Clinic manager",
                    "ratings": ["financial.impact=high"]}}}]
        code, out, err = self.cli("--json", "answer", "q.risks.what", text, "--ops", json.dumps(ops))
        self.assertEqual(code, 0, out + err)
        pid = json.loads(out)["proposal"]["id"]
        clear()
        server = mcp_server.Server("full", repo=self.root, cwd=self.tmp, env={}, err=io.StringIO())
        out = _support.mcp_call(server, "onto_apply", id=pid, accept="1")["content"][0]["text"]
        self.assertIn("refused if confirmed", out)
        self.assertIn("inherent and residual are not rated", out)
        nxt = out.split("Next:", 1)[1]
        self.assertIn("do not confirm", nxt)
        self.assertIn("rate the missing measures", nxt)
        self.assertNotIn("confirm=true", nxt)

    def test_agents_md_offers_rating_the_missing_measures_as_a_way_out(self):
        # H5: the risks and controls text moved to docs/risks-and-controls.md, which AGENTS.md links
        with open(os.path.join(os.path.dirname(os.path.dirname(_support.PLUGIN_DIR)), "AGENTS.md"), encoding="utf-8") as fh:
            agents = " ".join(fh.read().split())
        self.assertIn("plugins/general-ontology/docs/risks-and-controls.md", agents)
        with open(os.path.join(_support.PLUGIN_DIR, "docs", "risks-and-controls.md"), encoding="utf-8") as fh:
            text = " ".join(fh.read().split())
        self.assertIn("raise the residual, or rate the missing measures", text)
        for doc in (agents, text):
            self.assertNotIn("three ways out", doc)

    def test_neighbors_with_rests_on_lists_only_what_rests_on_a_premise(self):
        text = "The plan rests on the well keeping water, which also supports the fire rule."
        ops = [
            {"op": "add_node", "ref": "$well", "basis": "stated", "prov": [{"quote": "the well keeping water"}],
             "node": {"id": "premise:well", "kind": "premise", "name": "The well keeps water"}},
            {"op": "add_node", "ref": "$plan", "basis": "stated", "prov": [{"quote": "The plan"}],
             "node": {"id": "goal:plan", "kind": "goal", "name": "The plan"}},
            {"op": "add_node", "ref": "$fire", "basis": "stated", "prov": [{"quote": "the fire rule"}],
             "node": {"id": "constraint:fire", "kind": "constraint", "name": "The fire rule"}},
            {"op": "add_edge", "basis": "stated", "prov": [{"quote": "rests on the well"}],
             "edge": {"src": "$plan", "rel": "rests_on", "dst": "$well"}},
            {"op": "add_edge", "basis": "stated", "prov": [{"quote": "also supports the fire rule"}],
             "edge": {"src": "$well", "rel": "supports", "dst": "$fire"}}]
        result = interview.answer(self.repo, "q.constraints.premises", text, ops=ops, apply=True)
        self.assertTrue(result.get("applied"), result)
        code, out, err = self.cli("--json", "neighbors", "premise:well", "--rels", "rests_on")
        self.assertEqual(code, 0, out + err)
        self.assertEqual([i["id"] for i in json.loads(out)["items"]], ["goal:plan"])
        # without --rels the two labels tell the links apart: the inverse of rests_on is "underpins"
        code, out, err = self.cli("--json", "neighbors", "premise:well")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(sorted((i["id"], i["edge"]) for i in json.loads(out)["items"]),
                         [("constraint:fire", "supports"), ("goal:plan", "underpins")])
        for path in (os.path.join(os.path.dirname(_support.PLUGIN_DIR), "..", "AGENTS.md"),
                     os.path.join(_support.PLUGIN_DIR, "skills", "onto-review", "SKILL.md")):
            with open(path, encoding="utf-8") as fh:
                self.assertIn("--rels rests_on", fh.read(), path)


class RefusedAnswerUnitTest(unittest.TestCase):
    def test_refused_is_raised_with_dict_problems(self):
        # cmd_answer lists the problems of a refused answer one by one only when they are dicts with a code
        exc = Refused("x", problems=[{"code": "P23", "message": "m"}])
        self.assertTrue(interview.about_ops(exc))


if __name__ == "__main__":
    unittest.main()
