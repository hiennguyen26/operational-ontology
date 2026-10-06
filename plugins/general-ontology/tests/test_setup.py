"""``onto setup`` (SPEC A1, B3) and the launchers (A3): a template checkout becomes a ready topic in one call.

Every test works on a scratch template (a git repo on the branch ``general-ontology`` holding a copy of this kit)
under a folder whose name has a space and a curly apostrophe, with a temp ``HOME``, a ``PATH`` without the real
``claude`` (a stub stands in when a test needs one) and git configured only through the environment (a local
identity, no signing or hooks, ``gc.auto=0`` and ``maintenance.auto=false``).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests import _support
from ontokit import cli, doctor, onboard, store

REPO_ROOT = os.path.dirname(os.path.dirname(_support.PLUGIN_DIR))
LAUNCHERS = ("new-topic", "New topic.command", "new-topic.cmd")
KIT_URL = "https://github.com/example-owner/garden-kit.git"
NO_ANSWERS = ("--answers", "{}")  # an (empty) answers file: the agent's interview passed these flags
SPECIAL = "My topic\u2019s folder"  # a space and a curly apostrophe, as real paths have
GIT_CONFIG = (("gc.auto", "0"), ("maintenance.auto", "false"), ("user.name", "Onto Test"),
              ("user.email", "test@example.invalid"), ("commit.gpgsign", "false"), ("tag.gpgsign", "false"),
              ("core.hooksPath", os.devnull), ("init.defaultBranch", "main"))
SYSTEM_PATH = os.pathsep.join(p for p in ("/usr/bin", "/bin", "/usr/sbin", "/sbin") if os.path.isdir(p))
STUB_CLAUDE = """#!/bin/sh
printf '%s|' "$PWD" "$@" >> "$CLAUDE_LOG"
printf '\\n' >> "$CLAUDE_LOG"
if [ "$1 $2 $3" = "plugin marketplace list" ]; then printf '[]\\n'; fi
exit 0
"""


def git_env(home, path=SYSTEM_PATH, extra=None):
    """The environment every setup test runs under: temp HOME, a PATH without the real claude, git config by env."""
    env = dict(_support.GIT_ENV)
    env.update({"HOME": home, "PATH": path, "GIT_CONFIG_COUNT": str(len(GIT_CONFIG))})
    for i, (key, value) in enumerate(GIT_CONFIG):
        env["GIT_CONFIG_KEY_%d" % i] = key
        env["GIT_CONFIG_VALUE_%d" % i] = value
    env.update(extra or {})
    return env


def make_template(folder, origin=KIT_URL):
    """A scratch template checkout at ``folder`` on the branch general-ontology: this kit, the root dotfiles and
    the launchers, committed; ``origin`` names the kit URL (None for a template with no remote)."""
    kit = os.path.join(folder, "plugins", "general-ontology")
    os.makedirs(kit)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for part in ("ontokit", "bin"):
        shutil.copytree(os.path.join(_support.PLUGIN_DIR, part), os.path.join(kit, part), ignore=ignore)
    for name in (".gitignore", ".gitattributes", "README.md") + LAUNCHERS:
        shutil.copy2(os.path.join(REPO_ROOT, name), os.path.join(folder, name))
    _support.git(folder, "init", "-q")
    _support.git(folder, "symbolic-ref", "HEAD", "refs/heads/general-ontology")
    for key, value in GIT_CONFIG:
        _support.git(folder, "config", key, value)
    _support.commit_all(folder, "kit")
    if origin:
        _support.git(folder, "remote", "add", "origin", origin)
    return folder


def write_stub(folder):
    """A stub ``claude`` in ``folder`` that logs its working folder and arguments to ``$CLAUDE_LOG``."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "claude")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(STUB_CLAUDE)
    os.chmod(path, 0o755)
    return path


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def decisions(root):
    folder = os.path.join(root, "ledger", "decisions")
    return [read_json(os.path.join(folder, n)) for n in sorted(os.listdir(folder)) if n.endswith(".json")]


class SetupCase(_support.TempCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.base = os.path.realpath(tempfile.mkdtemp(prefix="onto-setup-"))
        cls.template_root = make_template(os.path.join(cls.base, SPECIAL, "template"))
        cls.bare_template = make_template(os.path.join(cls.base, SPECIAL, "no-remote"), origin=None)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, True)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.place = os.path.join(self.tmp, SPECIAL)
        os.makedirs(self.place)
        self.env = git_env(self.home)
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("_interactive", "_execvp"):
            original = getattr(onboard, name)
            self.addCleanup(setattr, onboard, name, original)
        onboard._interactive = lambda: False

    def clone_template(self, name="clone", source=None):
        """A clone of the scratch template still on the template branch (README step 1 not done)."""
        target = os.path.join(self.place, name)
        _support.git(self.place, "clone", "-q", "--single-branch", "-b", "general-ontology",
                     source or self.template_root, target)
        _support.git(target, "remote", "set-url", "origin", KIT_URL)
        _support.git(target, "update-ref", "refs/remotes/origin/general-ontology", "HEAD")
        return target

    def setup(self, *args, cwd=None):
        """``onto setup <args> --json`` run in process from ``cwd`` (the template by default)."""
        old = os.getcwd()
        os.chdir(cwd or self.template_root)
        try:
            code, out, err = _support.run_cli(["setup", "--json"] + list(args))
        finally:
            os.chdir(old)
        obj = json.loads(out) if out.strip() else {}
        return code, obj, err

    def steps(self, obj):
        return {s["id"]: s["status"] for s in obj.get("steps") or []}


