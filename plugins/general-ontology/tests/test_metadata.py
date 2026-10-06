"""Plugin metadata moves with the code (Claude Code caches a plugin by its version), and the skills, hook and docs
agree with the kit: ``__version__`` = ``plugin.json`` = the marketplace entry = the marketplace metadata; six skills
named ``onto`` and ``onto-*`` after their folders, counted in the plugin description; every reference a skill names
exists; the hook names a script that exists; the kit README's commands block lists exactly the registry; and the
commands and JSON the docs show are ones the kit accepts (releases with notes, object files under ``.onto/``,
locations in the kit's grammar)."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import tempfile
import unittest
from typing import Dict, List, Tuple

from tests import _support
from ontokit import __version__, commands

PLUGIN_DIR = _support.PLUGIN_DIR
REPO_ROOT = os.path.dirname(os.path.dirname(PLUGIN_DIR))
SKILLS_DIR = os.path.join(PLUGIN_DIR, "skills")
DOCS_DIR = os.path.join(PLUGIN_DIR, "docs")
# H5: the long material AGENTS.md links to, one kit-owned doc each
KIT_DOCS = ("checks.md", "cloud-agents.md", "decisions-and-log.md", "fresh-topic.md", "interviewing.md", "layout.md",
            "merging.md", "risks-and-controls.md", "safety.md", "setup-interview.md")
AGENTS_LIMIT = 16384  # the bytes an agent that reads only the start of AGENTS.md loads
AGENTS_TARGET = 15000  # headroom under the limit
TOP_BLOCK = 2048  # the block for agents without a session-start hook sits in the first 2 KiB
NAME = "general-ontology"
SKILLS = ("onto", "onto-compose", "onto-deliver", "onto-ingest", "onto-interview", "onto-review")
REFERENCES = {
    "onto": ("reading-rules.md",),
    "onto-interview": ("stages.md", "recap-examples.md"),
    "onto-ingest": ("extraction.md", "refresh.md"),
    "onto-review": ("verdicts.md",),
    "onto-compose": ("stage-c.md",),
    "onto-deliver": ("worked-example.md",),
}
ALLOWED_TOOL = 'Bash(python3 "${CLAUDE_PLUGIN_ROOT}/bin/onto" *)'
WORDS = {3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
CLI_START, CLI_END = "<!-- cli:start -->", "<!-- cli:end -->"
EM_DASH = chr(0x2014)


def read_json(*parts: str):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return json.load(fh)


def read_text(*parts: str) -> str:
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def frontmatter(text: str) -> Tuple[Dict[str, object], str]:
    """The YAML frontmatter of a SKILL.md as ``{key: str or list}`` (the subset skills use: ``key: value`` lines and
    ``- item`` lists), and the body after it."""
    if not text.startswith("---\n"):
        raise AssertionError("SKILL.md must start with a --- frontmatter block")
    head, sep, body = text[4:].partition("\n---\n")
    if not sep:
        raise AssertionError("the frontmatter block is not closed")
    out: Dict[str, object] = {}
    key = None
    for line in head.splitlines():
        if not line.strip():
            continue
        item = re.match(r"^\s+- (.*)$", line)
        if item and key is not None:
            value = out.get(key)
            out[key] = (value if isinstance(value, list) else []) + [item.group(1).strip()]
            continue
        m = re.match(r"^([a-z][a-z-]*):\s*(.*)$", line)
        if not m:
            raise AssertionError("unreadable frontmatter line: %r" % line)
        key, value = m.group(1), m.group(2).strip()
        out[key] = value if value else []
    return out, body


def docs() -> List[str]:
    """The docs this package writes: the front page, the agent manual, CLAUDE.md, the examples README, every skill
    file and every kit doc under ``plugins/general-ontology/docs/`` (H5)."""
    paths = [os.path.join(REPO_ROOT, p) for p in ("README.md", "AGENTS.md", "CLAUDE.md", "examples/README.md")]
    for dirpath, _dirs, files in sorted(os.walk(SKILLS_DIR)):
        paths += [os.path.join(dirpath, f) for f in sorted(files)]
    for dirpath, _dirs, files in sorted(os.walk(DOCS_DIR)):
        paths += [os.path.join(dirpath, f) for f in sorted(files)]
    return paths


def kit_doc(name: str) -> str:
    return read_text(DOCS_DIR, name)


def band(score: int) -> str:
    """The richness band of a score (SPEC WP5: seed <20, sketch <40, working <60, rich <80, deep >=80)."""
    for limit, name in ((20, "seed"), (40, "sketch"), (60, "working"), (80, "rich")):
        if score < limit:
            return name
    return "deep"


def skill_folders() -> List[str]:
    return sorted(d for d in os.listdir(SKILLS_DIR) if os.path.isfile(os.path.join(SKILLS_DIR, d, "SKILL.md")))


class VersionTest(unittest.TestCase):
    def test_versions_move_together(self) -> None:
        plugin = read_json(PLUGIN_DIR, ".claude-plugin", "plugin.json")
        market = read_json(REPO_ROOT, ".claude-plugin", "marketplace.json")
        entry = [p for p in market["plugins"] if p["name"] == NAME]
        self.assertEqual(len(entry), 1)
        self.assertEqual(plugin["name"], NAME)
        self.assertEqual(plugin["version"], __version__)
        self.assertEqual(entry[0]["version"], __version__)
        self.assertEqual(market["metadata"]["version"], __version__)
        self.assertEqual(market["name"], NAME)
        self.assertEqual(entry[0]["source"], "./plugins/%s" % NAME)
        self.assertIn("MCP server", entry[0]["description"])

    def test_the_mcp_config_has_no_env_block(self) -> None:
        self.assertEqual(read_json(PLUGIN_DIR, ".mcp.json"),
                         {"mcpServers": {"onto": {"command": "python3",
                                                  "args": ["${CLAUDE_PLUGIN_ROOT}/bin/onto-mcp"]}}})

    def test_the_session_start_hook_names_an_existing_script(self) -> None:
        hooks = read_json(PLUGIN_DIR, "hooks", "hooks.json")
        commands_seen = []
        for group in hooks["hooks"]["SessionStart"]:
            for hook in group["hooks"]:
                self.assertEqual(hook["type"], "command")
                commands_seen.append(hook["command"])
        self.assertEqual(len(commands_seen), 1)
        m = re.match(r'^python3 "\$\{CLAUDE_PLUGIN_ROOT\}/([^"]+)"$', commands_seen[0])
        self.assertIsNotNone(m, commands_seen[0])
        self.assertTrue(os.path.isfile(os.path.join(PLUGIN_DIR, m.group(1))), m.group(1))
        self.assertNotIn("hooks", read_json(PLUGIN_DIR, ".claude-plugin", "plugin.json"))

    def test_the_launchers_exist(self) -> None:
        for name in ("onto", "onto-mcp", "onto-session-start"):
            path = os.path.join(PLUGIN_DIR, "bin", name)
            self.assertTrue(os.path.isfile(path), name)
            self.assertTrue(os.access(path, os.X_OK), "%s is not executable" % name)


class SkillsTest(unittest.TestCase):
    def test_six_skills_prefixed_onto_and_named_after_their_folders(self) -> None:
        folders = skill_folders()
        self.assertEqual(folders, sorted(SKILLS))
        for name in folders:
            self.assertTrue(name == "onto" or name.startswith("onto-"), name)
            meta, _body = frontmatter(read_text(SKILLS_DIR, name, "SKILL.md"))
            self.assertEqual(meta.get("name"), name)

    def test_the_plugin_description_counts_the_skills(self) -> None:
        description = read_json(PLUGIN_DIR, ".claude-plugin", "plugin.json")["description"]
        self.assertIn("%s skills" % WORDS[len(skill_folders())], description)
        self.assertIn("MCP server", description)

    def test_frontmatter_description_and_allowed_tools(self) -> None:
        for name in skill_folders():
            meta, body = frontmatter(read_text(SKILLS_DIR, name, "SKILL.md"))
            self.assertEqual(sorted(meta), ["allowed-tools", "description", "name"], name)
            description = str(meta["description"])
            self.assertGreaterEqual(len(description), 200, "%s: pack the description with triggers" % name)
            self.assertLessEqual(len(description), 1024, name)
            self.assertIn('"', description, "%s: quote the trigger phrases" % name)
            # a plain YAML scalar: no ": " or " #" inside, and no indicator character first
            self.assertNotIn(": ", description, name)
            self.assertNotIn(" #", description, name)
            self.assertRegex(description, r"^[A-Za-z]", name)
            tools = meta["allowed-tools"]
            self.assertEqual(tools if isinstance(tools, list) else [tools], [ALLOWED_TOOL], name)
            self.assertTrue(body.strip(), name)

    def test_every_reference_is_named_and_exists(self) -> None:
        for name in skill_folders():
            text = read_text(SKILLS_DIR, name, "SKILL.md")
            folder = os.path.join(SKILLS_DIR, name, "references")
            present = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
            self.assertEqual(present, sorted(REFERENCES[name]), name)
            for ref in present:
                self.assertIn("references/%s" % ref, text, "%s never names references/%s" % (name, ref))
                self.assertTrue(read_text(folder, ref).strip(), ref)
            for ref in re.findall(r"(?<!/)references/([\w.-]+\.md)", text):
                self.assertIn(ref, present, "%s names a missing references/%s" % (name, ref))
            for other, ref in re.findall(r"\.\./(onto[\w-]*)/references/([\w.-]+\.md)", text):
                self.assertTrue(os.path.isfile(os.path.join(SKILLS_DIR, other, "references", ref)),
                                "%s names a missing ../%s/references/%s" % (name, other, ref))

    def test_skills_name_only_real_tools_commands_and_skills(self) -> None:
        tools = {c.tool for c in commands.COMMANDS if c.tool}
        names = {c.name for c in commands.COMMANDS} | set(sum((list(c.aliases) for c in commands.COMMANDS), []))
        for dirpath, _dirs, files in os.walk(SKILLS_DIR):
            for f in files:
                text = read_text(dirpath, f)
                for tool in re.findall(r"\bonto_[a-z_]+\b", text):
                    self.assertIn(tool, tools, "%s names %s" % (f, tool))
                for cmd in re.findall(r"`onto ([a-z][a-z-]*)", text):
                    self.assertIn(cmd, names, "%s names onto %s" % (f, cmd))
                for skill in re.findall(r"\bonto(?:-[a-z]+)+\b", text):
                    self.assertIn(skill, SKILLS + ("onto-mcp", "onto-session-start"), "%s names %s" % (f, skill))

    def test_the_router_hands_off_to_every_other_skill(self) -> None:
        text = read_text(SKILLS_DIR, "onto", "SKILL.md")
        for name in SKILLS[1:]:
            self.assertIn(name, text)


class DocsTest(unittest.TestCase):
    def test_the_kit_readme_commands_block_equals_the_registry(self) -> None:
        text = read_text(PLUGIN_DIR, "README.md")
        self.assertEqual(text.count(CLI_START), 1)
        self.assertEqual(text.count(CLI_END), 1)
        block = text.split(CLI_START, 1)[1].split(CLI_END, 1)[0]
        rows = [line for line in block.splitlines() if line.startswith("|") and "`" in line]
        listed = []
        for row in rows:
            first = re.findall(r"`([^`]+)`", row.split("|")[1])
            if not first:
                continue
            word = first[0][len("onto "):] if first[0].startswith("onto ") else first[0]
            listed.append(word.split()[0])
        self.assertEqual(sorted(listed), sorted(c.name for c in commands.COMMANDS))
        self.assertEqual(sorted(set(re.findall(r"\bonto_[a-z_]+\b", block))),
                         sorted(c.tool for c in commands.COMMANDS if c.tool))

    def test_claude_md_imports_agents_md(self) -> None:
        self.assertTrue(read_text(REPO_ROOT, "CLAUDE.md").startswith("@AGENTS.md\n"))

    def test_agents_md_has_the_six_sections(self) -> None:
        heads = re.findall(r"(?m)^## ([1-6])\. ", read_text(REPO_ROOT, "AGENTS.md"))
        self.assertEqual(heads, ["1", "2", "3", "4", "5", "6"])
        titles = re.findall(r"(?m)^## [1-6]\. (.+)$", read_text(REPO_ROOT, "AGENTS.md"))
        self.assertEqual(titles, ["Start here: the interview", "How to interview", "What this repo is",
                                  "How to read it", "How to work here", "Safety and field notes"])

    def test_the_front_page_covers_the_template_flow(self) -> None:
        text = read_text(REPO_ROOT, "README.md")
        for needle in ("git clone -b general-ontology --single-branch", "git remote rename origin kit",
                       "plugins/general-ontology/bin/onto init", "/plugin marketplace add <repo-url>#general-ontology",
                       "/plugin install general-ontology@general-ontology",
                       "git merge --no-commit --no-ff kit/general-ontology",  # round 2: an open merge can be aborted
                       "onto import add", "bash examples/demo.sh"):
            self.assertIn(needle, text)
        for name in SKILLS:
            self.assertIn("`%s`" % name, text)

    def test_docs_use_no_em_dash(self) -> None:
        for path in docs():
            self.assertNotIn(EM_DASH, read_text(path), os.path.relpath(path, REPO_ROOT))

    def test_doc_richness_examples_name_the_right_band(self) -> None:
        pattern = re.compile(r"[Rr]ichness (\d+)(?:\s*->\s*(\d+))?:?\s+\(?(seed|sketch|working|rich|deep)\b")
        seen = 0
        for path in docs():
            for m in pattern.finditer(read_text(path)):
                score = int(m.group(2) or m.group(1))
                self.assertEqual(m.group(3), band(score), "%s: %r" % (os.path.relpath(path, REPO_ROOT), m.group(0)))
                seen += 1
        self.assertGreater(seen, 0)

    def test_doc_version_lines_show_imports_only_as_pins(self) -> None:
        pin = r"[a-z][a-z0-9-]* v\d+ (?:[0-9a-f]{7}|-) (?:ok|mismatch)"
        for path in docs():
            for m in re.finditer(r"imports: ([^|`_\n]*)", read_text(path)):
                part = m.group(1).strip().rstrip(".,)")
                self.assertRegex(part, r"^%s(?:, %s)*$" % (pin, pin),
                                 "%s: the version line prints imports only as pins, and drops the part when there "
                                 "are none: %r" % (os.path.relpath(path, REPO_ROOT), m.group(0)))

    def test_docs_call_a_tool_read_only_or_writing_as_the_registry_does(self) -> None:
        read_only = {c.tool: bool(c.annotations.get("readOnlyHint")) for c in commands.COMMANDS if c.tool}
        checks = 0
        for path in docs():
            text = re.sub(r"\s+", " ", read_text(path))
            where = os.path.relpath(path, REPO_ROOT)
            for count, listed in re.findall(r"(\d+) read tools: ([^.]*)\.", text):
                tools = re.findall(r"onto_[a-z_]+", listed)
                self.assertEqual(len(tools), int(count), where)
                self.assertEqual([t for t in tools if not read_only[t]], [], "%s calls a write tool read" % where)
                checks += 1
            for count, listed in re.findall(r"(\d+) that write \(([^)]*)\)", text):
                tools = re.findall(r"onto_[a-z_]+", listed)
                self.assertEqual(len(tools), int(count), where)
                self.assertEqual([t for t in tools if read_only[t]], [], "%s calls a read-only tool a writer" % where)
                checks += 1
            for listed in re.findall(r"write tools?:? ([^.]*)", text):
                tools = re.findall(r"onto_[a-z_]+", listed)
                self.assertEqual([t for t in tools if read_only[t]], [], "%s calls a read-only tool a writer" % where)
            for listed in re.findall(r"read-only ((?:`onto_[a-z_]+`(?:, | and )?)+)", text):
                tools = re.findall(r"onto_[a-z_]+", listed)
                self.assertEqual([t for t in tools if not read_only[t]], [], "%s calls a write tool read-only" % where)
                checks += 1
        self.assertGreaterEqual(checks, 3)

    def test_documented_releases_carry_notes(self) -> None:
        """``release --write --commit`` without ``--notes`` fails, so no doc shows it that way."""
        seen = 0
        for path in docs() + [os.path.join(PLUGIN_DIR, "README.md")]:
            for span in re.findall(r"`([^`]*\brelease --write --commit\b[^`]*)`", read_text(path)):
                self.assertIn("--notes", span, "%s: %r" % (os.path.relpath(path, REPO_ROOT), span))
                seen += 1
        self.assertGreater(seen, 0)

    def test_documented_object_files_live_in_the_gitignored_onto_folder(self) -> None:
        """An ``@file`` the agent writes at the repo root lands in the next commit and blocks a release commit."""
        seen = 0
        for path in docs() + [os.path.join(PLUGIN_DIR, "README.md")]:
            for name in re.findall(r"@([A-Za-z0-9_./-]+\.json)\b", read_text(path)):
                self.assertTrue(name.startswith(".onto/"), "%s: @%s" % (os.path.relpath(path, REPO_ROOT), name))
                seen += 1
        self.assertGreater(seen, 0)

    def test_doc_locations_fit_the_kit_grammar(self) -> None:
        """Every concrete ``"loc"`` in a doc's JSON is one the kit accepts."""
        def patterns(obj):
            if isinstance(obj, dict):
                for key, value in obj.items():
                    if key == "pattern" and isinstance(value, str) and "Q:" in value:
                        yield value
                    else:
                        yield from patterns(value)
            elif isinstance(obj, list):
                for value in obj:
                    yield from patterns(value)

        found = sorted(set(patterns(read_json(PLUGIN_DIR, "ontokit", "schema", "records.schema.json"))))
        self.assertEqual(len(found), 1, found)
        grammar = re.compile(found[0])
        seen = 0
        for path in docs() + [os.path.join(PLUGIN_DIR, "README.md")]:
            for loc in re.findall(r'"loc": "([^"<]*)"', read_text(path)):
                self.assertRegex(loc, grammar, os.path.relpath(path, REPO_ROOT))
                seen += 1
        self.assertGreater(seen, 5)

    def test_the_ingest_skill_triggers_on_plugging_a_tool_in(self) -> None:
        meta, _body = frontmatter(read_text(SKILLS_DIR, "onto-ingest", "SKILL.md"))
        for phrase in ('"plug in a tool"', '"connect my app"'):
            self.assertIn(phrase, str(meta["description"]))

    def test_the_interview_slug_example_is_a_demo_topic_not_a_person(self) -> None:
        text = read_text(SKILLS_DIR, "onto-interview", "SKILL.md")
        slugs = re.findall(r"topic slug \((?:[^)]*?)for example `([^`]+)`\)", text)
        self.assertEqual(slugs, ["community-garden"])
        self.assertIn("new_topic garden community-garden garden", read_text(REPO_ROOT, "examples", "demo.sh"))
        self.assertNotIn("owner-topic", text)


