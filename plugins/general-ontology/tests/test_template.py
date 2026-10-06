"""The template branch holds the kit and no topic data (SPEC B.1 to B.3), and the rules every topic repo inherits
from it: ``.gitignore``, ``.gitattributes`` and the CI workflow (G.7).

In a topic repo (a clone of the template with ``ontology.json`` at its root) the "no topic data" check does not
apply and is skipped; the inherited rules are still checked.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from typing import List

from tests import _support

REPO_ROOT = os.path.dirname(os.path.dirname(_support.PLUGIN_DIR))
BRANCH = "general-ontology"
# B.2: every path onto init creates or a topic writes (the built and gitignored ones included)
TOPIC_PATHS = ("ontology.json", "packs", "graph", "sources", "proposals", "interview", "ledger", "metrics", "imports",
               "build", "MANIFEST.json", "VERSIONS.md", "inbox", ".onto")
# B.3: the kit-owned paths
KIT_PATHS = ("plugins/general-ontology", ".claude-plugin/marketplace.json", ".github/workflows/checks.yml", "examples",
             "README.md", "AGENTS.md", "CLAUDE.md", ".gitignore", ".gitattributes", "new-topic", "New topic.command",
             "new-topic.cmd", ".agents")
# the append-only logs, and the local questions (one question per line, only ever appended)
UNION = ("interview/log.jsonl", "ledger/changes.jsonl", "metrics/history.jsonl", "sources/index.jsonl",
         "packs/local.questions.jsonl")
NOT_UNION = ("graph/nodes.jsonl", "graph/edges.jsonl", "ontology.json", "packs/local.pack.json",
             "imports/lock.json")
# the union rules match at any depth, like the sources rule and .gitignore: a topic made with ``onto init --path``
# below the git root merges its logs the same way
UNION_RULES = tuple("**/" + rel for rel in UNION)
NESTED = "topics/t/"
# SPEC B.1: the topic rules match at any depth (a topic made with ``onto init --path`` below the git root keeps its
# raw inbox and kit state out of git too); only the template's own demo notes are re-included
GITIGNORE_RULES = ("inbox/", ".onto/", "build/index.html", "**/build/index.html", "!/examples/*/inbox/", "__pycache__/",
                   "*.pyc", ".DS_Store", "* [0-9].*", "**/.claude/settings.local.json", "**/.devin/*.local.json")
# the skills write CLI object arguments to .onto/ops.json and .onto/prop.json, so they never land in a commit
IGNORED = ("inbox/notes.md", "inbox/sub/transcript.vtt", ".onto/lock", ".onto/tools-check.json", ".onto/ops.json",
           ".onto/prop.json", "build/index.html",
           "topics/t/inbox/raw.md", "topics/t/inbox/sub/call.vtt", "topics/t/.onto/lock", "topics/t/build/index.html",
           "plugins/general-ontology/ontokit/__pycache__/util.cpython-39.pyc", "stray.pyc", ".DS_Store",
           "graph/.DS_Store", "notes 2.md", "graph/nodes 2.jsonl",
           # a local-scope plugin install (README step 2) writes it; it is per person and per machine
           ".claude/settings.local.json", "topics/t/.claude/settings.local.json",
           # Devin's local overrides (harness spec H3)
           ".devin/config.local.json", "topics/t/.devin/config.local.json")
TRACKED = ("build/export.json", "build/cards.json", "MANIFEST.json", "VERSIONS.md", "graph/nodes.jsonl",
           "sources/src-0123456789ab.txt", "topics/t/build/export.json", "topics/t/graph/nodes.jsonl",
           "topics/t/sources/index.jsonl", "examples/garden/inbox/handbook-excerpt.md",
           "examples/kitchen/inbox/menu-notes.md", "plugins/general-ontology/ontokit/util.py", ".gitignore",
           ".claude/settings.json", ".agents/skills/onto/SKILL.md", ".devin/blueprint.yaml", ".devin/hooks.v1.json",
           ".cursor/mcp.json", ".vscode/mcp.json")
# byte-exact topic data: text files keep LF on every checkout; stored sources (at any depth) are never converted
LF_TEXT = ("graph/nodes.jsonl", "ontology.json", "interview/log.jsonl", "imports/garden/export.json",
           "build/export.json", "MANIFEST.json", "examples/demo.sh", "plugins/general-ontology/ontokit/util.py")
NO_EOL_CONVERSION = ("sources/src-0123456789ab.txt", "sources/src-0123456789ab.orig.pdf", "sources/index.jsonl",
                     "topics/t/sources/src-0123456789ab.txt",
                     "plugins/general-ontology/tests/fixtures/mini/sources/src-000a61a61d03.txt")
# runner images whose Python toolcache has a 3.9 build (the kit's floor); ubuntu-latest moves past them
PY39_IMAGES = ("ubuntu-22.04", "ubuntu-24.04")


def _git(root: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, **_support.GIT_ENV)
    return subprocess.run(["git", "-C", root] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          stdin=subprocess.DEVNULL, env=env)


def _tracked(root: str) -> List[str]:
    """Files git tracks at ``root`` (empty when it is not a git checkout)."""
    proc = _git(root, "ls-files", "-z")
    if proc.returncode != 0:
        return []
    return [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p]


def _branch(root: str) -> str:
    proc = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return proc.stdout.decode("utf-8", "replace").strip() if proc.returncode == 0 else ""


def _has_git() -> bool:
    return shutil.which("git") is not None


class TemplateTest(unittest.TestCase):
    def _is_topic_repo(self) -> bool:
        return os.path.isfile(os.path.join(REPO_ROOT, "ontology.json"))

    def test_the_kit_paths_are_present(self) -> None:
        for rel in KIT_PATHS:
            self.assertTrue(os.path.exists(os.path.join(REPO_ROOT, rel)), rel)

    def test_no_topic_path_exists_on_the_branch(self) -> None:
        if self._is_topic_repo():
            if _has_git() and _branch(REPO_ROOT) == BRANCH:
                self.fail("ontology.json sits on the %s branch: the template must hold no topic data" % BRANCH)
            self.skipTest("a topic repo: the template holds no topic data, a topic repo does")
        present = [rel for rel in TOPIC_PATHS
                   if rel not in ("inbox", ".onto") and os.path.lexists(os.path.join(REPO_ROOT, rel))]
        self.assertEqual(present, [], "topic paths at the root of the template: %s" % present)
        tops = {p.split("/", 1)[0] for p in _tracked(REPO_ROOT)}
        self.assertEqual(sorted(tops & set(TOPIC_PATHS)), [])

    def test_no_junk_is_tracked(self) -> None:
        junk = re.compile(r"(^|/)(__pycache__/|\.DS_Store$)|\.py[co]$| [0-9]+\.[^/]*$")
        self.assertEqual([p for p in _tracked(REPO_ROOT) if junk.search(p)], [])

    def test_gitignore_lists_the_topic_and_junk_rules(self) -> None:
        with open(os.path.join(REPO_ROOT, ".gitignore"), encoding="utf-8") as fh:
            rules = [line.strip() for line in fh if line.strip() and not line.startswith("#")]
        for want in GITIGNORE_RULES:
            self.assertIn(want, rules)
        for anchored in ("/inbox/", "/.onto/"):  # a root-only rule leaves a nested topic's raw inbox unignored
            self.assertNotIn(anchored, rules)

    @unittest.skipUnless(_has_git(), "git is not installed")
    def test_gitignore_ignores_exactly_the_local_files(self) -> None:
        tmp = tempfile.mkdtemp(prefix="onto-template-")
        self.addCleanup(shutil.rmtree, tmp, True)
        _git(tmp, "init", "-q")
        shutil.copy(os.path.join(REPO_ROOT, ".gitignore"), os.path.join(tmp, ".gitignore"))
        for rel in IGNORED + TRACKED:
            if rel == ".gitignore":
                continue
            path = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("x\n")
        for rel in IGNORED:
            self.assertEqual(_git(tmp, "check-ignore", "-q", "--", rel).returncode, 0, "%s is not ignored" % rel)
        for rel in TRACKED:
            self.assertEqual(_git(tmp, "check-ignore", "-q", "--", rel).returncode, 1, "%s is ignored" % rel)
        # what `git add -A` would commit: exactly the tracked files, never a raw inbox file or the kit's lock
        self.assertEqual(_git(tmp, "add", "-A").returncode, 0)
        self.assertEqual(sorted(_tracked(tmp)), sorted(TRACKED))

    def test_gitattributes_unions_only_the_append_only_logs(self) -> None:
        with open(os.path.join(REPO_ROOT, ".gitattributes"), encoding="utf-8") as fh:
            rules = [line.split() for line in fh if line.strip() and not line.startswith("#")]
        union = sorted(r[0] for r in rules if "merge=union" in r[1:])
        self.assertEqual(union, sorted(UNION_RULES))

    @unittest.skipUnless(_has_git(), "git is not installed")
    def test_gitattributes_as_git_reads_them(self) -> None:
        """The union rules hold at the root and for a topic below it (``onto init --path``); the graph never
        merges by union at any depth."""
        tmp = tempfile.mkdtemp(prefix="onto-template-")
        self.addCleanup(shutil.rmtree, tmp, True)
        _git(tmp, "init", "-q")
        shutil.copy(os.path.join(REPO_ROOT, ".gitattributes"), os.path.join(tmp, ".gitattributes"))
        for prefix in ("", NESTED):
            for rel in UNION + NOT_UNION:
                path = prefix + rel
                out = _git(tmp, "check-attr", "merge", "--", path).stdout.decode("utf-8").strip()
                want = "union" if rel in UNION else "unspecified"
                self.assertTrue(out.endswith("merge: %s" % want), "%s: %s" % (path, out))

    @unittest.skipUnless(_has_git(), "git is not installed")
    def test_gitattributes_keep_topic_bytes_exact(self) -> None:
        tmp = tempfile.mkdtemp(prefix="onto-template-")
        self.addCleanup(shutil.rmtree, tmp, True)
        _git(tmp, "init", "-q")
        shutil.copy(os.path.join(REPO_ROOT, ".gitattributes"), os.path.join(tmp, ".gitattributes"))

        def attrs(rel: str) -> str:
            return _git(tmp, "check-attr", "text", "eol", "--", rel).stdout.decode("utf-8")

        for rel in LF_TEXT:
            self.assertIn("text: auto", attrs(rel), rel)
            self.assertIn("eol: lf", attrs(rel), rel)
        for rel in NO_EOL_CONVERSION:
            self.assertIn("text: unset", attrs(rel), rel)

    @unittest.skipUnless(_has_git(), "git is not installed")
    def test_a_crlf_checkout_keeps_a_topic_valid(self) -> None:
        """A clone with core.autocrlf=true (the Git for Windows default) gets the same bytes, so hashes and
        canonical JSON still check."""
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="onto-template-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        root = _support.make_topic(tmp, "mini", "mini")
        for dotfile in (".gitattributes", ".gitignore"):
            shutil.copy(os.path.join(REPO_ROOT, dotfile), os.path.join(root, dotfile))
        with open(os.path.join(root, "sources", "src-000a61a61d03.txt"), "rb") as fh:
            self.assertNotIn(b"\r", fh.read())
        _support.git_init(root)
        _support.commit_all(root, "topic")
        clone = os.path.join(tmp, "clone")
        proc = _git(tmp, "-c", "core.autocrlf=true", "clone", "-q", root, clone)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(_git(clone, "config", "core.autocrlf", "true").returncode, 0)
        code, out, err = _support.run_cli(["validate"], repo=clone)
        self.assertEqual(code, 0, out + err)
        for rel in _tracked(root):
            with open(os.path.join(root, rel), "rb") as a, open(os.path.join(clone, rel), "rb") as b:
                self.assertEqual(a.read(), b.read(), "%s changed in a core.autocrlf=true checkout" % rel)

    @unittest.skipUnless(_has_git(), "git is not installed")
    def test_the_logs_merge_by_union_at_any_depth(self) -> None:
        """Two branches that each append to every log merge without a conflict, at the root and in a topic below
        it; ``sources/** -text`` does not take the union merge away from ``sources/index.jsonl``."""
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="onto-template-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        _support.git_init(tmp)
        shutil.copy(os.path.join(REPO_ROOT, ".gitattributes"), os.path.join(tmp, ".gitattributes"))
        logs = [prefix + rel for prefix in ("", NESTED) for rel in UNION]

        def append(line: str) -> None:
            for rel in logs:
                path = os.path.join(tmp, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(line + "\n")

        append('{"id":"src-000000000001"}')
        _support.commit_all(tmp, "base")
        _support.git(tmp, "checkout", "-q", "-b", "other")
        append('{"id":"src-000000000002"}')
        _support.commit_all(tmp, "other")
        _support.git(tmp, "checkout", "-q", "main")
        append('{"id":"src-000000000003"}')
        _support.commit_all(tmp, "main")
        _support.git(tmp, "merge", "-q", "--no-edit", "other")
        for rel in logs:
            with open(os.path.join(tmp, rel), encoding="utf-8") as fh:
                lines = sorted(fh.read().splitlines())
            self.assertEqual(lines, ['{"id":"src-000000000001"}', '{"id":"src-000000000002"}',
                                     '{"id":"src-000000000003"}'], rel)


    def test_ci_runs_on_a_fixed_image_with_pinned_actions(self) -> None:
        with open(os.path.join(REPO_ROOT, ".github", "workflows", "checks.yml"), encoding="utf-8") as fh:
            text = fh.read()
        runs_on = re.findall(r"(?m)^    runs-on: (\S+)\s*$", text)
        self.assertEqual(len(runs_on), 1, runs_on)
        self.assertIn(runs_on[0], PY39_IMAGES, "runs-on must name an image with a Python 3.9 build, not a moving label")
        uses = re.findall(r"(?m)^\s*- uses: (\S+)(.*)$", text)
        self.assertEqual(len(uses), 2, uses)
        for action, rest in uses:
            self.assertRegex(action, r"^actions/[a-z-]+@[0-9a-f]{40}$", "pin %s to a full commit id" % action)
            self.assertRegex(rest, r"^ # v\d+\.\d+\.\d+$", "name the release pinned by %s" % action)

    def test_ci_workflow_follows_g7(self) -> None:
        path = os.path.join(REPO_ROOT, ".github", "workflows", "checks.yml")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        for trigger in ("pull_request:", "types: [opened, synchronize, reopened, edited]", "push:", "schedule:",
                        "workflow_dispatch:"):
            self.assertIn(trigger, body)
        self.assertRegex(body, r"(?m)^permissions:\n  contents: read$")
        self.assertIn("persist-credentials: false", body)
        self.assertIn('python-version: "3.9"', body)
        self.assertNotIn("pip install", body)
        jobs = body.split("\njobs:\n", 1)[1]
        self.assertEqual(re.findall(r"(?m)^  ([a-z][\w-]*):$", jobs), ["checks"])
        self.assertIsNone(re.search(r"(?m)^    if:", jobs), "no job-level if")
        steps = [
            "python -m unittest discover -s plugins/general-ontology/tests -t plugins/general-ontology",
            "python3 plugins/general-ontology/bin/onto scan .",
            "bash examples/demo.sh --check",
            "python3 plugins/general-ontology/bin/onto bench --against "
            "plugins/general-ontology/tests/golden/bench.json",
        ]
        blocks = re.split(r"(?m)^      - ", jobs)[1:]
        runs = [b for b in blocks if "run: " in b]
        self.assertEqual([re.search(r"run: (.*)", b).group(1).strip() for b in runs], steps)
        self.assertNotIn("if:", runs[0])
        for block in runs[1:]:
            self.assertIn("if: ${{ !cancelled() }}", block)
        self.assertIn("continue-on-error: true", runs[3])
        for block in runs[:3]:
            self.assertNotIn("continue-on-error", block)


@unittest.skipUnless(_has_git(), "git is not installed")
class MergeTopicBranchesTest(unittest.TestCase):
    """The procedure AGENTS.md documents under "Merging topic branches": two sessions that each add a node conflict
    in the graph files; keeping both sides' lines, then ``onto validate --fix``, gives a valid topic, and one id
    changed on both sides is reported (P06) for the user to settle."""

    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="onto-template-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = _support.init_topic(self.tmp, "demo", "Demo topic")
        for dotfile in (".gitattributes", ".gitignore"):
            shutil.copy(os.path.join(REPO_ROOT, dotfile), os.path.join(self.root, dotfile))
        _support.git_init(self.root)
        _support.commit_all(self.root, "init")

    def cli(self, *args: str) -> str:
        code, out, err = _support.run_cli(list(args), repo=self.root)
        self.assertEqual(code, 0, out + err)
        return out

    def answer(self, branch: str, question: str, text: str, ops: list, *flags: str) -> None:
        _support.git(self.root, "checkout", "-q", "-b", branch, "main")
        self.cli("answer", question, text, "--ops", json.dumps(ops), "--apply", *flags)
        _support.commit_all(self.root, branch)

    def add(self, kind: str, name: str, quote: str) -> list:
        return [{"op": "add_node", "ref": "$n", "basis": "stated", "node": {"kind": kind, "name": name},
                 "prov": [{"quote": quote, "by": "user"}]},
                {"op": "add_edge", "basis": "stated", "edge": {"src": "$n", "rel": "part_of", "dst": "topic:demo"},
                 "prov": [{"quote": quote, "by": "user"}]}]

    def merge(self, *branches: str) -> List[str]:
        """Merge each branch into main; returns the conflicted files of the last merge, with the base shown."""
        _support.git(self.root, "checkout", "-q", "main")
        for branch in branches[:-1]:
            _support.git(self.root, "merge", "-q", "--no-edit", branch)
        _git(self.root, "merge", "-q", "--no-edit", branches[-1])
        conflicted = _support.git(self.root, "diff", "--name-only", "--diff-filter=U").splitlines()
        if conflicted:
            _support.git(self.root, "checkout", "--conflict=diff3", "--", *conflicted)
        return conflicted

    def keep_both_sides(self, rel: str) -> None:
        """What the docs tell the agent: drop the marker lines and the base section, keep every line of both
        sides."""
        path = os.path.join(self.root, rel)
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines(True)
        out, section = [], None
        for line in lines:
            if line.startswith("<<<<<<< "):
                section = "ours"
            elif line.startswith("||||||| "):
                section = "base"
            elif line.startswith("=======") and section:
                section = "theirs"
            elif line.startswith(">>>>>>> "):
                section = None
            elif section != "base":
                out.append(line)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("".join(out))

    def test_different_ids_keep_both_lines_then_validate_fix(self) -> None:
        self.answer("a", "q.frame.goal", "The goal is fair weekly shares.", self.add("goal", "Fair weekly shares",
                                                                                     "fair weekly shares"))
        self.answer("b", "q.frame.deliverable", "First a weekly share sheet.",
                    self.add("deliverable", "Weekly share sheet", "a weekly share sheet"))
        conflicted = self.merge("a", "b")
        self.assertEqual(sorted(conflicted), ["graph/edges.jsonl", "graph/nodes.jsonl"])
        code, out, _err = _support.run_cli(["validate"], repo=self.root)
        self.assertEqual(code, 1)
        self.assertIn("P01", out)
        for rel in conflicted:
            self.keep_both_sides(rel)
        self.cli("validate", "--fix")
        out = self.cli("validate")
        self.assertIn("ok: 3 nodes, 2 edges", out)
        self.assertNotIn("P0", out)
        _support.commit_all(self.root, "Merge b")
        for nid in ("goal:fair-weekly-shares", "deliverable:weekly-share-sheet"):
            self.assertIn(nid, self.cli("get", nid))

    def resolve_pack(self) -> None:
        """What the docs tell the agent for ``packs/local.pack.json``: in each conflict, write both sides' entries as
        one JSON object, then drop the marker lines and the base section (order and spacing do not matter)."""
        path = os.path.join(self.root, "packs", "local.pack.json")
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines(True)

        def obj(part: List[str]) -> dict:
            return json.loads("{" + "".join(part).strip().rstrip(",") + "}")

        def union(a, b):
            if isinstance(a, dict) and isinstance(b, dict):
                return {k: union(a[k], b[k]) if k in a and k in b else a.get(k, b.get(k)) for k in set(a) | set(b)}
            if isinstance(a, list) and isinstance(b, list):
                return a + [x for x in b if x not in a]
            self.assertEqual(a, b, "one name declared two ways is the user's call")
            return a

        out, sides, section = [], {"ours": [], "base": [], "theirs": []}, None
        for line in lines:
            if line.startswith("<<<<<<< "):
                section = "ours"
            elif line.startswith("||||||| "):
                section = "base"
            elif line.startswith("=======") and section:
                section = "theirs"
            elif line.startswith(">>>>>>> "):
                merged = union(obj(sides["ours"]), obj(sides["theirs"]))
                comma = "," if "".join(sides["ours"]).rstrip().endswith(",") else ""
                out.append(json.dumps(merged)[1:-1] + comma + "\n")
                sides, section = {"ours": [], "base": [], "theirs": []}, None
            elif section:
                sides[section].append(line)
            else:
                out.append(line)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("".join(out))

    def pack_answer(self, branch: str, question: str, kind: str, name: str, quote: str, qid: str) -> None:
        """An answer that extends the local pack (a kind and a question) and adds a node of the new kind."""
        ops = [{"op": "add_kind", "name": kind, "kind": {"label": kind.capitalize()}},
               {"op": "add_question", "question": {"id": qid, "stage": 3, "ask": "Which %s matter?" % kind,
                                                   "why": "They shape the topic.",
                                                   "fills": {"kinds": [kind], "fields": []}}}]
        self.answer(branch, question, "We use %s." % quote, ops + self.add(kind, name, quote), "--confirm")

    def test_two_branches_that_extend_the_local_pack_keep_both_sides(self) -> None:
        self.pack_answer("a", "q.frame.goal", "bike", "Gravel bike", "a gravel bike", "q.local.tyres")
        self.pack_answer("b", "q.frame.deliverable", "part", "Spare chain", "a spare chain", "q.local.tools")
        conflicted = self.merge("a", "b")
        # the questions merge by union; the pack and the graph are the agent's to resolve
        self.assertEqual(sorted(conflicted), ["graph/edges.jsonl", "graph/nodes.jsonl", "packs/local.pack.json"])
        code, out, _err = _support.run_cli(["validate"], repo=self.root)
        self.assertEqual(code, 1)
        self.assertIn("P01", out)
        for rel in ("graph/edges.jsonl", "graph/nodes.jsonl"):
            self.keep_both_sides(rel)
        self.resolve_pack()
        self.cli("validate", "--fix")
        out = self.cli("validate")
        self.assertIn("ok: 3 nodes, 2 edges", out)
        self.assertNotIn("P0", out)
        _support.commit_all(self.root, "Merge b")
        with open(os.path.join(self.root, "packs", "local.pack.json"), encoding="utf-8") as fh:
            self.assertEqual(sorted(json.load(fh)["kinds"]), ["bike", "part"])
        with open(os.path.join(self.root, "packs", "local.questions.jsonl"), encoding="utf-8") as fh:
            self.assertEqual(sorted(json.loads(line)["id"] for line in fh), ["q.local.tools", "q.local.tyres"])
        for nid in ("bike:gravel-bike", "part:spare-chain"):
            self.assertIn(nid, self.cli("get", nid))

    def test_one_question_id_added_on_both_sides_is_reported(self) -> None:
        def question(branch: str, q: str, ask: str) -> None:
            op = {"op": "add_question", "question": {"id": "q.local.route", "stage": 3, "ask": ask,
                                                     "why": "Routes shape the plan.",
                                                     "fills": {"kinds": [], "fields": []}}}
            self.answer(branch, q, "We ride the river loop.", [op], "--confirm")

        question("e", "q.frame.goal", "Which route do you ride most?")
        question("f", "q.frame.deliverable", "Which routes matter most to you?")
        self.assertEqual(self.merge("e", "f"), [])  # git completes it: the questions merge by union
        # steps 6 and 7 still run; --fix collapses the answer both sides stored, never one of two questions
        for args in (["validate", "--fix"], ["validate"]):
            code, out, _err = _support.run_cli(args, repo=self.root)
            self.assertEqual(code, 1, out)
            self.assertIn("P20 duplicate question id 'q.local.route'", out)
        # the user keeps one question: the other line goes, and the topic validates
        path = os.path.join(self.root, "packs", "local.questions.jsonl")
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines(True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("".join(line for line in lines if "matter most to you" not in line))
        self.assertIn("ok:", self.cli("validate"))

    def test_one_id_changed_on_both_sides_is_the_users_call(self) -> None:
        def summary(branch: str, words: str) -> None:
            ops = [{"op": "update_node", "id": "topic:demo", "set": {"summary": words.capitalize() + "."},
                    "reason": "The user described the topic in their own words.",
                    "prov": [{"quote": words, "by": "user"}]}]
            self.answer(branch, "q.gap.thin@topic:demo", "It is %s." % words, ops, "--confirm")

        summary("c", "weekly shares for garden members")
        summary("d", "share planning for the kitchen")
        self.assertEqual(self.merge("c", "d"), ["graph/nodes.jsonl"])
        self.keep_both_sides("graph/nodes.jsonl")
        for args in (["validate", "--fix"], ["validate"]):  # --fix sorts; it never picks a version
            code, out, _err = _support.run_cli(args, repo=self.root)
            self.assertEqual(code, 1, out)
            self.assertIn("P06 id topic:demo", out)
        # the user keeps one version: the other line goes, and the topic validates
        path = os.path.join(self.root, "graph", "nodes.jsonl")
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines(True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("".join(line for line in lines if "share planning for the kitchen" not in line))
        self.assertIn("ok: 1 nodes", self.cli("validate"))
        self.assertIn("Weekly shares for garden members.", self.cli("get", "topic:demo"))


if __name__ == "__main__":
    unittest.main()
