"""Any agent harness (harness spec H1 to H3): the neutral skills in ``.agents/skills``, ``onto agents`` and
``onto setup --agent``.

- H1: the committed ``.agents/skills`` equals the render of ``plugins/general-ontology/skills`` byte for byte, and
  every render is clean: no Claude Code only token, ``name`` and ``description`` only (the name is the folder's, the
  description cut at a sentence end within 500 characters), the neutral ``O`` sentence, links that resolve inside
  ``.agents/skills``, no em dash. The renderer drops ``claude-only`` blocks and refuses what it cannot neutralize.
- H2: ``onto agents list|show|render [--check]``, and the files each harness gets: repo-relative, no absolute path,
  merged into what is there without dropping a key, and never over a file the user changed.
- H3: ``onto setup --agent``: the ``agents`` step writes and commits the files ("Wire the agents for <name>"), the
  paste blocks follow ``Next:``, question 7 is recorded only when the user gave it, Claude Code's plugin and launch
  steps apply only when it is chosen, and doctor's ``agents`` check reads it all back. The command that
  ``.devin/hooks.v1.json`` carries runs in a new topic and prints the SessionStart JSON.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import unittest

from tests import _support
from tests.test_setup import NO_ANSWERS, SPECIAL, SYSTEM_PATH, SetupCase, decisions
from ontokit import agents, doctor, hook, onboard

REPO_ROOT = os.path.dirname(os.path.dirname(_support.PLUGIN_DIR))
SOURCE = os.path.join(_support.PLUGIN_DIR, "skills")
TARGET = os.path.join(REPO_ROOT, ".agents", "skills")
EM_DASH = chr(0x2014)
LINK_RE = re.compile(r"(?:\]\(|`)((?:\.\./)*(?:[\w.-]+/)*references/[\w.-]+\.md)(?:\)|`)")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def committed():
    """``{path under .agents/skills: bytes}`` as they sit in this checkout."""
    out = {}
    for dirpath, _dirs, files in os.walk(TARGET):
        for name in files:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as fh:
                out[os.path.relpath(path, TARGET).replace(os.sep, "/")] = fh.read()
    return out


def meta_of(text):
    pairs, body = agents.split_frontmatter(text)
    return pairs, body


# H1: the committed render ----------------------------------------------------------------------------------------
class RenderGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.render = agents.render_skills(SOURCE)

    def test_the_committed_skills_equal_the_render_byte_for_byte(self):
        have = committed()
        drift = sorted(set(have) ^ set(self.render)) + sorted(k for k in self.render if k in have and
                                                               have[k] != self.render[k])
        self.assertEqual(drift, [], ".agents/skills is out of date; regenerate it: %s" % agents.REGENERATE)
        self.assertEqual(sorted(self.render), sorted(
            "%s/%s" % (n, rel) for n in agents.skill_names(SOURCE) for rel in agents._files_of(os.path.join(SOURCE, n))))

    def test_no_render_holds_a_claude_code_only_token(self):
        for rel, data in sorted(self.render.items()):
            text = data.decode("utf-8")
            for token in ("CLAUDE_PLUGIN_ROOT", "CLAUDE_PROJECT_DIR", "AskUserQuestion", "--plugin-dir"):
                self.assertNotIn(token, text, rel)
            # the slash command; a plugins/general-ontology path is fine
            self.assertNotRegex(text, r"/plugin\b", rel)

    def test_the_frontmatter_is_the_name_and_a_cut_description(self):
        names = agents.skill_names(SOURCE)
        self.assertEqual(len(names), 6)
        for name in names:
            pairs, _body = meta_of(self.render["%s/SKILL.md" % name].decode("utf-8"))
            self.assertEqual([k for k, _v in pairs], ["name", "description"], name)
            meta = dict(pairs)
            self.assertEqual(meta["name"], name)
            self.assertRegex(name, r"^[a-z0-9]+(-[a-z0-9]+)*$")
            description = meta["description"]
            self.assertLessEqual(len(description), 500, name)
            self.assertGreaterEqual(len(description), 150, "%s: the cut keeps the triggers" % name)
            source = dict(agents.split_frontmatter(read(os.path.join(SOURCE, name, "SKILL.md")))[0])["description"]
            self.assertTrue(source.startswith(description), name)
            self.assertTrue(description.endswith((".", "!", "?")), name)
            self.assertTrue(len(source) == len(description) or source[len(description)] == " ", name)

    def test_every_skill_says_what_o_is_and_where_to_run_it(self):
        for name in agents.skill_names(SOURCE):
            text = self.render["%s/SKILL.md" % name].decode("utf-8")
            self.assertEqual(text.count(agents.O_SENTENCE), 1, name)
            self.assertIn("`O` is `python3 plugins/general-ontology/bin/onto`, run from the topic repo root; prefer "
                          "the `onto_*` MCP tools when this harness has them", text)
            self.assertNotIn("${", text, name)

    def test_every_file_says_it_is_generated_and_from_where(self):
        for rel, data in self.render.items():
            text = data.decode("utf-8")
            line = "<!-- Generated by onto agents render from plugins/general-ontology/skills/%s; edit the source, " \
                   "then run python3 plugins/general-ontology/bin/onto agents render -->" % rel
            self.assertIn(line, text, rel)
            if rel.endswith("/SKILL.md"):
                self.assertTrue(text.split("\n---\n", 1)[1].startswith(line), rel)  # right after the frontmatter
            else:
                self.assertTrue(text.startswith(line), rel)

    def test_every_relative_link_resolves_inside_agents_skills(self):
        seen = 0
        root = os.path.realpath(TARGET)
        for rel, data in self.render.items():
            skill = rel.split("/", 1)[0]
            here = os.path.dirname(os.path.join(root, rel))
            for link in LINK_RE.findall(data.decode("utf-8")):
                base = os.path.join(root, skill) if link.startswith("references/") else here
                path = os.path.realpath(os.path.join(base, link))
                self.assertTrue(path.startswith(root + os.sep), (rel, link))
                self.assertIn(os.path.relpath(path, root).replace(os.sep, "/"), self.render, (rel, link))
                seen += 1
        self.assertGreater(seen, 8)

    def test_no_em_dash(self):
        for rel, data in self.render.items():
            self.assertNotIn(EM_DASH, data.decode("utf-8"), rel)

    def test_the_interview_asks_with_numbered_plain_text_options(self):
        text = self.render["onto-interview/SKILL.md"].decode("utf-8")
        flat = " ".join(text.split())
        self.assertIn('When the question has `options`, list them as numbered plain-text options. Always offer "skip", '
                      '"later", "n/a" (not applicable) and "why?".', flat)
        self.assertNotIn("question tool", flat)
        # the Claude Code plugin install is not this harness's business; the README still says how
        self.assertIn("If the user wants the Claude Code plugin installed by hand, the README (Manual setup, step 2) "
                      "says how.", flat)
        self.assertIn("the topic is ready; hand its `Next:` lines over with the plugin step's detail.", flat)

    def test_claude_code_keeps_its_own_skills_and_no_claude_skills_folder_exists(self):
        # Cursor, Devin and VS Code read .claude/skills too: a copy there would load every skill twice
        self.assertFalse(os.path.exists(os.path.join(REPO_ROOT, ".claude", "skills")))
        self.assertIn("allowed-tools", read(os.path.join(SOURCE, "onto", "SKILL.md")))
        self.assertNotIn("allowed-tools", self.render["onto/SKILL.md"].decode("utf-8"))


# H1: the renderer ------------------------------------------------------------------------------------------------
SKILL = """---
name: onto-demo
description: Demo the kit. Use when the user says "demo". It shows a garden.
allowed-tools:
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/bin/onto" *)
---