class AgentsSizeTest(unittest.TestCase):
    """H5: AGENTS.md fits the 16 KiB an agent loads from the start of the file, keeps its six sections there with
    the block for agents without a session-start hook near the top, and links every kit doc it moved out."""

    def agents_bytes(self) -> bytes:
        with open(os.path.join(REPO_ROOT, "AGENTS.md"), "rb") as fh:
            return fh.read()

    def test_agents_md_fits_16_kib_with_headroom(self) -> None:
        size = len(self.agents_bytes())
        self.assertLessEqual(size, AGENTS_LIMIT)
        self.assertLessEqual(size, AGENTS_TARGET, "AGENTS.md is %d bytes: keep it at %d or less for headroom"
                             % (size, AGENTS_TARGET))

    def test_the_six_sections_and_the_top_block_sit_in_the_first_16_kib(self) -> None:
        head = self.agents_bytes()[:AGENTS_LIMIT].decode("utf-8", "ignore")
        self.assertEqual(re.findall(r"(?m)^## ([1-6])\. ", head), ["1", "2", "3", "4", "5", "6"])
        self.assertEqual(len(re.findall(r"(?m)^## \d+\. ", self.agents_bytes().decode("utf-8"))), 6)
        top = head[:TOP_BLOCK]
        self.assertLess(top.index("No session-start hook?"), head.index("## 1. "))
        # Other harnesses have session-start hooks too, so the block names the condition, never one harness.
        self.assertIn("a harness the kit has not wired", flat(top))
        for claim in ("Only Claude Code runs one", "Outside Claude Code no session-start hook runs"):
            self.assertNotIn(claim, flat(head))
        for needle in ("python3 plugins/general-ontology/bin/onto status", "first", "one question per message",
                       "numbered plain-text options", "`.agents/skills/`", "Claude Code: the plugin",
                       "Read `plugins/general-ontology/docs/setup-interview.md` before"):
            self.assertIn(needle, flat(top))

    def test_every_kit_doc_is_linked_with_a_read_it_before_line(self) -> None:
        self.assertEqual(sorted(f for f in os.listdir(DOCS_DIR) if f.endswith(".md")), sorted(KIT_DOCS))
        agents = flat(read_text(REPO_ROOT, "AGENTS.md"))
        for name in KIT_DOCS:
            self.assertIn("Read `plugins/general-ontology/docs/%s` before" % name, agents, name)
            text = kit_doc(name)
            self.assertTrue(text.startswith("# "), name)
            self.assertIn("Read it before", text.split("\n\n", 2)[1], name)

    def test_docs_name_only_real_tools_commands_and_skills(self) -> None:
        """The skills' gate (``test_skills_name_only_real_tools_commands_and_skills``), over the other docs too."""
        tools = {c.tool for c in commands.COMMANDS if c.tool}
        names = {c.name for c in commands.COMMANDS} | set(sum((list(c.aliases) for c in commands.COMMANDS), []))
        # H6: the README quotes the Devin paste blocks as `onto agents show devin` prints them, and a playbook's
        # macro (`!onto-gaps`) is a name the kit prints, not a skill: only the macros the kit itself prints pass
        from ontokit import agents

        macros = set(re.findall(r"!(onto[a-z-]*)", " ".join(b["text"] for b in agents.paste_blocks(agents.NAMES))))
        self.assertIn("onto-gaps", macros)
        checked = 0
        for path in docs():
            if path.startswith(SKILLS_DIR + os.sep):
                continue
            text = read_text(path)
            for macro in re.findall(r"!(onto(?:-[a-z]+)+)\b", text):
                self.assertIn(macro, macros, "%s names the playbook !%s" % (os.path.relpath(path, REPO_ROOT), macro))
            text = re.sub(r"!onto(?:-[a-z]+)+\b", "!", text)
            where = os.path.relpath(path, REPO_ROOT)
            for tool in re.findall(r"\bonto_[a-z_]+\b", text):
                self.assertIn(tool, tools, "%s names %s" % (where, tool))
            for cmd in re.findall(r"`onto ([a-z][a-z-]*)", text):
                self.assertIn(cmd, names, "%s names onto %s" % (where, cmd))
                checked += 1
            for skill in re.findall(r"\bonto(?:-[a-z]+)+\b", text):
                self.assertIn(skill, SKILLS + ("onto-mcp", "onto-session-start"), "%s names %s" % (where, skill))
        self.assertGreater(checked, 50)

    def test_kit_docs_mark_their_claude_code_lines(self) -> None:
        for name in KIT_DOCS:
            for para in re.split(r"\n\s*\n|\n(?=- )", kit_doc(name)):
                if re.search(r"AskUserQuestion|/plugin |CLAUDE_PLUGIN_ROOT", para):
                    self.assertIn("Claude Code:", para, "%s: %s" % (name, para))