class NewFolderTest(SetupCase):
    def test_new_makes_a_committed_topic_with_its_kit_remote(self):
        target = os.path.join(self.place, "garden")
        code, obj, err = self.setup("--yes", "--plugin", "skip", "--launch", "none", "--new", target, *NO_ANSWERS)
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(self.steps(obj), {"preflight": "done", "clone": "done", "branch": "done", "init": "done",
                                           "packs": "skipped", "answers": "done", "commit": "done",
                                           "plugin": "skipped", "agents": "skipped", "launch": "skipped"})
        self.assertEqual(obj["topic"], {"path": target, "name": "garden", "ns": "garden", "title": "Garden"})
        manifest = read_json(os.path.join(target, "ontology.json"))
        self.assertEqual((manifest["ns"], manifest["name"]), ("garden", "garden"))
        self.assertEqual(_support.git(target, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(_support.git(target, "remote").split(), ["kit"])
        self.assertEqual(_support.git(target, "remote", "get-url", "kit"), KIT_URL)
        self.assertEqual(_support.git(target, "log", "-1", "--format=%s"), "Start garden")
        self.assertEqual(_support.git(target, "status", "--porcelain"), "")
        # the topic carries its own kit and validates with it
        self.assertTrue(os.path.isfile(os.path.join(target, "plugins", "general-ontology", "bin", "onto")))
        code, _out, err = _support.run_cli(["validate"], repo=target)
        self.assertEqual(code, 0, err)
        # --new and --plugin came with the answers file, so they are decisions; --yes defaults are not
        recorded = {d["question"]: d for d in decisions(target)}
        self.assertEqual(sorted(recorded), sorted([onboard.Q_LOCATION, onboard.Q_PLUGIN]))
        self.assertEqual(recorded[onboard.Q_LOCATION]["chosen"], "elsewhere")
        # the full path never reaches the ledger: folder names above the topic can name a person or a machine
        self.assertEqual(recorded[onboard.Q_LOCATION]["chosen_text"], "a folder named garden")
        self.assertNotIn(SPECIAL, json.dumps(decisions(target), ensure_ascii=False))
        self.assertEqual(recorded[onboard.Q_PLUGIN]["chosen"], "skip")
        self.assertTrue(all(d["decided_by"] == "user" and d["scope"] == ["setup"] for d in recorded.values()))
        self.assertIn("Codex or another agent: open the folder and ask the agent to follow AGENTS.md", obj["next"])

    def test_scripted_flags_without_an_answers_file_record_no_decisions(self):
        target = os.path.join(self.place, "garden")
        code, obj, err = self.setup("--yes", "--plugin", "skip", "--launch", "none", "--new", target, "--personal",
                                    "keep", "--packs", "core", "--origin", "git@example.invalid:me/garden.git")
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(self.steps(obj)["answers"], "skipped")
        self.assertEqual(decisions(target), [])

    def test_a_second_run_reports_already(self):
        target = os.path.join(self.place, "garden")
        args = ("--yes", "--plugin", "skip", "--launch", "none", "--new", target) + NO_ANSWERS
        self.assertEqual(self.setup(*args)[0], 0)
        head = _support.git(target, "rev-parse", "HEAD")
        code, obj, err = self.setup(*args)
        self.assertEqual(code, 0, (obj, err))
        steps = self.steps(obj)
        for sid in ("clone", "branch", "init", "answers", "commit"):
            self.assertEqual(steps[sid], "already", (sid, obj))
        self.assertEqual(_support.git(target, "rev-parse", "HEAD"), head)
        self.assertEqual(len(decisions(target)), 2)

    def test_yes_alone_picks_the_home_folder_and_records_no_defaults(self):
        code, obj, err = self.setup("--yes", "--launch", "none", "--title", "Community garden plots")
        self.assertEqual(code, 0, (obj, err))
        target = os.path.join(self.home, "Ontologies", "community-garden-plots")
        self.assertEqual(obj["topic"]["path"], target)
        self.assertEqual(obj["topic"]["ns"], "community-garden-plots")
        self.assertTrue(os.path.isfile(os.path.join(target, "ontology.json")))
        self.assertEqual(self.steps(obj)["answers"], "skipped")
        self.assertEqual(decisions(target), [])
        # project wiring without claude on PATH: settings.json written and committed
        settings = read_json(os.path.join(target, ".claude", "settings.json"))
        self.assertEqual(settings, {"extraKnownMarketplaces": {"general-ontology": {"source": {
            "source": "github", "repo": "example-owner/garden-kit", "ref": "general-ontology"}}},
            "enabledPlugins": {"general-ontology@general-ontology": True}})
        self.assertEqual(_support.git(target, "log", "-1", "--format=%s"), "Wire the general-ontology plugin")
        self.assertEqual(_support.git(target, "status", "--porcelain"), "")

    def test_unknown_values_are_refused_before_anything_is_written(self):
        target = os.path.join(self.place, "garden")
        for args, needle in ((("--packs", "nosuch"), "unknown pack 'nosuch'"),
                             (("--ns", "self"), "--ns must be"),
                             (("--answers", '{"answers": [{"q": "q.frame.goal"}]}'), "needs text"),
                             (("--answers", '{"extra": 1}'), "unknown key"),
                             (("--origin", "https://me:hunter2@example.invalid/x.git"), "holds a password"),
                             (("--here",), "not both")):
            code, obj, _err = self.setup("--yes", "--launch", "none", "--new", target, *args)
            self.assertNotEqual(code, 0, args)
            self.assertIn(needle, obj.get("message", ""), args)
            self.assertFalse(os.path.exists(target), args)

    def test_a_folder_that_is_not_empty_stops_the_preflight(self):
        target = os.path.join(self.place, "busy")
        os.makedirs(target)
        with open(os.path.join(target, "notes.md"), "w") as fh:
            fh.write("x\n")
        code, obj, _err = self.setup("--yes", "--launch", "none", "--new", target)
        self.assertEqual(code, 1)
        self.assertEqual(self.steps(obj)["preflight"], "failed")
        self.assertEqual(self.steps(obj)["clone"], "skipped")
        self.assertEqual(os.listdir(target), ["notes.md"])

    def test_a_cloud_synced_folder_needs_confirming(self):
        os.makedirs(os.path.join(self.home, "Library", "Mobile Documents", "com~apple~CloudDocs", "Desktop"))
        target = os.path.join(self.home, "Desktop", "garden")
        code, obj, _err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target)
        self.assertEqual(code, 1)
        self.assertIn("cloud-synced", obj["steps"][0]["detail"])
        self.assertFalse(os.path.exists(target))
        code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--cloud-ok", "--new", target)
        self.assertEqual(code, 0, (obj, err))
        self.assertIn("accepted", obj["steps"][0]["detail"])

    def test_personal_and_packs_reach_the_manifest(self):
        target = os.path.join(self.place, "garden")
        code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target,
                                    "--personal", "refuse", "--packs", "core", *NO_ANSWERS)
        self.assertEqual(code, 0, (obj, err))
        manifest = read_json(os.path.join(target, "ontology.json"))
        self.assertEqual(set(manifest["policy"]["personal"].values()), {"refuse"})
        self.assertEqual(manifest["packs"], ["core", "discovery", "local"])
        self.assertEqual(self.steps(obj)["packs"], "already")
        questions = {d["question"]: d["chosen"] for d in decisions(target)}
        self.assertEqual(questions[onboard.Q_PERSONAL], "refuse")
        self.assertEqual(questions[onboard.Q_PACKS], "core")

    def test_packs_assessment_goes_through_pack_add_and_the_change_log(self):
        target = os.path.join(self.place, "garden")
        code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target,
                                    "--packs", "assessment")
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(self.steps(obj)["packs"], "done")
        manifest = read_json(os.path.join(target, "ontology.json"))
        self.assertEqual(manifest["packs"], ["core", "discovery", "assessment", "local"])
        rows, bad = store.read_jsonl(os.path.join(target, "ledger", "changes.jsonl"))
        self.assertEqual(bad, [])
        pack_changes = [r for r in rows if r.get("type") == "pack"]
        self.assertEqual([(r["summary"], r["by"]) for r in pack_changes], [("add pack assessment", "user")])
        history, _bad = store.read_jsonl(os.path.join(target, "metrics", "history.jsonl"))
        self.assertIn("pack", [h.get("kind") for h in history])
        code, out, err = _support.run_cli(["validate"], repo=target)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(_support.git(target, "status", "--porcelain").strip(), "")  # the commit holds the change
        code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target,
                                    "--packs", "assessment")
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(self.steps(obj)["packs"], "already")
        rows, _bad = store.read_jsonl(os.path.join(target, "ledger", "changes.jsonl"))
        self.assertEqual(len([r for r in rows if r.get("type") == "pack"]), 1)