# Demo

Plan the beds. `O` is `python3 "${CLAUDE_PLUGIN_ROOT}/bin/onto"`; prefer the `onto_*` MCP tools. Follow the rules.

<!-- claude-only -->
In Claude Code, run /plugin install first.
<!-- /claude-only -->

Read `references/notes.md`.
"""


class RendererTest(_support.TempCase):
    def source(self, text=SKILL, name="onto-demo"):
        root = os.path.join(self.tmp, "kit ’s root")
        folder = os.path.join(root, "plugins", "general-ontology", "skills", name)
        os.makedirs(os.path.join(folder, "references"))
        os.makedirs(os.path.join(root, "plugins", "general-ontology", "ontokit"))  # a template checkout's marker
        with open(os.path.join(folder, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write(text)
        with open(os.path.join(folder, "references", "notes.md"), "w", encoding="utf-8") as fh:
            fh.write("# Notes\n\nWater the beds.\n")
        return root

    def test_claude_only_blocks_are_dropped_and_the_rest_neutralized(self):
        root = self.source()
        out = agents.render_skills(os.path.join(root, "plugins", "general-ontology", "skills"))
        self.assertEqual(sorted(out), ["onto-demo/SKILL.md", "onto-demo/references/notes.md"])
        text = out["onto-demo/SKILL.md"].decode("utf-8")
        self.assertTrue(text.startswith("---\nname: onto-demo\ndescription: Demo the kit. Use when the user says "
                                        "\"demo\". It shows a garden.\n---\n<!-- Generated by onto agents render"))
        self.assertIn("Plan the beds. %s. Follow the rules." % agents.O_SENTENCE, text)
        for gone in ("/plugin install", "claude-only", "In Claude Code", "allowed-tools", "${"):
            self.assertNotIn(gone, text)
        self.assertNotIn("\n\n\n", text)
        self.assertIn("Read `references/notes.md`.", text)
        notes = out["onto-demo/references/notes.md"].decode("utf-8")
        self.assertTrue(notes.startswith("<!-- Generated by onto agents render from plugins/general-ontology/skills/"
                                         "onto-demo/references/notes.md;"))
        self.assertTrue(notes.endswith("# Notes\n\nWater the beds.\n"))

    def test_the_render_is_deterministic(self):
        root = self.source()
        skills = os.path.join(root, "plugins", "general-ontology", "skills")
        self.assertEqual(agents.render_skills(skills), agents.render_skills(skills))
        self.assertEqual(agents.render_skills(SOURCE), agents.render_skills(SOURCE))

    def test_markers_must_pair_up_and_stand_alone(self):
        for broken in ("<!-- claude-only -->\nnever closed\n", "<!-- /claude-only -->\n",
                       "<!-- claude-only -->\n<!-- claude-only -->\nx\n<!-- /claude-only -->\n",
                       "inline <!-- claude-only --> marker\n"):
            with self.assertRaises(agents.RenderError, msg=broken):
                agents.drop_claude_only("a\n" + broken)

    def test_a_claude_code_only_line_left_in_a_source_is_refused(self):
        for line in ("Run /plugin marketplace add first.", "Start claude --plugin-dir here.",
                     "Use AskUserQuestion for it.", "Read $CLAUDE_PROJECT_DIR first."):
            root = self.source(SKILL.replace("Read `references/notes.md`.", line))
            with self.assertRaises(agents.RenderError) as ctx:
                agents.render_skills(os.path.join(root, "plugins", "general-ontology", "skills"))
            self.assertIn("claude-only", ctx.exception.message)
            shutil.rmtree(root)

    def test_a_skill_named_unlike_its_folder_is_refused(self):
        root = self.source(SKILL.replace("name: onto-demo", "name: onto-other"))
        with self.assertRaises(agents.RenderError):
            agents.render_skills(os.path.join(root, "plugins", "general-ontology", "skills"))

    def test_the_description_is_cut_at_a_sentence_end(self):
        first = "Grow the garden plan. " * 10  # 220 characters
        text = first + "Use when the user says " + ", ".join('"plot %d"' % i for i in range(60)) + "."
        cut = agents.cut_description(text)
        self.assertEqual(cut, first.strip())
        self.assertEqual(agents.cut_description("Short. Fine."), "Short. Fine.")
        long_one = "word " * 200  # no sentence end at all: the last whole word that fits, with a period
        cut = agents.cut_description(long_one)
        self.assertLessEqual(len(cut), 500)
        self.assertTrue(cut.endswith("word."))

    def test_a_rewrite_never_reaches_past_its_paragraph(self):
        first, last, _instead = agents.REWRITES[-1]
        text = ("%s say how.\n\n## Important section\n\nKeep this text.\n\nLater: %s\n" % (first, last))
        with self.assertRaises(agents.RenderError) as ctx:
            agents.neutral_body(text, "x/SKILL.md")
        self.assertIn("same paragraph", ctx.exception.message)
        # the passage as one paragraph, wrapped over lines, is still rewritten
        whole = "Before.\n\n%s\nthe README\nsays %s\n\n## Next\n\nKeep this.\n" % (first, last)
        out = agents.neutral_body(whole)
        self.assertIn(_instead, out)
        self.assertIn("## Next\n\nKeep this.", out)

    def test_a_quoted_description_stays_one_closed_yaml_string(self):
        sentence = "Grow the \\ garden \"plan\" here. "
        long_desc = json.dumps(sentence * 30)
        text = "---\nname: onto-x\ndescription: %s\n---\nBody.\n" % long_desc
        out = agents.render_file(text, "x/SKILL.md", True)
        line = next(l for l in out.split("\n") if l.startswith("description: "))
        value = json.loads(line[len("description: "):])  # one closed double-quoted string
        self.assertLessEqual(len(value), agents.DESCRIPTION_MAX)
        self.assertTrue(value.endswith('"plan" here.'), value)
        single = "---\nname: onto-x\ndescription: 'It''s short.'\n---\nBody.\n"
        self.assertIn('description: "It\'s short."\n', agents.render_file(single, "x/SKILL.md", True))
        for broken in ('"never closed', "'never closed", ">", "|", '"two" "strings"'):
            with self.assertRaises(agents.RenderError, msg=broken):
                agents.render_file("---\nname: onto-x\ndescription: %s\n---\nBody.\n" % broken, "x/SKILL.md",
                                   True)
        plain = agents.render_file("---\nname: onto-x\ndescription: Plain words.\n---\nBody.\n", "x", True)
        self.assertIn("\ndescription: Plain words.\n", plain)

    def test_render_writes_removes_stale_files_and_keeps_the_users_own_skills(self):
        root = self.source()
        target = os.path.join(root, ".agents", "skills")
        done = agents.write_skills(root)
        self.assertEqual(done["written"], ["onto-demo/SKILL.md", "onto-demo/references/notes.md"])
        self.assertEqual(agents.skills_drift(root)["changed"], [])
        # a stale generated skill, a user's own skill and a hand edit
        os.makedirs(os.path.join(target, "onto-old"))
        with open(os.path.join(target, "onto-old", "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: onto-old\n---\n%s" % agents.header("x"))
        os.makedirs(os.path.join(target, "my-skill"))
        with open(os.path.join(target, "my-skill", "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: my-skill\ndescription: Mine.\n---\nMine.\n")
        with open(os.path.join(target, "onto-demo", "references", "notes.md"), "a", encoding="utf-8") as fh:
            fh.write("edited\n")
        drift = agents.skills_drift(root)
        self.assertEqual((drift["changed"], drift["extra"]), (["onto-demo/references/notes.md"],
                                                              ["onto-old/SKILL.md"]))
        code, out, _err = _support.run_cli(["agents", "render", "--check"], repo=root)
        self.assertEqual(code, 1, out)
        self.assertIn("changed: onto-demo/references/notes.md", out)
        self.assertIn("Regenerate it: %s" % agents.REGENERATE, out)
        code, out, _err = _support.run_cli(["agents", "render"], repo=root)
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(os.path.join(target, "onto-old")))
        self.assertTrue(os.path.isfile(os.path.join(target, "my-skill", "SKILL.md")))
        code, out, _err = _support.run_cli(["agents", "render", "--check"], repo=root)
        self.assertEqual(code, 0, out)


# H2: onto agents and the harness files ---------------------------------------------------------------------------
CURSOR = {"mcpServers": {"onto": {"type": "stdio", "command": "python3",
                                  "args": ["${workspaceFolder}/plugins/general-ontology/bin/onto-mcp"],
                                  "env": {"ONTO_REPO": "${workspaceFolder}"}}}}
COPILOT = {"servers": dict(CURSOR["mcpServers"])}


class AgentsCommandTest(_support.TempCase):
    def test_list_names_every_harness_and_what_it_reads(self):
        code, out, err = _support.run_cli(["agents", "list", "--json"], repo=REPO_ROOT)
        self.assertEqual(code, 0, err)
        obj = json.loads(out)
        self.assertEqual(obj["where"], "template")
        self.assertEqual([a["name"] for a in obj["agents"]],
                         ["claude", "devin", "codex", "cursor", "copilot", "gemini", "generic"])
        by = {a["name"]: a for a in obj["agents"]}
        self.assertEqual(by["devin"]["files"], [".devin/blueprint.yaml", ".devin/hooks.v1.json"])
        self.assertIn("AGENTS.md (the first 16 KiB)", by["devin"]["reads"])
        self.assertEqual(by["devin"]["wired"], "not wired")
        code, out, _err = _support.run_cli(["agents"], repo=REPO_ROOT)
        self.assertEqual(code, 0)
        self.assertIn("Details: onto agents show <name>", out)

    def test_show_prints_the_files_and_the_paste_blocks(self):
        code, out, err = _support.run_cli(["agents", "show", "devin", "--json"])
        self.assertEqual(code, 0, err)
        obj = json.loads(out)
        files = {f["path"]: f["content"] for f in obj["files"]}
        self.assertEqual(sorted(files), [".devin/blueprint.yaml", ".devin/hooks.v1.json"])
        hooks = json.loads(files[".devin/hooks.v1.json"])
        self.assertEqual(hooks, {"SessionStart": [{"matcher": "", "hooks": [{
            "type": "command", "command": "python3 plugins/general-ontology/bin/onto-session-start --format json",
            "timeout": 10}]}]})
        blueprint = files[".devin/blueprint.yaml"]
        for needle in ("initialize:\n", "uses: github.com/actions/setup-python@v5", 'python-version: "3.12"',
                       "maintenance:\n", "run: python3 plugins/general-ontology/bin/onto doctor || true",
                       "knowledge:\n", "onto status` first", "AGENTS.md section 1",
                       "numbered plain-text options", "reviewed proposal"):
            self.assertIn(needle, blueprint)
        titles = [p["title"] for p in obj["paste"]]
        self.assertEqual(titles, ["Devin MCP server", "Devin playbook !onto", "Devin playbook !onto-gaps (optional)"])
        mcp = obj["paste"][0]["text"]
        self.assertIn("Command: python3", mcp)
        self.assertIn("Arguments: <repo>/plugins/general-ontology/bin/onto-mcp, --repo, <repo>", mcp)
        self.assertIn("what pwd prints at the repo root in a Devin session", mcp)
        playbook = obj["paste"][1]["text"]
        for needle in ("## Overview", "## What's Needed From User", "## Procedure", "## Forbidden Actions",
                       "onto status", "@skills:", "Never merge", "invoke line", "shell line",
                       "Never run `onto init`", "neither a topic nor a template checkout"):
            self.assertIn(needle, playbook)
        self.assertIn("onto gaps", obj["paste"][2]["text"])
        self.assertTrue(any("Devin CLI and Devin Local only" in n and "unverified" in n for n in obj["notes"]))
        code, text, _err = _support.run_cli(["agents", "show", "devin"])
        self.assertEqual(code, 0)
        self.assertIn("File .devin/blueprint.yaml:", text)
        self.assertIn("Paste, Devin MCP server:", text)

    def test_show_refuses_an_unknown_agent_and_needs_a_name(self):
        code, _out, err = _support.run_cli(["agents", "show", "nosuch"])
        self.assertEqual(code, 2)
        self.assertIn("unknown agent 'nosuch'", err)
        code, _out, err = _support.run_cli(["agents", "show"])
        self.assertEqual(code, 2)
        self.assertIn("needs a name", err)
        code, _out, err = _support.run_cli(["agents", "list", "devin"])
        self.assertEqual(code, 2)

    def test_every_harness_file_is_repo_relative_and_parses(self):
        home = os.path.expanduser("~")
        for h in agents.HARNESSES:
            shown = agents.show(h["name"])
            for f in shown["files"]:
                content = f["content"]
                self.assertNotIn(home, content, f["path"])
                self.assertNotRegex(content, r"(?<![\w$}])/(Users|home)/", f["path"])
                self.assertNotIn(EM_DASH, content)
                self.assertFalse(f["path"].startswith(("/", "~")), f["path"])
                self.assertFalse(f["path"].startswith(".claude/"), f["path"])
                if f["path"].endswith(".json"):
                    json.loads(content)
                if f["path"].endswith(".toml") and sys.version_info >= (3, 11):
                    import tomllib

                    tomllib.loads(content)
            for block in shown["paste"]:
                self.assertNotIn(home, block["text"])
                self.assertNotIn(EM_DASH, block["text"])
            self.assertNotIn(EM_DASH, " ".join(shown["notes"] + shown["reads"] + [shown["open"]]))

    def test_the_mcp_configs_set_the_repo(self):
        files = {f["path"]: f["content"] for h in agents.HARNESSES for f in agents.show(h["name"])["files"]}
        self.assertEqual(json.loads(files[".cursor/mcp.json"]), CURSOR)
        self.assertEqual(json.loads(files[".vscode/mcp.json"]), COPILOT)
        gemini = json.loads(files[".gemini/settings.json"])
        self.assertEqual(gemini["context"], {"fileName": ["AGENTS.md"]})
        self.assertEqual(gemini["mcpServers"]["onto"], {"command": "python3", "args": [
            "plugins/general-ontology/bin/onto-mcp", "--repo", "."], "cwd": "."})
        codex = files[".codex/config.toml"]
        self.assertTrue(codex.startswith('[mcp_servers.onto]\ncommand = "sh"\nargs = ["-c", '), codex)
        self.assertIn('exec python3 \\"$T/plugins/general-ontology/bin/onto-mcp\\" --repo \\"$T\\"', codex)
        self.assertIn("git rev-parse --show-toplevel", codex)
        hooks = json.loads(files[".codex/hooks.json"])
        group = hooks["hooks"]["SessionStart"][0]
        self.assertEqual(group["matcher"], "startup|resume|clear|compact")
        self.assertEqual(group["hooks"][0]["command"],
                         'python3 "$(git rev-parse --show-toplevel)/plugins/general-ontology/bin/onto-session-start"')
        self.assertNotIn("--format", group["hooks"][0]["command"])  # Codex takes plain stdout
        rule = files[".cursor/rules/onto.mdc"]
        self.assertTrue(rule.startswith("---\n"))
        self.assertIn("\nalwaysApply: true\n", rule)
        self.assertIn("Follow AGENTS.md", rule)
        self.assertIn("onto status", rule)
        notes = " ".join(agents.show("codex")["notes"])
        self.assertIn("trusted project", notes)
        self.assertIn("never edits ~/.codex", notes)
        generic = agents.show("generic")
        self.assertEqual(generic["files"], [])
        self.assertIn("ONTO_REPO=<repo>", generic["paste"][0]["text"])
        for other in ("OpenCode", "Amp", "goose", "Zed", "Jules", "Factory", "Aider"):
            self.assertTrue(any(n.startswith(other + ":") for n in generic["notes"]), other)
        jules = next(n for n in generic["notes"] if n.startswith("Jules:"))
        self.assertIn("custom stdio MCP unverified", jules)  # others.md: only built-in connections are verified
        self.assertIn("Copilot cloud agent MCP", [p["title"] for p in agents.show("copilot")["paste"]])

    def test_a_json_file_keeps_every_other_key(self):
        spec = agents.harness("gemini")["files"][0]
        mine = {"theme": "dark", "context": {"fileName": "GEMINI.md"},
                "mcpServers": {"other": {"command": "x"}}}
        status, data, _why = agents.plan_file(spec, json.dumps(mine).encode())
        self.assertEqual(status, "write")
        doc = json.loads(data)
        self.assertEqual(doc["theme"], "dark")
        self.assertEqual(doc["context"]["fileName"], ["GEMINI.md", "AGENTS.md"])
        self.assertEqual(sorted(doc["mcpServers"]), ["onto", "other"])
        self.assertEqual(agents.plan_file(spec, data)[0], "already")
        hooks = agents.harness("devin")["files"][1]
        theirs = {"SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": "./mine.sh"}]}],
                  "PreToolUse": []}
        status, data, _why = agents.plan_file(hooks, json.dumps(theirs).encode())
        doc = json.loads(data)
        self.assertEqual(status, "write")
        self.assertEqual(doc["PreToolUse"], [])
        self.assertEqual(len(doc["SessionStart"]), 2)
        self.assertEqual(doc["SessionStart"][0]["hooks"][0]["command"], "./mine.sh")

    def test_a_file_the_user_changed_is_refused_unless_setup_wrote_it(self):
        cursor = agents.harness("cursor")["files"]
        changed = json.loads(json.dumps(CURSOR))
        changed["mcpServers"]["onto"]["command"] = "python3.12"
        data = json.dumps(changed).encode()
        self.assertEqual(agents.plan_file(cursor[0], data)[0], "refused")
        self.assertEqual(agents.plan_file(cursor[0], data, own=True)[0], "write")  # setup's own earlier bytes
        self.assertEqual(agents.plan_file(cursor[1], b"my own rule\n")[0], "refused")
        self.assertEqual(agents.plan_file(cursor[1], b"older kit rule\n", own=True)[0], "write")
        self.assertEqual(agents.plan_file(cursor[0], b"{not json")[0], "refused")
        self.assertEqual(agents.plan_file(cursor[0], b"[]")[0], "refused")
        devin_hooks = agents.harness("devin")["files"][1]
        old = {"SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command":
                                                           "python3 plugins/general-ontology/bin/onto-session-start"}]}]}
        self.assertEqual(agents.plan_file(devin_hooks, json.dumps(old).encode())[0], "refused")
        status, data, _why = agents.plan_file(devin_hooks, json.dumps(old).encode(), own=True)
        self.assertEqual(len(json.loads(data)["SessionStart"]), 1)  # replaced, not doubled

    def test_a_toml_file_keeps_the_users_tables(self):
        spec = agents.harness("codex")["files"][0]
        mine = b'model = "o4"\n\n[mcp_servers.other]\ncommand = "x"\n'
        status, data, _why = agents.plan_file(spec, mine)
        self.assertEqual(status, "write")
        text = data.decode()
        self.assertTrue(text.startswith(mine.decode() + "\n[mcp_servers.onto]\n"), text)
        self.assertEqual(agents.plan_file(spec, data)[0], "already")
        edited = text.replace('command = "sh"', 'command = "bash"')
        self.assertEqual(agents.plan_file(spec, edited.encode())[0], "refused")
        status, again, _why = agents.plan_file(spec, edited.encode(), own=True)
        self.assertEqual((status, again.decode()), ("write", text))
        if sys.version_info >= (3, 11):
            import tomllib

            self.assertEqual(sorted(tomllib.loads(text)["mcp_servers"]), ["onto", "other"])

    def test_a_quoted_onto_table_is_the_same_table_never_a_second_one(self):
        spec = agents.harness("codex")["files"][0]
        block = agents.CODEX_BLOCK
        for header in ('[mcp_servers."onto"]', "[mcp_servers.'onto']", '[ "mcp_servers" . "onto" ]'):
            theirs = ('%s\ncommand = "x"\n' % header).encode()
            status, data, why = agents.plan_file(spec, theirs)
            self.assertEqual((status, data), ("refused", None), header)
            self.assertIn("[mcp_servers.onto] table differs", why)
            same = block.replace("[mcp_servers.onto]", header).encode()
            self.assertEqual(agents.plan_file(spec, same)[0], "already", header)
        for other in ('["mcp_servers"]\nonto = { command = "x" }\n', '"mcp_servers".onto.command = "x"\n'):
            status, _data, why = agents.plan_file(spec, other.encode())
            self.assertEqual(status, "refused", other)
            self.assertIn("sets mcp_servers another way", why)


# H3: onto setup --agent ------------------------------------------------------------------------------------------
class SetupAgentsTest(SetupCase):
    def new(self, *extra, name="garden"):
        target = os.path.join(self.place, name)
        code, obj, err = self.setup("--yes", "--launch", "none", "--new", target, *extra)
        return target, code, obj, err

    def test_a_bare_rerun_keeps_the_agents_a_scripted_run_wired(self):
        # a scripted --agent run records no decision; the wired files are what keeps its choice on the next run
        target, code, obj, err = self.new("--agent", "devin")
        self.assertEqual(code, 0, (obj, err))
        self.assertNotIn(onboard.Q_AGENTS, {d["question"] for d in decisions(target)})
        for where in (None, target):  # rerun from the kit checkout with --new, and from inside the topic
            args = ("--yes", "--launch", "none") + (("--new", target) if where is None else ())
            code, obj, err = self.setup(*args, cwd=where)
            self.assertEqual(code, 0, (obj, err))
            steps = self.steps(obj)
            self.assertEqual(steps["agents"], "already", obj)
            self.assertEqual(steps["plugin"], "skipped", obj)  # Claude Code is still not one of the agents
            self.assertFalse(os.path.exists(os.path.join(target, ".claude")))
            self.assertIn(agents.harness("devin")["open"], obj["next"])
            self.assertFalse(any("Claude Code" in line for line in obj["next"]), obj["next"])
        self.assertEqual(_support.git(target, "status", "--porcelain"), "")

    def test_the_agents_step_writes_commits_and_prints_the_paste_blocks(self):
        target, code, obj, err = self.new("--agent", "devin,cursor", *NO_ANSWERS)
        self.assertEqual(code, 0, (obj, err))
        steps = self.steps(obj)
        self.assertEqual(steps["agents"], "done", obj)
        self.assertEqual(steps["plugin"], "skipped")  # Claude Code was not chosen: no question 7a, no wiring
        self.assertEqual(steps["launch"], "skipped")
        self.assertEqual([s["id"] for s in obj["steps"]], list(onboard.STEPS))
        for rel in (".devin/blueprint.yaml", ".devin/hooks.v1.json", ".cursor/mcp.json", ".cursor/rules/onto.mdc"):
            spec = next(f for h in ("devin", "cursor") for f in agents.harness(h)["files"] if f["path"] == rel)
            with open(os.path.join(target, *rel.split("/")), "rb") as fh:
                self.assertEqual(fh.read(), agents.fresh(spec), rel)
        self.assertFalse(os.path.exists(os.path.join(target, ".claude")))
        self.assertEqual(_support.git(target, "log", "-1", "--format=%s"), "Wire the agents for garden")
        self.assertEqual(sorted(_support.git(target, "show", "--name-only", "--format=", "HEAD").split()),
                         [".cursor/mcp.json", ".cursor/rules/onto.mdc", ".devin/blueprint.yaml",
                          ".devin/hooks.v1.json"])
        self.assertEqual(_support.git(target, "status", "--porcelain"), "")
        # nothing committed names the machine: no absolute path, no home folder
        tracked = _support.git(target, "ls-files").split("\n")
        for rel in tracked:
            if rel.startswith((".devin/", ".cursor/")):
                with open(os.path.join(target, rel), encoding="utf-8") as fh:
                    text = fh.read()
                self.assertNotIn(self.home, text, rel)
                self.assertNotIn(SPECIAL, text, rel)
        self.assertEqual([p["title"] for p in obj["paste"]],
                         ["Devin MCP server", "Devin playbook !onto", "Devin playbook !onto-gaps (optional)"])
        self.assertIn(agents.harness("devin")["open"], obj["next"])
        self.assertIn(agents.harness("cursor")["open"], obj["next"])
        self.assertFalse(any("Claude Code" in line for line in obj["next"]), obj["next"])
        # question 7 came with the answers file: recorded in the user's choice
        recorded = {d["question"]: d for d in decisions(target)}
        self.assertEqual(recorded[onboard.Q_AGENTS]["chosen"], "devin, cursor")
        self.assertEqual(recorded[onboard.Q_AGENTS]["chosen_text"], "Devin, Cursor")
        self.assertNotIn(onboard.Q_PLUGIN, recorded)
        # the text form prints the paste blocks after the Next lines
        code, text, err = self.text_setup("--yes", "--launch", "none", "--new", target)
        self.assertEqual(code, 0, err)
        lines = text.split("\n")
        self.assertLess(lines.index("Next:"), lines.index("Paste, Devin MCP server:"))
        self.assertIn("    Command: python3", lines)
        self.assertIn("already  agents", text)

    def text_setup(self, *args):
        old = os.getcwd()
        os.chdir(self.template_root)
        try:
            return _support.run_cli(["setup"] + list(args))
        finally:
            os.chdir(old)

    def test_a_second_run_keeps_the_recorded_agents_and_commits_nothing(self):
        target, code, obj, err = self.new("--agent", "codex,gemini,copilot", *NO_ANSWERS)
        self.assertEqual(code, 0, (obj, err))
        head = _support.git(target, "rev-parse", "HEAD")
        code, obj, err = self.setup("--yes", "--launch", "none", cwd=target)
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(self.steps(obj)["agents"], "already", obj)
        self.assertEqual(_support.git(target, "rev-parse", "HEAD"), head)
        self.assertIn(agents.harness("gemini")["open"], obj["next"])
        self.assertEqual(json.loads(_support.git(target, "show", "HEAD:.vscode/mcp.json")), COPILOT)

    def test_scripted_agents_without_an_answers_file_record_no_decision(self):
        target, code, obj, err = self.new("--agent", "generic", "--plugin", "skip")
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(decisions(target), [])
        self.assertEqual(self.steps(obj)["agents"], "already")  # generic: no files, only the paste block
        self.assertEqual([p["agent"] for p in obj["paste"]], ["generic"])

    def test_claude_alone_is_the_default_and_its_step_is_the_plugin_step(self):
        target, code, obj, err = self.new("--plugin", "skip", *NO_ANSWERS)
        self.assertEqual(code, 0, (obj, err))
        agents_step = next(s for s in obj["steps"] if s["id"] == "agents")
        self.assertEqual((agents_step["status"], agents_step["detail"]),
                         ("skipped", "Claude Code only: the plugin step wires it"))
        self.assertNotIn("paste", obj)
        self.assertNotIn(onboard.Q_AGENTS, [d["question"] for d in decisions(target)])
        self.assertIn("Codex or another agent: open the folder and ask the agent to follow AGENTS.md", obj["next"])

    def test_an_unknown_agent_is_refused_before_anything_is_written(self):
        for args in (("--agent", "devin,nosuch"),
                     ("--answers", json.dumps({"setup": {"agent": ["robot"]}})),
                     ("--answers", json.dumps({"setup": {"agent": "devin"}})),
                     ("--answers", json.dumps({"decisions": [{"question": onboard.Q_AGENTS, "chosen": "devin"}]}))):
            target, code, obj, _err = self.new(*args)
            self.assertNotEqual(code, 0, args)
            self.assertFalse(os.path.exists(target), args)
        self.assertIn("unknown agent nosuch", self.new("--agent", "nosuch")[2]["message"])

    def test_the_answers_file_gives_the_agents_and_a_skip_records_question_7(self):
        answers = os.path.join(self.tmp, "setup.json")
        with open(answers, "w", encoding="utf-8") as fh:
            json.dump({"setup": {"agent": ["cursor", "claude"], "plugin": "skip"}}, fh)
        target, code, obj, err = self.new("--answers", "@" + answers)
        self.assertEqual(code, 0, (obj, err))
        recorded = {d["question"]: d for d in decisions(target)}
        self.assertEqual(recorded[onboard.Q_AGENTS]["chosen"], "claude, cursor")
        self.assertIn(agents.harness("cursor")["open"], obj["next"])
        self.assertTrue(os.path.isfile(os.path.join(target, ".cursor", "mcp.json")))
        with open(answers, "w", encoding="utf-8") as fh:
            json.dump({"setup": {"skipped": [7]}}, fh)
        target, code, obj, err = self.new("--answers", "@" + answers, name="kitchen")
        self.assertEqual(code, 0, (obj, err))
        recorded = {d["question"]: d for d in decisions(target)}
        self.assertEqual(recorded[onboard.Q_AGENTS]["chosen"], onboard.SKIPPED)
        self.assertEqual(recorded[onboard.Q_PLUGIN]["chosen"], onboard.SKIPPED)

    def test_a_wired_file_the_user_changed_is_left_alone(self):
        target, code, obj, err = self.new("--agent", "cursor", *NO_ANSWERS)
        self.assertEqual(code, 0, (obj, err))
        path = os.path.join(target, ".cursor", "mcp.json")
        doc = json.loads(read(path))
        doc["mcpServers"]["onto"]["command"] = "python3.12"
        doc["mcpServers"]["mine"] = {"command": "x"}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        _support.git(target, "commit", "-q", "-am", "my server")
        before = read(path)
        for _run in range(2):  # every later run keeps it too, and still exits 0
            code, obj, err = self.setup("--yes", "--launch", "none", "--agent", "cursor", cwd=target)
            self.assertEqual(code, 0, (obj, err))
            step = next(s for s in obj["steps"] if s["id"] == "agents")
            self.assertEqual(step["status"], "skipped", step)
            self.assertIn("kept the user's version of .cursor/mcp.json (mcpServers.onto holds another entry)",
                          step["detail"])
            self.assertIn("onto agents show <name>", step["detail"])
            self.assertEqual(read(path), before)
        checks = {c["id"]: c for c in doctor.run_checks(target, self.env)["checks"]}
        self.assertEqual(checks["agents"]["status"], "warn", checks["agents"])
        self.assertIn("cursor: .cursor/mcp.json differs from its render", checks["agents"]["detail"])
        # the fix names the repair that works: setup would keep the file again
        self.assertIn("merge the kit's entries into .cursor/mcp.json by hand (onto agents show cursor prints them)",
                      checks["agents"]["fix"])
        self.assertNotIn("onto setup --agent cursor (", checks["agents"]["fix"])

    def test_devin_without_a_remote_is_told_to_push_the_topic_first(self):
        """Regression: Devin clones the topic from a git host, yet setup told the user to add a repo that no git host
        held (only the kit remote existed). With no origin, the Devin Next line says to create one and push."""
        target, code, obj, err = self.new("--agent", "devin", *NO_ANSWERS)
        self.assertEqual(code, 0, (obj, err))
        quoted = doctor.shell_quote(target)
        push = agents.harness("devin")["needs_origin"] % (quoted, quoted)
        self.assertIn("create a private repo there", push)
        self.assertIn("remote add origin <url> && git -C %s push -u origin main" % quoted, push)
        self.assertIn(push, obj["next"])
        self.assertLess(obj["next"].index(push), obj["next"].index(agents.harness("devin")["open"]))
        self.assertEqual(sorted(_support.git(target, "remote").split()), ["kit"])
        # with --origin the topic has a remote: no push line
        target, code, obj, err = self.new("--agent", "devin", "--origin", "https://git.example.com/me/kitchen.git",
                                          *NO_ANSWERS, name="kitchen")
        self.assertEqual(code, 0, (obj, err))
        self.assertFalse(any("has no remote yet" in line for line in obj["next"]), obj["next"])
        self.assertIn(agents.harness("devin")["open"], obj["next"])
        # an agent opened on this machine needs no remote
        target, code, obj, err = self.new("--agent", "cursor", *NO_ANSWERS, name="shed")
        self.assertFalse(any("has no remote yet" in line for line in obj["next"]), obj["next"])

    def test_doctor_does_not_ask_for_the_claude_plugin_in_a_topic_for_other_agents(self):
        """Regression: in a Devin-only clone (origin only, no kit remote) doctor warned that the plugin was not wired
        and told the agent to run onto setup --plugin project, against the recorded agents decision."""
        target, code, obj, err = self.new("--agent", "devin", *NO_ANSWERS)
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(doctor.topic_agents(target), (["devin"], "decision"))
        clone = os.path.join(self.tmp, "vm", "garden")
        os.makedirs(os.path.dirname(clone))
        _support.git(self.tmp, "clone", "-q", target, clone)
        self.assertEqual(_support.git(clone, "remote").split(), ["origin"])
        checks = {c["id"]: c for c in doctor.run_checks(clone, self.env)["checks"]}
        self.assertEqual(checks["plugin"]["status"], "ok", checks["plugin"])
        self.assertEqual(checks["plugin"]["detail"], "not wired: Claude Code is not one of the agents (the recorded "
                                                     "setup decision: devin)")
        self.assertNotIn("--plugin project", checks["plugin"].get("fix") or "")
        # no decision (a scripted setup): the wired harness files say the same
        target, code, obj, err = self.new("--agent", "devin,codex", "--plugin", "skip", name="kitchen")
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(decisions(target), [])
        clone = os.path.join(self.tmp, "vm", "kitchen")
        _support.git(self.tmp, "clone", "-q", target, clone)
        self.assertEqual(doctor.topic_agents(clone), (["devin", "codex"], "wired"))
        checks = {c["id"]: c for c in doctor.run_checks(clone, self.env)["checks"]}
        self.assertEqual((checks["plugin"]["status"], checks["plugin"]["detail"]),
                         ("ok", "not wired: Claude Code is not one of the agents (wired: devin, codex)"))
        # a recorded choice that includes Claude Code still warns in a clone with no wiring
        target, code, obj, err = self.new("--agent", "claude,devin", "--plugin", "plugin-dir", *NO_ANSWERS,
                                          name="shed")
        self.assertEqual(code, 0, (obj, err))
        clone = os.path.join(self.tmp, "vm", "shed")
        _support.git(self.tmp, "clone", "-q", target, clone)
        self.assertEqual(doctor.topic_agents(clone)[0], ["claude", "devin"])

    def test_the_devin_hooks_command_runs_in_the_topic_and_prints_its_json(self):
        # the command .devin/hooks.v1.json carries, run as a shell would run it from the topic root, with a python3
        # on PATH that is this interpreter: one line of JSON whose additionalContext is the hook's lines
        target, code, obj, err = self.new("--agent", "devin", *NO_ANSWERS)
        self.assertEqual(code, 0, (obj, err))
        hooks = json.loads(read(os.path.join(target, ".devin", "hooks.v1.json")))
        commands = [h["command"] for group in hooks["SessionStart"] for h in group["hooks"]]
        self.assertEqual(len(commands), 1, hooks)
        self.assertIn("--format json", commands[0])
        shim = os.path.join(self.tmp, "shim")
        os.makedirs(shim)
        os.symlink(sys.executable, os.path.join(shim, "python3"))
        env = {k: v for k, v in self.env.items() if not k.startswith("ONTO_") and k != "CLAUDE_PROJECT_DIR"}
        env["PATH"] = os.pathsep.join([shim, SYSTEM_PATH])

        def run(command):
            proc = subprocess.run(["/bin/sh", "-c", command], cwd=target, env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            self.assertEqual((proc.returncode, proc.stderr), (0, b""), proc)
            return proc.stdout.decode("utf-8")

        out = run(commands[0])
        self.assertEqual(out.count("\n"), 1, out)
        payload = json.loads(out)
        self.assertEqual(list(payload), ["hookSpecificOutput"])
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
        context = payload["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(context.split("\n")[0].startswith("garden "), context)
        self.assertTrue(context.split("\n")[-1].startswith("Next: use the onto"), context)
        self.assertLessEqual(len(context.split("\n")), hook.MAX_LINES)
        # the same lines the text form prints
        self.assertEqual(run(commands[0].replace(" --format json", "")), context + "\n")

    def test_a_changed_text_file_is_kept_and_the_rest_still_wired(self):
        target, code, obj, err = self.new("--agent", "devin", *NO_ANSWERS)
        self.assertEqual(code, 0, (obj, err))
        blueprint = os.path.join(target, ".devin", "blueprint.yaml")
        with open(blueprint, "a", encoding="utf-8") as fh:
            fh.write("# mine\n")
        os.remove(os.path.join(target, ".devin", "hooks.v1.json"))
        _support.git(target, "commit", "-q", "-am", "mine")
        code, obj, err = self.setup("--yes", "--launch", "none", "--agent", "devin", cwd=target)
        self.assertEqual(code, 0, (obj, err))
        step = next(s for s in obj["steps"] if s["id"] == "agents")
        self.assertEqual(step["status"], "done", step)
        self.assertIn("wrote .devin/hooks.v1.json", step["detail"])
        self.assertIn("kept the user's version of .devin/blueprint.yaml (it differs from the kit's)", step["detail"])
        self.assertTrue(read(blueprint).endswith("# mine\n"))

    def test_a_file_setup_wrote_and_did_not_commit_is_committed_by_the_next_run(self):
        target, code, obj, err = self.new("--agent", "devin", *NO_ANSWERS)
        self.assertEqual(code, 0, (obj, err))
        _support.git(target, "reset", "-q", "--soft", "HEAD~1")  # as if the commit had failed
        _support.git(target, "reset", "-q")
        self.assertIn(".devin/", _support.git(target, "status", "--porcelain"))
        code, obj, err = self.setup("--yes", "--launch", "none", cwd=target)
        self.assertEqual(code, 0, (obj, err))
        self.assertEqual(self.steps(obj)["commit"], "already", obj)
        self.assertEqual(self.steps(obj)["agents"], "done", obj)
        self.assertEqual(_support.git(target, "log", "-1", "--format=%s"), "Wire the agents for garden")
        self.assertEqual(_support.git(target, "status", "--porcelain"), "")

    def test_doctor_reads_the_wiring_back(self):
        target, code, obj, err = self.new("--agent", "devin,codex,cursor,copilot,gemini", *NO_ANSWERS)
        self.assertEqual(code, 0, (obj, err))
        check = agents.skills_check(target)
        self.assertEqual(check["status"], "ok", check)
        self.assertIn("wired: devin, codex, cursor, copilot, gemini", check["detail"])
        os.remove(os.path.join(target, ".codex", "hooks.json"))
        check = agents.skills_check(target)
        self.assertEqual(check["status"], "warn", check)
        self.assertIn("codex: .codex/hooks.json differs from its render", check["detail"])
        self.assertIn("onto setup --agent codex", check["fix"])


class CloudAgentDocsTest(unittest.TestCase):
    """Regression: a cloud agent's machine is discarded after the session, yet nothing told it so. The setup
    interview made a topic on that machine with no remote, and "never push unless asked" lost every commit; the
    playbook's "open a PR" contradicted it. Every place a cloud agent reads now says what to do."""

    def flat(self, *parts):
        return " ".join(read(os.path.join(REPO_ROOT, *parts)).split())

    def test_agents_md_points_a_cloud_agent_at_the_rules_in_its_top_block(self):
        raw = read(os.path.join(REPO_ROOT, "AGENTS.md"))
        top = " ".join(raw.encode("utf-8")[:2048].decode("utf-8", "ignore").split())
        self.assertIn("A cloud agent's machine goes with the session, unpushed work too", top)
        self.assertIn("Read `plugins/general-ontology/docs/cloud-agents.md` before setup or \"stop\" there", top)
        self.assertIn("(a yes to a cloud agent's offer is asking)", " ".join(raw.split()))

    def test_the_cloud_doc_forbids_a_lost_topic_and_offers_the_push(self):
        doc = self.flat("plugins", "general-ontology", "docs", "cloud-agents.md")
        for needle in ("never run `onto setup --new` on the session machine without a remote",
                       "The user runs `./new-topic`", "Pass `--origin <url>`", "git -C <folder> push -u origin main",
                       "A yes is the user asking for the push", "git push -u origin HEAD:refs/heads/onto/<yyyy-mm-dd>",
                       "Never push to the default branch, never force-push and never merge",
                       "the next session starts from the default branch", 'git branch -r --list "origin/onto/*"'):
            self.assertIn(needle, doc)
        setup = self.flat("plugins", "general-ontology", "docs", "setup-interview.md")
        self.assertIn("**In a cloud agent**", setup)
        self.assertIn("Never create a topic with `--new` on that machine without a remote", setup)
        self.assertIn("In a cloud agent only the remote keeps the topic", setup)

    def test_the_interview_skill_and_its_render_carry_the_cloud_rules(self):
        for root in (SOURCE, TARGET):
            text = " ".join(read(os.path.join(root, "onto-interview", "SKILL.md")).split())
            self.assertIn("**In a cloud agent** (a machine discarded after the session, such as cloud Devin)", text)
            self.assertIn("Never run `--new` there without a remote", text)
            self.assertIn("offer to push a session branch and open a pull request", text)
            self.assertIn("A yes to that offer is the user asking.", text)

    def test_the_devin_playbook_and_blueprint_agree_with_agents_md_on_the_push(self):
        playbook = " ".join(agents.DEVIN_PLAYBOOK.split())
        self.assertIn("offer to push a new onto/<date> branch and open a PR with the proposals; a yes is the user "
                      "asking for the push", playbook)
        self.assertIn("the next session starts from the default branch: merge the PR first", playbook)
        self.assertIn("origin/onto/* session branch, ask whether to continue from it", playbook)
        self.assertIn("Never merge a PR, push to the default branch or force-push.", playbook)
        self.assertIn("never `onto setup --new` without a remote to push the new topic to", playbook)
        self.assertNotIn("then open a PR with the proposals.", playbook)  # the PR no longer comes unasked
        blueprint = " ".join(agents.BLUEPRINT.split())
        self.assertIn("This machine is discarded after the session, with every unpushed commit: at stop, offer to "
                      "push a session branch and open a PR", blueprint)
        readme = self.flat("README.md")
        self.assertIn(" ".join(agents.DEVIN_PLAYBOOK.split()), readme)
        self.assertIn("a folder made on the session machine is lost when the session ends", readme)


    def test_the_devin_playbook_allows_the_setup_run_a_fresh_topic_needs(self):
        """Regression: the playbook forbade `onto setup` "outside a template clone", yet a fresh topic is not a
        template clone and AGENTS.md section 1 and fresh-topic.md tell the agent to run setup there for questions
        2 and 4 to 7. The forbidden action now names only the "neither" case and allows the documented command."""
        playbook = " ".join(agents.DEVIN_PLAYBOOK.split())
        forbidden = playbook.split("## Forbidden Actions", 1)[1]
        self.assertNotIn("outside a template clone", playbook)
        command = "onto setup --answers @.onto/setup.json --launch none"
        self.assertIn("In a topic, never run setup except as AGENTS.md section 1 says: `%s` with the flags the "
                      "answers map to, and no --new or --here." % command, forbidden)
        self.assertIn("Never run `onto setup` where `onto doctor` says neither a topic nor a template checkout",
                      forbidden)
        self.assertIn(command, self.flat("plugins", "general-ontology", "docs", "fresh-topic.md"))
        agents_md = self.flat("AGENTS.md")
        self.assertIn("| Fresh topic |", agents_md)
        self.assertIn("never run `init` or `setup` here", agents_md)  # the "neither" case the playbook names
        doctor_src = " ".join(read(os.path.join(_support.PLUGIN_DIR, "ontokit", "doctor.py")).split())
        self.assertIn("neither a topic nor a template checkout", doctor_src)  # the words doctor prints there


class DoctorAgentsTest(_support.TempCase):
    def test_the_template_checkout_matches_its_render(self):
        check = agents.skills_check(REPO_ROOT)
        self.assertEqual(check["status"], "ok", check)
        self.assertIn(".agents/skills matches the render of kit", check["detail"])

    def test_a_drifted_copy_warns_with_the_regenerate_command(self):
        root = os.path.join(self.tmp, "kit")
        shutil.copytree(SOURCE, os.path.join(root, "plugins", "general-ontology", "skills"))
        shutil.copytree(TARGET, os.path.join(root, ".agents", "skills"))
        self.assertEqual(agents.skills_check(root)["status"], "ok")
        with open(os.path.join(root, ".agents", "skills", "onto", "SKILL.md"), "a", encoding="utf-8") as fh:
            fh.write("drift\n")
        check = agents.skills_check(root)
        self.assertEqual(check["status"], "warn")
        self.assertIn("onto/SKILL.md", check["detail"])
        self.assertIn(agents.REGENERATE, check["fix"])


if __name__ == "__main__":
    unittest.main()