class InterviewDocsTest(unittest.TestCase):
    """SPEC B1 and B2: AGENTS.md opens with the interview (a state table, the setup interview, how to ask), CLAUDE.md
    maps it to Claude Code, and the onto-interview skill and the session-start hook say the same thing."""

    def test_agents_md_starts_with_the_state_table_and_the_setup_interview(self) -> None:
        # H5: the state table stays in AGENTS.md, section 1; the seven questions and the hand-off moved with their
        # needles to docs/setup-interview.md, which section 1 links with a "read it before" line
        start = flat(section(read_text(REPO_ROOT, "AGENTS.md"), "1. Start here: the interview"))
        for needle in ("onto status", "onto doctor", "onto setup", "--answers @.onto/setup.json", "welcome back",
                       "onto log --last", "deepen", "one question at a time",
                       "Read `plugins/general-ontology/docs/setup-interview.md` before"):
            self.assertIn(needle, start)
        interview = kit_doc("setup-interview.md")
        for needle in ("onto setup", "--answers @.onto/setup.json", "q.frame.goal", "--personal redact",
                       "--packs assessment", "--plugin project", "~/Ontologies/<name>", "cloud-synced",
                       "one question at a time"):
            self.assertIn(needle, flat(interview))
        self.assertEqual(len(re.findall(r"(?m)^[1-7]\. \"", section(interview, "The questions"))), 7)

    def test_agents_md_says_how_to_interview(self) -> None:
        how = flat(section(read_text(REPO_ROOT, "AGENTS.md"), "2. How to interview"))
        for needle in ("One question per turn", "up to 3", "AskUserQuestion", "numbered plain-text options",
                       '"skip", "later", "n/a" and "why?"', "example answer", '"Noted: ..."',
                       "never a separate OK turn",
                       "Probe once", "Anyone else?", "onto_next about=<id>", "progress line every turn",
                       "Never re-ask", "same turn", "user's words", '"stop"', "onto log --checkpoint"):
            self.assertIn(needle, how)

    def test_agents_md_keeps_the_premise_rule_and_the_field_notes(self) -> None:
        agents = flat(read_text(REPO_ROOT, "AGENTS.md"))
        for needle in ("When a premise changes, never delete", "rests_on", "propose their replacements in the same "
                       "proposal", "onto neighbors <premise>"):
            self.assertIn(needle, agents)
        # H5: the field notes moved to docs/safety.md, which section 6 links
        self.assertIn("Read `plugins/general-ontology/docs/safety.md` before",
                      flat(section(read_text(REPO_ROOT, "AGENTS.md"), "6. Safety and field notes")))
        notes = section(kit_doc("safety.md"), "Field notes")
        kit_notes = section(read_text(PLUGIN_DIR, "README.md"), "Field notes")
        for text in (notes, kit_notes):
            self.assertEqual(len(re.findall(r"(?m)^- ", text)), 12)
            for needle in ("cloud-synced", "byte-stable", "headroom", "untrusted", "named owner"):
                self.assertIn(needle, text)

    def test_claude_md_maps_the_questions_and_names_setup(self) -> None:
        text = flat(read_text(REPO_ROOT, "CLAUDE.md"))
        self.assertTrue(read_text(REPO_ROOT, "CLAUDE.md").startswith("@AGENTS.md\n"))
        for needle in ("AskUserQuestion", "one question per call", '"Other"', "(recommended)",
                       "plugins/general-ontology/bin/onto setup", "--answers @.onto/setup.json",
                       "AGENTS.md, section 1"):
            self.assertIn(needle, text)

    def test_the_interview_skill_runs_setup_and_asks_one_question_per_turn(self) -> None:
        skill = read_text(SKILLS_DIR, "onto-interview", "SKILL.md")
        first = flat(section(skill, "1. No ontology yet"))
        self.assertIn("O doctor", first)
        self.assertIn("O setup ", first)
        self.assertIn("--answers @.onto/setup.json", first)
        self.assertNotIn("git rev-parse", first)
        self.assertNotIn("test -d", first)
        self.assertNotRegex(first, r"(?m)^O init ")
        loop = flat(section(skill, "3. The loop"))
        for needle in ("onto_next n=1", "one question per turn", "AskUserQuestion", "numbered plain-text options",
                       "onto_next about=<id>", "progress line", "Probe once", "in the same turn", "Noted: ...",
                       "Never re-ask"):
            self.assertIn(needle, loop)
        self.assertNotIn("up to 3 plain questions", loop)
        self.assertIn("O log --last", flat(section(skill, "2. Say where things stand")))
        meta, _body = frontmatter(skill)
        self.assertIn("onto setup", str(meta["description"]))
        self.assertIn("one ranked question per turn", str(meta["description"]))
        self.assertNotIn("up to 3", str(meta["description"]))

    def test_the_hook_and_the_skill_agree_on_the_template_step(self) -> None:
        from ontokit import hook

        lines = " ".join(hook.TEMPLATE_LINES)
        self.assertIn("onto-interview", lines)
        self.assertIn("setup questions", lines)
        self.assertIn("onto setup", lines)
        first = flat(section(read_text(SKILLS_DIR, "onto-interview", "SKILL.md"), "1. No ontology yet"))
        self.assertIn("setup interview", first)
        self.assertIn("O setup ", first)
        kit = flat(section(read_text(PLUGIN_DIR, "README.md"), "Session-start hook"))
        for needle in ("onto setup", "onto log --last", "onto doctor"):
            self.assertIn(needle, kit)
        self.assertNotIn("which runs `onto init`", kit)