class AnswersTest(SetupCase):
    def test_answers_and_decisions_are_replayed_once(self):
        answers = os.path.join(self.tmp, "setup.json")
        with open(answers, "w", encoding="utf-8") as fh:
            json.dump({"answers": [{"q": "q.frame.goal", "text": "Plan next season's beds and share the tools."}],
                       "decisions": [{"question": "Who reviews the drafts?", "options": ["me=Me", "club=The club"],
                                      "chosen": "club", "scope": ["setup"]}]}, fh)
        target = os.path.join(self.place, "garden")
        args = ("--yes", "--launch", "none", "--plugin", "skip", "--new", target, "--answers", "@" + answers)
        code, obj, err = self.setup(*args)
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(self.steps(obj)["answers"], "done")
        rows, _problems = store.read_jsonl(os.path.join(target, "interview", "log.jsonl"))
        goal = [r for r in rows if r.get("q") == "q.frame.goal"]
        self.assertEqual(len(goal), 1)
        self.assertEqual(goal[0]["status"], "answered")
        source = goal[0].get("src") or goal[0].get("source")
        with open(os.path.join(target, "sources", "%s.txt" % source), encoding="utf-8") as fh:
            self.assertIn("Plan next season's beds and share the tools.", fh.read())
        recorded = {d["question"]: d for d in decisions(target)}
        self.assertEqual(recorded["Who reviews the drafts?"]["chosen"], "club")
        self.assertEqual(recorded["Who reviews the drafts?"]["decided_by"], "user")
        self.assertIn(onboard.Q_LOCATION, recorded)
        self.assertEqual(_support.git(target, "status", "--porcelain"), "")
        code, obj, err = self.setup(*args)
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(self.steps(obj)["answers"], "already")
        self.assertEqual(len(decisions(target)), len(recorded))

    def test_a_changed_choice_supersedes_the_old_decision(self):
        target = os.path.join(self.place, "garden")
        base = ("--yes", "--launch", "none", "--new", target) + NO_ANSWERS
        self.assertEqual(self.setup(*base, "--plugin", "skip")[0], 0)
        code, obj, err = self.setup(*base, "--plugin", "plugin-dir")
        self.assertEqual(code, 0, (obj, err))
        plugin = [d for d in decisions(target) if d["question"] == onboard.Q_PLUGIN]
        self.assertEqual(sorted(d["status"] for d in plugin), ["active", "superseded"])
        active = [d for d in plugin if d["status"] == "active"][0]
        self.assertEqual(active["chosen"], "plugin-dir")
        self.assertTrue(active["supersedes"])
        self.assertIn("--plugin-dir ./plugins/general-ontology", " ".join(obj["next"]))