class KitDocsTest(unittest.TestCase):
    """Kit 0.2.0 docs: the kit README names every setup step and every doctor check the code has, says project scope
    is the default and how private templates authenticate, and AGENTS.md marks its Claude Code only lines."""

    def test_the_kit_readme_names_every_setup_step_and_doctor_check(self) -> None:
        from ontokit import onboard

        kit = read_text(PLUGIN_DIR, "README.md")
        steps = section(kit, "Setup (onto setup)")
        for step in onboard.STEPS:
            self.assertIn("| `%s` |" % step, steps, step)
        doctor = section(kit, "Doctor (onto doctor)")
        ids = sorted(set(re.findall(r'\bcheck\("([a-z_]+)"', read_text(PLUGIN_DIR, "ontokit", "doctor.py"))))
        self.assertGreater(len(ids), 10)
        for cid in ids:
            self.assertIn("`%s`" % cid, doctor, cid)

    def test_the_install_section_defaults_to_project_scope(self) -> None:
        text = flat(section(read_text(PLUGIN_DIR, "README.md"), "Install"))
        for needle in ("Project scope, through `onto setup` (the default)", "--scope project",
                       ".claude/settings.json", "Local scope", "No install", "stored git credentials",
                       "never prompts", "gh auth setup-git"):
            self.assertIn(needle, text)
        self.assertLess(text.index("Project scope"), text.index("Local scope"))

    def test_agents_md_marks_its_claude_code_lines(self) -> None:
        agents = read_text(REPO_ROOT, "AGENTS.md")
        for para in re.split(r"\n\s*\n|\n(?=- )", agents):
            if re.search(r"AskUserQuestion|/plugin |CLAUDE_PLUGIN_ROOT", para):
                self.assertIn("Claude Code:", para, para)
        start = flat(section(agents, "1. Start here: the interview"))
        self.assertIn("Run `onto status` first", start)
        self.assertIn("no session-start hook", start)
        self.assertIn("python3 plugins/general-ontology/bin/onto", flat(agents))

    def test_the_front_page_quick_start_starts_with_one_click(self) -> None:
        quick = section(read_text(REPO_ROOT, "README.md"), "Quick start")
        manual = quick.index("### Manual setup")
        for needle in ("New topic.command", "./new-topic", "new-topic.cmd", "~/Ontologies/<name>",
                       "Start <name>", ".claude/settings.json", "Start the ontology"):
            self.assertLess(quick.index(needle), manual, needle)


def flat(text: str) -> str:
    """``text`` with every run of whitespace collapsed to one space (docs wrap lines at 120 characters)."""
    return re.sub(r"\s+", " ", text)


def section(text: str, heading: str) -> str:
    """The body of the markdown section whose heading line ends with ``heading``, up to the next heading of the same
    or a higher level."""
    m = re.search(r"(?m)^(#+) [^\n]*%s[^\n]*$" % re.escape(heading), text)
    if not m:
        raise AssertionError("no section %r" % heading)
    rest = text[m.end():]
    end = re.search(r"(?m)^#{1,%d} " % len(m.group(1)), rest)
    return rest[:end.start()] if end else rest


class SafetyDocsTest(unittest.TestCase):
    """The skills and the agent manual keep the user in charge where the kit cannot: running a tool's command,
    starting a topic, committing, merging branches and erasing personal data."""

    def test_a_tool_runs_only_when_confirmed_and_after_a_yes(self) -> None:
        refresh = read_text(SKILLS_DIR, "onto-ingest", "references", "refresh.md")
        gated = [refresh, section(read_text(SKILLS_DIR, "onto-ingest", "SKILL.md"), "Plug a tool in"),
                 read_text(REPO_ROOT, "AGENTS.md")]
        for text in gated:
            text = flat(text)
            self.assertRegex(text, r"[Nn]ever run (?:a draft or untrusted tool|a tool whose node or `refresh_with` "
                                   r"link is `\(draft\)` or `\[untrusted\]`)")
            self.assertIn("copied from an ingested source stays untrusted until the user confirms it", text)
            self.assertIn("exact `invoke` line", text)
            self.assertIn("explicit yes", text)
        steps = flat(section(refresh, "Refresh a stale source"))
        run = steps.index("run the tool with your own tools")
        self.assertLess(steps.index("a draft or untrusted, stop here"), run)
        self.assertLess(steps.index("Only after an explicit yes"), run)
        self.assertIn("never in a bulk `all=accept`", flat(read_text(SKILLS_DIR, "onto-review", "SKILL.md")))

    def test_the_documented_tool_confirmation_confirms_a_drafted_tool(self) -> None:
        """The ops in refresh.md, "Confirm a tool with the user", turn a tool drafted from an ingested source into a
        confirmed tool with trust ``user``, and the JSON flags the doc names are the ones the kit prints."""
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="onto-docs-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        root = _support.init_topic(tmp, "farm", "Farm log")

        def cli(*args: str) -> dict:
            code, out, err = _support.run_cli(list(args) + ["--json"], repo=root)
            self.assertEqual(code, 0, out + err)
            return json.loads(out)

        inbox = os.path.join(root, "inbox", "export.md")
        os.makedirs(os.path.dirname(inbox), exist_ok=True)
        with open(inbox, "w", encoding="utf-8") as fh:
            fh.write("# Export\nThe harvest log is refreshed with the harvest sheet export.\n")
        src = cli("ingest", inbox, "--title", "Export note")["source"]["id"]
        quote = [{"src": src, "loc": "L2-L2", "quote": "The harvest log is refreshed with the harvest sheet export",
                  "by": "agent"}]
        prop = cli("propose", "--proposal", json.dumps({"source": src, "summary": "Export tool", "ops": [
            {"op": "add_node", "ref": "$tool", "basis": "stated", "prov": quote,
             "node": {"kind": "tool", "name": "Harvest sheet export",
                      "attrs": {"interface": "cli", "invoke": "sheet-export --tab harvest"}}},
            {"op": "add_node", "ref": "$log", "basis": "stated", "prov": quote,
             "node": {"kind": "dataset", "name": "Harvest log"}},
            {"op": "add_edge", "basis": "stated", "prov": quote,
             "edge": {"src": "$log", "rel": "refresh_with", "dst": "$tool"}}]}))
        cli("apply", prop["proposal"]["id"], "--all", "draft")
        tool = cli("get", "tool:harvest-sheet-export")
        link = tool["relations"]["refreshes"][0]
        self.assertTrue(tool["node"]["draft"] and tool["node"]["untrusted"])
        self.assertTrue(link["link_draft"] and link["link_untrusted"])

        block = section(read_text(SKILLS_DIR, "onto-ingest", "references", "refresh.md"), "Confirm a tool")
        ops = json.loads(re.search(r"```json\n(.*?)```", block, re.S).group(1))
        ops[1]["id"] = link["edge"]
        words = "Yes, we run sheet-export --tab harvest, and it renews the harvest log."
        call = re.search(r"`onto_answer q=(q\.gap\.draft@tool:[a-z-]+) ", flat(block)).group(1)
        self.assertEqual(call, "q.gap.draft@tool:harvest-sheet-export")
        cli("answer", call, words, "--ops", json.dumps(ops), "--apply")
        tool = cli("get", "tool:harvest-sheet-export")
        self.assertEqual((tool["node"]["status"], tool["node"]["trust"]), ("confirmed", "user"))
        link = tool["relations"]["refreshes"][0]
        self.assertFalse(link.get("link_draft") or link.get("link_untrusted"), link)

    def test_the_interview_starts_a_topic_only_in_a_template_clone(self) -> None:
        interview = read_text(SKILLS_DIR, "onto-interview", "SKILL.md")
        first = section(interview, "1. No ontology yet")
        self.assertLess(first.index("plugins/general-ontology/ontokit"), first.index("O init"))
        self.assertIn("do not run `init`", flat(first))
        last = section(interview, "7. When the user stops")
        check = last.index("git check-ignore -q inbox/ && git check-ignore -q .onto/")
        self.assertLess(check, last.index("git add -A"))
        self.assertIn("never run `git add -A`", flat(last))
        router = flat(section(read_text(SKILLS_DIR, "onto", "SKILL.md"), "1. Where things stand"))
        self.assertIn("only in a clone of the template", router)
        self.assertIn("git check-ignore", read_text(REPO_ROOT, "AGENTS.md"))

    def test_merging_topic_branches_is_documented(self) -> None:
        # H5: the steps moved to docs/merging.md; AGENTS.md keeps the heading (the kit's damage hints name it) and
        # links the doc
        self.assertIn("Read `plugins/general-ontology/docs/merging.md` before",
                      flat(section(read_text(REPO_ROOT, "AGENTS.md"), "Merging topic branches")))
        agents = flat(kit_doc("merging.md"))
        for needle in ("git checkout --conflict=diff3", "keep every line of both sides", "onto validate --fix",
                       "P06", "ask the user", "git merge --abort", "git diff --name-only --diff-filter=U",
                       "packs/local.pack.json", "keeps every entry of both sides", "packs/local.questions.jsonl",
                       "P20 duplicate question id"):
            self.assertIn(needle, agents)
        # the abort comes before any onto command: a kit write (validate --fix rewrites the logs) blocks it
        self.assertLess(agents.index("git merge --abort"), agents.index("onto validate --fix"))
        for path in (os.path.join(REPO_ROOT, "AGENTS.md"), os.path.join(SKILLS_DIR, "onto", "SKILL.md"),
                     os.path.join(SKILLS_DIR, "onto", "references", "reading-rules.md")):
            self.assertIn("`packs/local.*`", flat(read_text(path)), path)
        self.assertIn("AGENTS.md", section(read_text(REPO_ROOT, "README.md"), "Merging topic branches"))
        for path in (os.path.join(REPO_ROOT, "AGENTS.md"), os.path.join(SKILLS_DIR, "onto", "SKILL.md"),
                     os.path.join(SKILLS_DIR, "onto", "references", "reading-rules.md")):
            self.assertIn("Merging topic branches", read_text(path), path)

    def test_the_documented_erase_steps_run_and_keep_the_name_out_of_the_decision(self) -> None:
        text = section(read_text(SKILLS_DIR, "onto", "SKILL.md"), "Erase (privacy)")
        meta, _body = frontmatter(read_text(SKILLS_DIR, "onto", "SKILL.md"))
        self.assertIn('"forget this person"', str(meta["description"]))
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="onto-docs-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        root = _support.make_topic(tmp, "mini", "mini")
        node, src = "person:volunteer-lead", "src-e08998852112"
        fill = {"<node>": node, "<src>": src, "<the name>": "volunteer lead"}

        def run(span: str) -> Dict[str, object]:
            for key, value in fill.items():
                span = span.replace(key, value)
            code, out, err = _support.run_cli(shlex.split(span)[1:] + ["--json"], repo=root)
            self.assertEqual(code, 0, span + "\n" + out + err)
            return json.loads(out)

        # step 1's finder reads every place the name sits and writes nothing; it names the ids to erase
        finds = [e for e in re.findall(r"`(O erase [^`]*)`", flat(text)) if e.split()[2] == "--find"]
        self.assertEqual(len(finds), 1, finds)
        found = run(finds[0])["find"]
        self.assertIn(node, found["erase"])
        self.assertIn(src, found["erase"])
        decide = re.findall(r"`(O decide [^`]*)`", flat(text))
        self.assertEqual(len(decide), 1, decide)
        dec = run(decide[0])["decision"]
        fill["<dec>"] = dec["id"]
        self.assertNotRegex(dec["id"], r"volunteer|lead")
        erases = [e for e in re.findall(r"`(O erase [^`]*)`", flat(text)) if e not in finds]
        # round 6: a record that holds the name only in a field is scrubbed, not erased (one documented call)
        scrubs = [e for e in erases if e.split()[2] == "--scrub"]
        self.assertEqual(len(scrubs), 1, scrubs)
        erases = [e for e in erases if e not in scrubs]
        self.assertEqual([e.split()[2] for e in erases], ["<node>", "<src>"])
        for span in erases + scrubs:
            run(span)
        with open(os.path.join(root, "sources", src + ".txt"), encoding="utf-8") as fh:
            self.assertNotIn("volunteer lead", fh.read().lower())
        code, out, err = _support.run_cli(["validate"], repo=root)
        self.assertEqual(code, 0, out + err)