class HereAndTopicTest(SetupCase):
    def test_here_does_step_one_and_adds_the_users_origin(self):
        clone = self.clone_template()
        code, obj, err = self.setup("--here", "--yes", "--launch", "none", "--plugin", "skip", "--title",
                                    "Neighborhood kitchen", "--origin", "git@example.invalid:me/kitchen.git",
                                    *NO_ANSWERS, cwd=clone)
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(self.steps(obj)["clone"], "skipped")
        self.assertEqual(_support.git(clone, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(_support.git(clone, "remote", "get-url", "kit"), KIT_URL)
        self.assertEqual(_support.git(clone, "remote", "get-url", "origin"), "git@example.invalid:me/kitchen.git")
        self.assertEqual(obj["topic"]["name"], "neighborhood-kitchen")
        recorded = {d["question"]: d for d in decisions(clone)}
        self.assertEqual(recorded[onboard.Q_LOCATION]["chosen"], "here")
        self.assertEqual(recorded[onboard.Q_LOCATION]["chosen_text"], "a folder named clone")
        self.assertEqual(recorded[onboard.Q_REMOTE]["chosen_text"], "example.invalid:me/kitchen.git")
        self.assertEqual(_support.git(clone, "log", "-1", "--format=%s"), "Start neighborhood-kitchen")

    def test_a_finished_topic_runs_only_what_is_missing(self):
        clone = self.clone_template()
        self.assertEqual(self.setup("--here", "--yes", "--launch", "none", "--plugin", "skip", cwd=clone)[0], 0)
        code, obj, err = self.setup("--launch", "none", "--plugin", "local", *NO_ANSWERS, cwd=clone)
        self.assertEqual(code, 0, (obj, err))
        steps = self.steps(obj)
        self.assertEqual(steps["init"], "already")
        self.assertEqual(steps["branch"], "already")
        self.assertEqual(steps["plugin"], "done")
        local = read_json(os.path.join(clone, ".claude", "settings.local.json"))
        self.assertTrue(local["enabledPlugins"]["general-ontology@general-ontology"])
        # settings.local.json stays out of git; the new decision is committed
        self.assertEqual(_support.git(clone, "status", "--porcelain"), "")
        self.assertEqual(_support.git(clone, "log", "-1", "--format=%s"), "Record the setup of clone")

    def test_without_a_choice_and_without_a_terminal_it_asks_for_a_flag(self):
        code, obj, _err = self.setup("--launch", "none")
        self.assertEqual(code, 2)
        self.assertIn("--new DIR", obj["message"])

    def test_prompts_fill_what_the_flags_leave_out(self):
        onboard._interactive = lambda: True
        target = os.path.join(self.place, "picked")
        replies = iter(["new", "Garden to table", "", "g2t", target])
        asked = []
        with mock.patch.object(onboard, "_input", side_effect=lambda q: (asked.append(q), next(replies))[1]):
            code, obj, err = self.setup("--launch", "none", "--plugin", "skip")
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(len(asked), 5)
        self.assertEqual(obj["topic"], {"path": target, "name": "garden-to-table", "ns": "g2t",
                                        "title": "Garden to table"})
        chosen = {d["question"]: d["chosen"] for d in decisions(target)}
        self.assertEqual(chosen[onboard.Q_LOCATION], "elsewhere")

    def test_an_existing_topic_keeps_its_names_and_refuses_other_ones(self):
        target = os.path.join(self.place, "garden-plans")
        self.assertEqual(self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target)[0], 0)
        head = _support.git(target, "rev-parse", "HEAD")
        for flag, value in (("--ns", "kitchen"), ("--title", "Kitchen stock"), ("--name", "kitchen")):
            code, obj, _err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target, flag,
                                         value, *NO_ANSWERS)
            self.assertEqual(code, 2, (flag, obj))
            self.assertIn("already holds the topic garden-plans", obj["message"])
            self.assertIn(flag, obj["message"])
        self.assertEqual(_support.git(target, "rev-parse", "HEAD"), head)
        self.assertEqual(decisions(target), [])
        # the same names (or none) are fine, and the topic object names the real topic
        code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target, "--title",
                                    "Garden plans", *NO_ANSWERS)
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(obj["topic"], {"path": target, "name": "garden-plans", "ns": "garden-plans",
                                        "title": "Garden plans"})
        # nothing was decided about the location now: the topic was already there
        self.assertNotIn(onboard.Q_LOCATION, [d["question"] for d in decisions(target)])

    def test_personal_on_an_existing_topic_is_not_applied_or_recorded_when_it_differs(self):
        clone = self.clone_template()
        self.assertEqual(self.setup("--here", "--yes", "--launch", "none", "--plugin", "skip", cwd=clone)[0], 0)
        before = read_json(os.path.join(clone, "ontology.json"))["policy"]["personal"]
        code, obj, err = self.setup("--launch", "none", "--plugin", "skip", "--personal", "keep", *NO_ANSWERS,
                                    cwd=clone)
        self.assertEqual(code, 0, (obj, err))
        init = [s for s in obj["steps"] if s["id"] == "init"][0]
        self.assertEqual(init["status"], "already")
        self.assertIn("--personal keep not applied", init["detail"])
        self.assertEqual(read_json(os.path.join(clone, "ontology.json"))["policy"]["personal"], before)
        self.assertNotIn(onboard.Q_PERSONAL, [d["question"] for d in decisions(clone)])
        # a value that matches the policy is consistent, so it is recorded
        target = os.path.join(self.place, "kept")
        base = ("--yes", "--launch", "none", "--plugin", "skip", "--new", target, "--personal", "keep")
        self.assertEqual(self.setup(*base)[0], 0)
        code, obj, err = self.setup(*base, *NO_ANSWERS)
        self.assertEqual(code, 0, (obj, err))
        self.assertNotIn("not applied", json.dumps(obj))
        self.assertIn(onboard.Q_PERSONAL, [d["question"] for d in decisions(target)])

    def test_the_name_comes_from_the_title_before_the_folder(self):
        target = os.path.join(self.place, "short")
        code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target, "--title",
                                    "Garden")
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual((obj["topic"]["name"], obj["topic"]["ns"]), ("garden", "garden"))

    def test_the_commit_waits_for_the_ignore_rules(self):
        clone = self.clone_template()
        self.assertEqual(self.setup("--here", "--yes", "--launch", "none", "--plugin", "skip", cwd=clone)[0], 0)
        with open(os.path.join(clone, ".gitignore"), "w", encoding="utf-8") as fh:
            fh.write("inbox/\n")
        _support.commit_all(clone, "no .onto rule")
        head = _support.git(clone, "rev-parse", "HEAD")
        answers = json.dumps({"decisions": [{"question": "Who waters the beds?", "chosen": "me"}]})
        code, obj, err = self.setup("--launch", "none", "--plugin", "skip", "--answers", answers, cwd=clone)
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(self.steps(obj)["answers"], "done")
        commit = [s for s in obj["steps"] if s["id"] == "commit"][0]
        self.assertEqual(commit["status"], "skipped")
        self.assertIn(".onto/ not ignored", commit["detail"])
        self.assertEqual(_support.git(clone, "rev-parse", "HEAD"), head)