class InstallDocsTest(unittest.TestCase):
    """Claude Code keeps one marketplace per name per user and loads a folder marketplace in place, so adding a topic
    folder (``./``) as the marketplace repoints every topic's skills and hook at it. The docs register the
    marketplace once, from the template's remote, and install at local scope in each topic."""

    INSTALL_TEXTS = (("README.md", "Quick start"), ("plugins/general-ontology/README.md", "Install"),
                     ("CLAUDE.md", None),
                     ("plugins/general-ontology/skills/onto-interview/SKILL.md", "1. No ontology yet"))

    def test_the_install_steps_add_the_marketplace_once_and_install_at_local_scope(self) -> None:
        for rel, heading in self.INSTALL_TEXTS:
            text = read_text(REPO_ROOT, rel)
            text = flat(section(text, heading) if heading else text)
            self.assertIn("/plugin marketplace add <repo-url>#general-ontology", text, rel)
            self.assertIn("/plugin install general-ontology@general-ontology", text, rel)
            self.assertIn("in this repo only (local scope)", text, rel)
            self.assertIn("git remote get-url kit", text, rel)

    def test_no_doc_adds_a_topic_folder_as_the_marketplace(self) -> None:
        seen = 0
        for path in docs() + [os.path.join(PLUGIN_DIR, "README.md")]:
            for para in re.split(r"\n\s*\n", read_text(path)):
                if "marketplace add ./" in para:
                    self.assertRegex(para, r"\b[Nn]ever\b", "%s tells the user to add a topic folder as the "
                                     "marketplace: %r" % (os.path.relpath(path, REPO_ROOT), para))
                    seen += 1
        self.assertGreaterEqual(seen, 4)

    def test_the_kit_readme_names_where_skills_load_from_and_the_other_setups(self) -> None:
        text = flat(section(read_text(PLUGIN_DIR, "README.md"), "Install"))
        for needle in ("--scope local", "claude --plugin-dir ./plugins/general-ontology",
                       "/plugin marketplace remove general-ontology", "/plugin marketplace update general-ontology",
                       "git clone -b general-ontology --single-branch <repo-url> ~/general-ontology-kit",
                       "the skills and `hooks/hooks.json` come from the installed copy"):
            self.assertIn(needle, text)


class HarnessDocsTest(unittest.TestCase):
    """SPEC-harness H3 and H6: every doc that states the setup questions asks question 7 as "Which agents will open
    this topic?" and the plugin question as 7a, only for Claude Code; the front page has "Use it with any agent", with
    one table row per harness and the Devin paste blocks exactly as ``onto agents show devin`` prints them; and the
    kit README has "Harnesses", with the verified facts dated and the open questions."""

    def test_question_7_asks_for_the_agents_and_7a_for_the_plugin(self) -> None:
        from ontokit import onboard

        questions = section(kit_doc("setup-interview.md"), "The questions")
        self.assertIn('7. "%s"' % onboard.Q_AGENTS, questions)
        self.assertIn('7a. "%s" Ask it only when Claude Code is' % onboard.Q_PLUGIN, flat(questions))
        self.assertLess(questions.index('7. "'), questions.index('7a. "'))
        seven = questions[questions.index('7. "'):questions.index('7a. "')]
        for label in ("Claude Code", "Devin", "Codex", "Cursor", "Copilot", "Gemini", "another agent"):
            self.assertIn(label, seven, label)
        skill = flat(section(read_text(SKILLS_DIR, "onto-interview", "SKILL.md"), "1. No ontology yet"))
        self.assertIn("7. Which agents will open this topic?", skill)
        self.assertIn("7a. Only when Claude Code is one of them", skill)
        self.assertIn("--agent claude", skill)
        claude = flat(read_text(REPO_ROOT, "CLAUDE.md"))
        for needle in ("Which agents will open this topic?", "multiSelect: true", "Ask 7a"):
            self.assertIn(needle, claude)
        self.assertIn("7a only when Claude Code is one of the agents",
                      flat(section(read_text(REPO_ROOT, "AGENTS.md"), "The setup interview")))
        self.assertIn("Ask 7a only when Claude Code", flat(kit_doc("fresh-topic.md")))

    def test_the_front_page_has_a_row_per_harness_and_the_devin_paste_blocks(self) -> None:
        from ontokit import agents

        text = read_text(REPO_ROOT, "README.md")
        anyagent = text.split("## Use it with any agent\n", 1)[1].split("\n## Layout\n", 1)[0]
        rows = [re.match(r"^\| [^|]*\(`([a-z]+)`\) \|", line) for line in anyagent.splitlines()]
        self.assertEqual([m.group(1) for m in rows if m], list(agents.NAMES))
        blocks = agents.paste_blocks(["devin"])
        self.assertEqual(len(blocks), 3)
        for block in blocks:
            self.assertIn("```\n%s\n```" % block["text"], anyagent, block["title"])
        for needle in ("@skills:onto-interview", "`!onto`", "Linear", "onto agents show devin", "unverified"):
            self.assertIn(needle, flat(anyagent))

    def test_the_kit_readme_has_the_harness_facts_dated_and_the_open_questions(self) -> None:
        text = flat(section(read_text(PLUGIN_DIR, "README.md"), "Harnesses"))
        self.assertGreaterEqual(text.count("as of 2026-10-06"), 2)
        for needle in ("**Cloud hooks.**", "**MCP host and cwd.**", "**Frontmatter handling.**",
                       "**Python on the default image.**", ".agents/skills/", "16 KiB", "32 KiB", "roots/list",
                       "trusted project", "context.fileName", "${workspaceFolder}", "never writes `.claude/skills/`"):
            self.assertIn(needle, text)