def config_env(home, pairs):
    """``git_env`` with git config ``pairs`` in place of ``GIT_CONFIG`` (env config, as the setup tests use)."""
    env = git_env(home)
    for i in range(len(GIT_CONFIG)):
        env.pop("GIT_CONFIG_KEY_%d" % i, None)
        env.pop("GIT_CONFIG_VALUE_%d" % i, None)
    env["GIT_CONFIG_COUNT"] = str(len(pairs))
    for i, (key, value) in enumerate(pairs):
        env["GIT_CONFIG_KEY_%d" % i] = key
        env["GIT_CONFIG_VALUE_%d" % i] = value
    return env


NO_IDENTITY = tuple(p for p in GIT_CONFIG if not p[0].startswith("user.")) + (("user.useConfigOnly", "true"),)


class GitIdentityTest(SetupCase):
    """A new machine without a git user name and email: setup stops before writing; a run whose commit failed is
    finished by the next run, which commits "Start <name>"."""

    def test_the_preflight_stops_without_a_git_identity(self):
        target = os.path.join(self.place, "garden")
        with mock.patch.dict(os.environ, config_env(self.home, NO_IDENTITY)):
            code, obj, _err = self.setup("--yes", "--launch", "none", "--plugin", "skip", "--new", target)
        self.assertEqual(code, 1)
        preflight = [s for s in obj["steps"] if s["id"] == "preflight"][0]
        self.assertEqual(preflight["status"], "failed")
        self.assertIn("git config --global user.email", preflight["detail"])
        self.assertFalse(os.path.exists(target))
        # no Next line points at the folder that was never made
        self.assertEqual(len(obj["next"]), 1)
        self.assertIn("run the same onto setup command again", obj["next"][0])
        self.assertNotIn("Folder:", " ".join(obj["next"]))

    def test_a_rerun_commits_after_a_failed_commit(self):
        target = os.path.join(self.place, "garden")
        argv = ("--yes", "--launch", "none", "--plugin", "skip", "--new", target)
        failing = GIT_CONFIG + (("commit.gpgsign", "true"), ("gpg.program", "false"))
        with mock.patch.dict(os.environ, config_env(self.home, failing)):
            code, obj, _err = self.setup(*argv)
        self.assertEqual(code, 1)
        self.assertEqual(self.steps(obj)["commit"], "failed")
        self.assertIn("then run onto setup again", [s for s in obj["steps"] if s["id"] == "commit"][0]["detail"])
        self.assertTrue(obj["next"][0].startswith("Fix the failed step above"))
        self.assertIn("Folder: %s" % target, obj["next"])
        self.assertFalse(_support.git(target, "log", "--format=%s", "-1", "--", "ontology.json"))
        code, obj, err = self.setup(*argv)
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(self.steps(obj)["commit"], "done")
        self.assertEqual(_support.git(target, "log", "--format=%s", "-1"), "Start garden")
        self.assertEqual(_support.git(target, "status", "--porcelain"), "")