class UpgradeDocsTest(unittest.TestCase):
    """README "Upgrading the kit": after the merge, ``onto migrate`` stamps the new kit (``onto validate`` only warns
    W07), and Claude Code restarts so the MCP server and the skills load the new kit."""

    def test_the_upgrade_steps_migrate_refresh_and_restart(self) -> None:
        text = section(read_text(REPO_ROOT, "README.md"), "Upgrading the kit")
        for needle in ("git fetch kit && git merge --no-commit --no-ff kit/general-ontology", "onto migrate --check",
                       "/plugin marketplace update general-ontology", "restart Claude Code"):
            self.assertIn(needle, flat(text))

    def test_the_documented_onto_steps_bring_an_older_topic_up_to_date(self) -> None:
        from ontokit import store

        text = section(read_text(REPO_ROOT, "README.md"), "Upgrading the kit")
        block = text.split("```", 2)[1]
        prefix = "python3 plugins/general-ontology/bin/onto "
        steps = [line[len(prefix):].split() for line in block.splitlines() if line.startswith(prefix)]
        # round 6: the scan runs before the guarded commit (git add -u), and scans the topic
        self.assertEqual([s[0] for s in steps], ["migrate", "migrate", "validate", "scan"])
        steps[3] = ["scan", "<root>" if steps[3][1:] == ["."] else steps[3][1]]
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="onto-docs-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        root = _support.make_topic(tmp, "mini", "mini")
        path = os.path.join(root, "ontology.json")
        manifest = read_json(path)
        manifest["kit"] = "0.0.1"
        store.write_json(path, manifest)
        code, out, err = _support.run_cli(["validate"], repo=root)
        self.assertIn("W07", out + err)
        outs = []
        for args in steps:
            args = [root if a == "<root>" else a for a in args]
            code, out, err = _support.run_cli(args, repo=root)
            self.assertEqual(code, 0, " ".join(args) + "\n" + out + err)
            outs.append(out)
        self.assertIn("stamp kit %s" % __version__, outs[0])
        self.assertEqual(read_json(path)["kit"], __version__)
        self.assertNotIn("W07", outs[2])
        self.assertIn("ok:", outs[2])


class TranscriptDocsTest(unittest.TestCase):
    """Only a .vtt or .srt file given as a path is stored with T<hh:mm:ss> lines; pasted text keeps its own stamps
    as plain text and is cited by L lines. The ingest skill says so, and says to use the locator the result names."""

    def test_the_ingest_skill_says_which_transcripts_are_cited_by_time(self) -> None:
        skill = read_text(SKILLS_DIR, "onto-ingest", "SKILL.md")
        row = [line for line in skill.splitlines() if line.startswith("| A transcript |")]
        self.assertEqual(len(row), 1)
        for needle in ("`.vtt` or `.srt` file given as `path`", "`cite T<hh:mm:ss>`", "Pasted text", "`L` lines"):
            self.assertIn(needle, row[0])
        self.assertNotIn("keep their", row[0])
        self.assertIn("Use the locator the ingest result line names", flat(section(skill, "4. Draft ops")))
        extraction = flat(read_text(SKILLS_DIR, "onto-ingest", "references", "extraction.md"))
        self.assertIn("any other transcript is cited by `L` lines", extraction)

    def test_the_kit_prints_cite_t_only_for_a_subtitle_file(self) -> None:
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="onto-docs-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        root = _support.bare_topic(tmp, "tr")
        pasted = "[00:00:45] Maya: We ship on November 15.\n[00:01:10] Sam: The box needs a new insert.\n"
        code, out, err = _support.run_cli(["ingest", "--body", pasted, "--kind", "transcript", "--title",
                                           "Pasted sync"], repo=root)
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("cite T<hh:mm:ss>", out)
        os.makedirs(os.path.join(root, "inbox"))
        with open(os.path.join(root, "inbox", "sync.vtt"), "w", encoding="utf-8") as fh:
            fh.write("WEBVTT\n\n00:00:45.000 --> 00:00:50.000\nMaya: We ship on November 15.\n")
        code, out, err = _support.run_cli(["ingest", os.path.join(root, "inbox", "sync.vtt"), "--kind",
                                           "transcript", "--title", "Sync captions"], repo=root)
        self.assertEqual(code, 0, out + err)
        self.assertIn("cite T<hh:mm:ss>", out)


if __name__ == "__main__":
    unittest.main()