class PluginTest(SetupCase):
    def test_project_wiring_with_claude_on_path(self):
        bin_dir = os.path.join(self.tmp, "stub-bin")
        write_stub(bin_dir)
        log = os.path.join(self.tmp, "claude.log")
        target = os.path.join(self.place, "garden")
        with mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + SYSTEM_PATH, "CLAUDE_LOG": log}):
            code, obj, err = self.setup("--yes", "--launch", "none", "--new", target)
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(self.steps(obj)["plugin"], "done")
        with open(log, encoding="utf-8") as fh:
            calls = [line.rstrip("|\n").split("|") for line in fh if line.strip()]
        # the listing first: a folder-sourced general-ontology marketplace would make the install load that folder
        self.assertEqual(calls, [
            [self.template_root, "plugin", "marketplace", "list", "--json"],
            [target, "plugin", "marketplace", "add", KIT_URL + "#general-ontology", "--scope", "project"],
            [target, "plugin", "install", "general-ontology@general-ontology", "--scope", "project"]])

    def test_a_claude_that_writes_no_settings_still_leaves_the_topic_wired(self):
        # the stub exits 0 for marketplace add and install but writes nothing, as an older claude might
        bin_dir = os.path.join(self.tmp, "stub-bin")
        write_stub(bin_dir)
        target = os.path.join(self.place, "garden")
        with mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + SYSTEM_PATH,
                                          "CLAUDE_LOG": os.path.join(self.tmp, "claude.log")}):
            code, obj, err = self.setup("--yes", "--launch", "none", "--new", target)
        self.assertEqual(code, 0, (obj, err))
        plugin = [s for s in obj["steps"] if s["id"] == "plugin"][0]
        self.assertEqual(plugin["status"], "done")
        self.assertIn("so setup wrote them", plugin["detail"])
        settings = read_json(os.path.join(target, ".claude", "settings.json"))
        self.assertEqual(settings["enabledPlugins"], {"general-ontology@general-ontology": True})
        self.assertEqual(settings["extraKnownMarketplaces"]["general-ontology"]["source"],
                         {"source": "github", "repo": "example-owner/garden-kit", "ref": "general-ontology"})
        self.assertTrue(doctor.settings_wiring(target)["project"])
        self.assertEqual(_support.git(target, "log", "--format=%s", "-1"), "Wire the general-ontology plugin")
        self.assertEqual(_support.git(target, "status", "--porcelain"), "")

    def test_a_shorthand_kit_url_is_a_github_remote_for_git(self):
        target = os.path.join(self.place, "garden")
        code, obj, err = self.setup("--yes", "--launch", "none", "--new", target, "--kit-url",
                                    "example-owner/garden-kit")
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(_support.git(target, "remote", "get-url", "kit"),
                         "https://github.com/example-owner/garden-kit.git")
        settings = read_json(os.path.join(target, ".claude", "settings.json"))
        self.assertEqual(settings["extraKnownMarketplaces"]["general-ontology"]["source"],
                         {"source": "github", "repo": "example-owner/garden-kit", "ref": "general-ontology"})
        self.assertEqual(onboard.git_url("owner/kit.git"), "https://github.com/owner/kit.git")
        self.assertEqual(onboard.git_url(KIT_URL), KIT_URL)
        self.assertEqual(onboard.git_url("./kit"), "./kit")

    def test_local_mode_never_installs_from_a_folder_marketplace(self):
        bin_dir = os.path.join(self.tmp, "stub-bin")
        stub = write_stub(bin_dir)
        log = os.path.join(self.tmp, "claude.log")
        with open(stub, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nprintf '%s|' \"$@\" >> \"$CLAUDE_LOG\"\nprintf '\\n' >> \"$CLAUDE_LOG\"\n"
                     "printf '%s\\n' '[{\"name\": \"general-ontology\", \"source\": {\"source\": \"directory\", "
                     "\"path\": \"/somewhere\"}}]'\n")
        target = os.path.join(self.place, "garden")
        with mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + SYSTEM_PATH, "CLAUDE_LOG": log}):
            code, obj, err = self.setup("--yes", "--launch", "none", "--plugin", "local", "--new", target)
        self.assertEqual(code, 0, (obj, err))
        plugin = [s for s in obj["steps"] if s["id"] == "plugin"][0]
        self.assertEqual(plugin["status"], "skipped")
        self.assertIn("local folder", plugin["detail"])
        self.assertIn("marketplace remove general-ontology", plugin["detail"])
        with open(log, encoding="utf-8") as fh:
            self.assertNotIn("install", fh.read())
        self.assertIn("--plugin-dir ./plugins/general-ontology", " ".join(obj["next"]))

    def test_a_local_kit_url_falls_back_to_plugin_dir(self):
        target = os.path.join(self.place, "garden")
        code, obj, err = self.setup("--yes", "--launch", "none", "--new", target, cwd=self.bare_template)
        self.assertEqual(code, 0, (obj, err))
        plugin = [s for s in obj["steps"] if s["id"] == "plugin"][0]
        self.assertEqual(plugin["status"], "skipped")
        self.assertIn("local folder", plugin["detail"])
        self.assertFalse(os.path.exists(os.path.join(target, ".claude", "settings.json")))
        self.assertIn("claude --plugin-dir ./plugins/general-ontology 'Start the ontology'", " ".join(obj["next"]))

    def test_settings_are_merged_never_dropped(self):
        folder = os.path.join(self.tmp, "t")
        os.makedirs(os.path.join(folder, ".claude"))
        path = os.path.join(folder, ".claude", "settings.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"permissions": {"allow": ["Bash(ls)"]}, "enabledPlugins": {"other@x": True},
                       "extraKnownMarketplaces": {"x": {"source": {"source": "github", "repo": "a/b"}}}}, fh)
        setup = onboard.Setup.__new__(onboard.Setup)
        setup.target, setup.kit_url = folder, "git@example.invalid:team/kit.git"
        status, _detail = setup._write_settings("project")
        self.assertEqual(status, "done")
        data = read_json(path)
        self.assertEqual(data["permissions"], {"allow": ["Bash(ls)"]})
        self.assertEqual(data["enabledPlugins"], {"other@x": True, "general-ontology@general-ontology": True})
        self.assertEqual(data["extraKnownMarketplaces"]["general-ontology"], {"source": {
            "source": "git", "url": "git@example.invalid:team/kit.git", "ref": "general-ontology"}})
        self.assertIn("x", data["extraKnownMarketplaces"])
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(setup._write_settings("project")[0], "failed")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "{not json")

    def test_launch_starts_claude_once_the_checklist_is_printed(self):
        bin_dir = os.path.join(self.tmp, "stub-bin")
        stub = write_stub(bin_dir)
        onboard._interactive = lambda: True
        started = []
        onboard._execvp = lambda exe, argv: started.append((exe, argv, os.getcwd()))
        target = os.path.join(self.place, "garden")
        old = os.getcwd()
        os.chdir(self.template_root)
        self.addCleanup(os.chdir, old)
        with mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + SYSTEM_PATH,
                                          "CLAUDE_LOG": os.path.join(self.tmp, "log")}):
            code, out, err = _support.run_cli(["setup", "--yes", "--plugin", "plugin-dir", "--new", target])
        self.assertEqual(code, 0, err)
        self.assertIn("starting Claude Code", out)
        self.assertEqual(started, [(stub, [stub, "--plugin-dir", "./plugins/general-ontology",
                                           "Start the ontology"], target)])


class HelpersTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(onboard.slug("Community garden: plots & beds!"), "community-garden-plots-beds")
        self.assertEqual(onboard.derive_ns("2026-market"), "market")
        self.assertEqual(onboard.derive_ns("self"), "self-topic")
        self.assertEqual(onboard.derive_ns("a-very-long-name-for-a-topic-about-gardens"), "a-very-long-name-for-a-topic")
        self.assertEqual(onboard.slug(""), "")
        self.assertEqual(onboard.title_from("farm-market"), "Farm market")

    def test_marketplace_sources(self):
        github = {"source": "github", "repo": "owner/kit", "ref": "general-ontology"}
        for url in ("https://github.com/owner/kit.git", "https://github.com/owner/kit", "git@github.com:owner/kit.git",
                    "owner/kit"):
            self.assertEqual(onboard.marketplace_source(url), github, url)
        self.assertEqual(onboard.marketplace_source("https://git.example.invalid/kit.git"),
                         {"source": "git", "url": "https://git.example.invalid/kit.git", "ref": "general-ontology"})

    def test_local_sources_and_display(self):
        from ontokit import doctor

        for url in ("/srv/kit", "./kit", "~/kit", "file:///srv/kit", "", None, "C:\\kit"):
            self.assertTrue(doctor.local_source(url), url)
        for url in ("https://example.invalid/kit.git", "git@example.invalid:o/k.git", "owner/kit"):
            self.assertFalse(doctor.local_source(url), url)
        self.assertEqual(onboard.display_url("https://me:tok@example.invalid/x.git"), "https://example.invalid/x.git")
        self.assertEqual(onboard.display_url("git@example.invalid:o/k.git"), "example.invalid:o/k.git")

    def test_add_builtin_packs_keeps_local_last(self):
        tmp = tempfile.mkdtemp(prefix="onto-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root = _support.bare_topic(tmp)
        path = os.path.join(root, "ontology.json")
        manifest = read_json(path)
        manifest["packs"] = ["core", "local"]
        store.write_json(path, manifest)
        self.assertEqual(onboard.add_builtin_packs(root, ["discovery"]), ["discovery"])
        self.assertEqual(read_json(path)["packs"], ["core", "discovery", "local"])
        self.assertEqual(onboard.add_builtin_packs(root, ["discovery"]), [])
        rows, _bad = store.read_jsonl(os.path.join(root, "ledger", "changes.jsonl"))
        self.assertEqual([r["summary"] for r in rows if r.get("type") == "pack"], ["add pack discovery"])
        with self.assertRaises(Exception):
            onboard.check_packs(["local"], root)


    def test_answers_file_shapes(self):
        ok = onboard.check_answers({"answers": [{"q": "q.frame.you", "status": "skipped"}],
                                    "decisions": [{"question": "Q?", "chosen": "a", "options": [{"id": "a",
                                                                                                  "label": "A"}]}]})
        self.assertEqual(len(ok["answers"]), 1)
        self.assertEqual(onboard._option_texts(ok["decisions"][0]["options"]), ["a=A"])
        self.assertEqual(onboard.check_answers(None), {"answers": [], "decisions": []})
        for bad in ([], {"answers": {}}, {"answers": [{"q": "x", "status": "maybe"}]},
                    {"decisions": [{"question": "Q?"}]}, {"decisions": [{"question": "Q?", "chosen": "a",
                                                                         "when": "now"}]}):
            with self.assertRaises(Exception):
                onboard.check_answers(bad)

    def test_run_onto_serves_a_folder_without_its_own_kit_in_process(self):
        tmp = tempfile.mkdtemp(prefix="onto-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root = _support.init_topic(tmp, "garden")
        obj = onboard.run_onto(root, ["decide", "--json", "--repo=%s" % root, "--question=Who waters?",
                                      "--chosen=me", "--options=[\"me=Me\", \"club=The club\"]"])
        self.assertEqual(obj["exit_code"], 0, obj)
        self.assertEqual(obj["decision"]["chosen"], "me")
        obj = onboard.run_onto(root, ["decide", "--json", "--repo=%s" % root, "--question=Q", "--chosen=x",
                                      "--options=[\"a=A\"]"])
        self.assertEqual(obj["exit_code"], 2)
        self.assertIn("not one of the options", onboard._message(obj))


class InitPersonalTest(_support.TempCase):
    def test_init_personal_sets_every_kind(self):
        root = os.path.join(self.tmp, "g")
        code, _out, err = _support.run_cli(["init", "--ns", "garden", "--title", "Garden", "--path", root,
                                            "--personal", "keep"])
        self.assertEqual(code, 0, err)
        personal = read_json(os.path.join(root, "ontology.json"))["policy"]["personal"]
        self.assertEqual(sorted(personal), sorted(store.DEFAULT_POLICY["personal"]))
        self.assertEqual(set(personal.values()), {"keep"})
        code, _out, _err = _support.run_cli(["init", "--ns", "kitchen", "--title", "K", "--path",
                                             os.path.join(self.tmp, "k"), "--personal", "maybe"])
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "k")))

    def test_init_without_personal_keeps_the_defaults(self):
        root = os.path.join(self.tmp, "g")
        self.assertEqual(_support.run_cli(["init", "--ns", "garden", "--title", "Garden", "--path", root])[0], 0)
        self.assertEqual(read_json(os.path.join(root, "ontology.json"))["policy"], store.DEFAULT_POLICY)


class LauncherTest(SetupCase):
    def run_launcher(self, shell, script, target, extra_env=None, args=()):
        env = dict(self.env, **(extra_env or {}))
        cmd = [shell, os.path.join(self.template_root, script), "--yes", "--launch", "none", "--new", target]
        return subprocess.run(cmd + list(args), cwd=self.tmp, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)

    def test_new_topic_runs_under_every_shell(self):
        ran = 0
        for shell in ("sh", "bash", "zsh"):
            exe = shutil.which(shell, path=SYSTEM_PATH)
            if not exe:
                continue
            ran += 1
            target = os.path.join(self.place, "%s run" % shell, "garden")
            proc = self.run_launcher(exe, "new-topic", target, args=("--plugin", "skip"))
            self.assertEqual(proc.returncode, 0, (shell, proc.stderr.decode("utf-8", "replace")))
            self.assertIn(b"done     init", proc.stdout)
            self.assertTrue(os.path.isfile(os.path.join(target, "ontology.json")), shell)
            self.assertEqual(_support.git(target, "log", "-1", "--format=%s"), "Start garden")
        self.assertGreater(ran, 0)

    def test_new_topic_wires_the_plugin_with_claude_on_path(self):
        bin_dir = os.path.join(self.tmp, "stub-bin")
        write_stub(bin_dir)
        log = os.path.join(self.tmp, "claude.log")
        target = os.path.join(self.place, "garden")
        proc = self.run_launcher(shutil.which("sh", path=SYSTEM_PATH), "new-topic", target,
                                 {"PATH": bin_dir + os.pathsep + SYSTEM_PATH, "CLAUDE_LOG": log})
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        with open(log, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("plugin|install|general-ontology@general-ontology|--scope|project", text)

    def test_the_command_file_waits_and_keeps_the_exit_code(self):
        target = os.path.join(self.place, "garden")
        proc = self.run_launcher(shutil.which("sh", path=SYSTEM_PATH), "New topic.command", target,
                                 args=("--plugin", "skip"))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertIn(b"Press Return", proc.stdout)
        busy = os.path.join(self.place, "busy")
        os.makedirs(busy)
        with open(os.path.join(busy, "x"), "w") as fh:
            fh.write("x")
        proc = self.run_launcher(shutil.which("sh", path=SYSTEM_PATH), "New topic.command", busy)
        self.assertEqual(proc.returncode, 1)

    def test_the_launchers_are_kit_files_with_the_right_modes(self):
        for name in ("new-topic", "New topic.command"):
            mode = os.stat(os.path.join(REPO_ROOT, name)).st_mode
            self.assertTrue(mode & stat.S_IXUSR, name)
            with open(os.path.join(REPO_ROOT, name), encoding="utf-8") as fh:
                self.assertTrue(fh.read().startswith("#!/bin/sh\n"), name)
        with open(os.path.join(REPO_ROOT, "new-topic"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("set -eu", text)
        self.assertIn('exec "$py" "$onto" setup "$@"', text)
        with open(os.path.join(REPO_ROOT, "new-topic.cmd"), encoding="utf-8") as fh:
            text = fh.read()
        for needle in ("py -3", "python", "setup %*", "pause", 'cd /d "%~dp0"'):
            self.assertIn(needle, text)



class DocumentedSetupTest(SetupCase):
    """The onto-interview skill, "1. No ontology yet", run as written: its answers file and its ``O setup`` line make
    a committed topic that holds the user's goal and records the user's choices as decisions (B1, B2, B3)."""

    def test_the_skill_setup_line_runs_as_written(self):
        import re
        import shlex

        with open(os.path.join(_support.PLUGIN_DIR, "skills", "onto-interview", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        first = text.split("## 1. No ontology yet", 1)[1].split("\n## ", 1)[0]
        (answers,) = [json.loads(b) for b in re.findall(r"```json\n(.*?)```", first, re.S)]
        lines = [line for line in first.splitlines() if line.startswith("O setup ")]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("--answers @.onto/setup.json", lines[0])
        self.assertNotIn("git rev-parse", first)
        clone = self.clone_template()
        os.makedirs(os.path.join(clone, ".onto"))
        with open(os.path.join(clone, ".onto", "setup.json"), "w", encoding="utf-8") as fh:
            json.dump(answers, fh)
        target = os.path.join(self.place, "garden")
        line = lines[0][len("O setup "):].replace("~/Ontologies/<slug>", shlex.quote(target))
        for key, value in (("<slug>", "community-garden"), ("<ns>", "garden"), ("<title>", "Community garden")):
            line = line.replace(key, value)
        code, obj, err = self.setup(*shlex.split(line), cwd=clone)
        self.assertEqual(code, 0, (obj, err))
        steps = self.steps(obj)
        for step in ("clone", "branch", "init", "answers", "commit", "plugin"):
            self.assertEqual(steps[step], "done", (step, obj))
        rows, _problems = store.read_jsonl(os.path.join(target, "interview", "log.jsonl"))
        goal = [r for r in rows if r.get("q") == "q.frame.goal"]
        self.assertEqual([r["status"] for r in goal], ["answered"])
        recorded = {d["question"]: d for d in decisions(target)}
        for question in (onboard.Q_LOCATION, onboard.Q_PERSONAL, onboard.Q_PLUGIN):
            self.assertEqual(recorded[question]["decided_by"], "user", question)
        self.assertEqual(recorded[onboard.Q_PERSONAL]["chosen"], "redact")
        self.assertEqual(_support.git(target, "status", "--porcelain"), "")

    def test_the_agent_manual_asks_the_questions_setup_records(self):
        """The setup interview (``plugins/general-ontology/docs/setup-interview.md``, linked from AGENTS.md, section
        1) asks the setup questions in the words setup records them under, so an agent can skip the ones
        ``onto decisions`` already lists. H5 moved the questions out of AGENTS.md; the needles moved with them."""
        with open(os.path.join(REPO_ROOT, "AGENTS.md"), encoding="utf-8") as fh:
            start = " ".join(fh.read().split("## 2. ", 1)[0].split())
        self.assertIn("plugins/general-ontology/docs/setup-interview.md", start)
        with open(os.path.join(_support.PLUGIN_DIR, "docs", "setup-interview.md"), encoding="utf-8") as fh:
            text = " ".join(fh.read().split())
        for question in (onboard.Q_LOCATION, onboard.Q_REMOTE, onboard.Q_PERSONAL, onboard.Q_PLUGIN):
            self.assertIn('"%s"' % question, text)
        self.assertIn("onto setup", text)
        self.assertIn("@.onto/setup.json", text)


if __name__ == "__main__":
    unittest.main()
