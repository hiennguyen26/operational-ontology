# general-ontology

A template for building an ontology on any topic, one discovery at a time. An agent interviews you, reads what you
feed it, drafts proposals whose quotes a deterministic kit checks, and you review. The ontology grows richer as gaps
close, and two released topics can be composed into a third.

This branch holds **no topic data**. It holds the kit (`plugins/general-ontology/`: the `onto` command line, a stdio
MCP server and six skills), the docs, CI, the `new-topic` launchers and a synthetic demo. `onto setup` (which the
launchers run) makes a topic repo from it.

| Who | Does |
|---|---|
| The kit (standard-library Python 3.9+, no network, no model calls) | Stores, sanitizes, validates, resolves ids, searches, walks, ranks gaps and next questions, checks proposals, applies reviewed changes, composes imports, measures richness, renders, builds and releases |
| The agent (following the skills) | Asks the questions, turns answers and source text into proposal ops, suggests bridges, writes deliverables |
| You | Answer, review, decide, and ask for releases and pushes |

## Quick start

**One click.** In a checkout of the template, double-click `New topic.command` (macOS), run `./new-topic` (any OS
with a shell) or double-click `new-topic.cmd` (Windows). Each one runs `onto setup`.

**From nothing**, clone the template once as your kit checkout and start the first topic from it:

```
git clone -b general-ontology --single-branch <repo-url> ~/general-ontology-kit && cd ~/general-ontology-kit && ./new-topic
```

When it asks "new folder (recommended) or here?", answer `new`: "here" would turn the kit checkout itself into a
topic. Run `./new-topic` there again for each new topic; `git -C ~/general-ontology-kit pull` updates the kit. For a
private template, git uses the credentials it has stored and never prompts (with the GitHub CLI, run
`gh auth setup-git` once); without the network, setup still makes the topic and only the plugin install waits.
Git needs your name and email for the first commit; on a new machine, run
`git config --global user.name "Your Name"` and `git config --global user.email "you@example.com"` first.

- **What it asks.** A new folder or this one, a one-sentence title, a short name and a namespace (both suggested
  from the title), and the folder. It asks once more only when that folder is cloud-synced. `--yes` takes every
  default. Flags set the rest: `--personal keep|redact|refuse`, `--packs assessment`, `--origin <url>`,
  `--agent claude,devin,...` and `--plugin project|local|plugin-dir|skip` (`onto setup --help` lists them all).
- **Where the topic goes.** `~/Ontologies/<name>`: a new git repo on branch `main`, whose `kit` remote points at the
  template. Setup adds your own `origin` only when you pass `--origin`, and never pushes.
- **What it commits and wires.** It commits the topic as "Start <name>", but only once git ignores `inbox/` and
  `.onto/`. Then it wires the plugin for that repo (project scope): `.claude/settings.json`, committed as "Wire the
  general-ontology plugin". Claude Code offers the plugin when you trust the folder. With `--agent`, it also writes
  each other agent's files, committed as "Wire the agents for <name>" ("Use it with any agent" below).
- **How the interview starts.** From a terminal with `claude` on `PATH`, setup starts Claude Code in the new folder
  with "Start the ontology". Otherwise it prints the folder and the next step: open it in Claude Code (terminal
  `claude`, or the desktop app's Code tab) and say "start the ontology". In Codex or another agent, open the folder
  and ask the agent to follow `AGENTS.md`. The agent asks the setup questions setup did not record, then the quick
  start: 5 questions, about 5 minutes.

Every step reports `done`, `already`, `skipped (reason)` or `failed`, so running setup again is safe and does only
what is missing. A failed step names its fix: fix it, then run the same command again. `onto doctor` checks the
machine (git's name and email included), the folder and the wiring, and changes nothing. You can also skip the
terminal: open the template checkout in your agent and say "start the ontology". It runs the setup interview
(`AGENTS.md`, section 1) and then `onto setup` with your answers. In a cloud agent (cloud Devin, the Copilot cloud
agent, a Cursor cloud agent), start the topic on your own machine instead, push it to a private remote and add that
repo to the agent: a folder made on the session machine is lost when the session ends
(`plugins/general-ontology/docs/cloud-agents.md`).

### Manual setup

The same, by hand:

1. **Clone the template** into a repo of your own:

   ```
   git clone -b general-ontology --single-branch <repo-url> my-topic && cd my-topic
   git checkout -b main && git remote rename origin kit
   ```

   Then add your own private remote as `origin`.

2. **Initialize, commit and install the plugin:**

   ```
   python3 plugins/general-ontology/bin/onto init --name my-topic --ns mytopic --title "My topic"
   git add -A && git commit -m "Start my-topic"
   ```

   In Claude Code, add the plugin's marketplace **once per machine**, from the template's remote
   (`git remote get-url kit` prints `<repo-url>`):

   ```
   /plugin marketplace add <repo-url>#general-ontology
   ```

   Then, in each topic repo, run `/plugin install general-ontology@general-ontology` and pick "Install for you, in
   this repo only (local scope)" (from a shell:
   `claude plugin install general-ontology@general-ontology --scope local`). The MCP server and the skills load
   with it. Then start a session in the repo and say "start the ontology".

   Never run `/plugin marketplace add ./` in a topic. Claude Code keeps one `general-ontology` marketplace per user,
   so each topic added that way takes it over: every topic then loads its skills from the last one, and loses them
   when that folder moves. Avoid user scope too: it loads the hook, the MCP server and six skills in every project.
   The kit README, "Install", has the other setups and how to repair an install made from `./`.

   Start a topic only inside a clone of the template: its `.gitignore` keeps the raw `inbox/` out of git, and its
   `.gitattributes` merges the logs. `onto-interview` asks you to clone first when the folder is not one.

### After setup

- **Interview.** Say "start the ontology". The `onto` skill sees a fresh topic and hands off to `onto-interview`:
  a quick start of 5 questions (about 5 minutes), then the main kinds of things in your topic (they become its own
  kinds, after you confirm them), then stages on people, data, vocabulary, processes, constraints
  (premises, and risks when the `assessment` pack is on), deliverables and open questions, then questions driven by
  gaps, with no end. It asks one question per turn, with why it matters and an example answer, and shows a progress
  line every turn.
- **Feed it and grow it.** Paste notes or drop files, transcripts and fetched pages into `inbox/`. `onto-ingest`
  stores each input sanitized and hashed, then drafts proposals whose quotes the kit checks against the stored
  text. `onto-review` applies your verdicts. Richness rises, and its gaps pick the next questions and the next thing
  worth ingesting. `onto-deliver` writes deliverables from one `onto_context` call and proposes what it learned back
  into the graph.
- **Plug tools in** the same way: a spreadsheet export, a script, a tracker or an app's MCP server becomes a `tool`
  node that says how to run it. `onto-ingest` stores what the tool returns and, when that source goes stale, offers
  to run it again: it shows you the exact command and runs it only on your yes. A tool that is still a draft (for
  example one an ingested document described) never runs until you confirm it.
- **Commit at the end of each session.** The agent writes a checkpoint and offers a commit; on your own, run
  `git add -A && git commit -m "<what changed>"`. Commits are your undo, and a release needs them. `inbox/` and
  `.onto/` (the raw drop folder, the kit's lock and the agent's scratch files) are gitignored by the template's
  `.gitignore`.
- **Release and compose.** With the topic data committed, `onto release --write --commit --notes "First release"`
  commits the release files and tags `vN`. Another topic can then run `onto import add` on released topics (see
  Composition below).

To see all of it without your own data, run the demo: `bash examples/demo.sh` (see `examples/README.md`).

## Skills

| Skill | Use it to |
|---|---|
| `onto` | Ask anything about the topic; it starts with one call and hands off |
| `onto-interview` | Set up a new topic (the setup interview, then `onto setup`), start the ontology, answer questions, keep going |
| `onto-ingest` | Add notes, files, transcripts or pages; plug in a tool, a tracker or an app; refresh stale sources |
| `onto-review` | Review pending proposals and apply your verdicts, including premise changes and risk ratings |
| `onto-compose` | Import released topics, pin them, and bridge them |
| `onto-deliver` | Write a brief, plan or report from the ontology |

Without Claude Code, use the command line: `python3 plugins/general-ontology/bin/onto <command>` (`--help` lists the
commands; the kit README has the full table), and ask the agent to follow `AGENTS.md`. It starts with `onto status`
and interviews you the same way; the skills are plain Markdown in `.agents/skills/` ("Use it with any agent"
below). Every output
starts with a version line such as `garden v1 | richness 46 working` (a topic with imports adds their pins, such as
`imports: garden v1 a1b2c3d ok`).

## Use it with any agent

Every agent reads `AGENTS.md`, runs the `onto` command line and can use the stdio MCP server
(`plugins/general-ontology/bin/onto-mcp`). The six skills also come as plain `SKILL.md` files in `.agents/skills/`,
generated from the plugin's skills (`onto agents render`), which Devin, Codex, Cursor, Copilot and Gemini CLI read.
Setup question 7 asks which agents will open the topic; `onto setup --agent claude,devin` wires each one picked,
commits its files and prints the parts set in the agent's own app after `Next:`. `onto agents list` shows what each
agent reads and what this repo has wired, and `onto agents show <name>` prints the files and paste blocks.

| Agent (`--agent`) | Files setup wires | What you do once |
|---|---|---|
| Claude Code (`claude`) | `.claude/settings.json` (the plugin step) | Trust the folder; Claude Code offers the plugin |
| Devin (`devin`) | `.devin/blueprint.yaml`, `.devin/hooks.v1.json` | Add the repo in Devin, paste the MCP server and the `!onto` playbook (below) |
| Codex (`codex`) | `.codex/config.toml`, `.codex/hooks.json` | Trust the project when Codex asks: it reads `.codex/` only in a trusted project |
| Cursor (`cursor`) | `.cursor/mcp.json`, `.cursor/rules/onto.mdc` | Approve the `onto` MCP server |
| Copilot (`copilot`) | `.vscode/mcp.json` | In VS Code, trust the folder and start the MCP server; for the cloud agent, paste the MCP block into the repo's Copilot settings on GitHub |
| Gemini CLI (`gemini`) | `.gemini/settings.json` (it lists `AGENTS.md` in `context.fileName`) | Trust the folder when asked |
| Another agent (`generic`) | none | Point it at `AGENTS.md`, and set up the MCP command `onto agents show generic` prints |

Setup merges into a file that is already there and keeps its other keys, and never overwrites a file you changed.
It never edits your own settings (`~/.codex`, `~/.claude` and the like). Other agents that read `AGENTS.md`
natively (OpenCode, Amp, goose, Zed, Jules, Factory, and Aider through `read:`) work as `generic`.

### Devin

Devin reads the first 16 KiB of `AGENTS.md` (the kit keeps it under that), finds the skills in `.agents/skills/`,
and builds its machine from `.devin/blueprint.yaml` on the default branch. Start a session with `!onto`, or name a
skill: `@skills:onto-interview` runs the interview. Devin has no question tool, so the agent asks numbered
plain-text options in chat, one question per message. `.devin/hooks.v1.json` runs the session-start hook in the
Devin CLI and Devin Local only; whether cloud sessions run it is unverified, so cloud Devin starts from `AGENTS.md`.

Devin clones the topic from your git host, so push it there first (`git remote add origin <url> && git push -u
origin main`); setup's `Next:` line says so when the topic has no remote. A cloud session's machine is discarded
when the session ends, and the next session clones the default branch again. So at "stop" the agent offers to push
the session to an `onto/<date>` branch and open a pull request; merge it before the next session, or ask that
session to continue from the branch (`plugins/general-ontology/docs/cloud-agents.md`).

The MCP server and the playbooks live in Devin's web app, not in the repo. `onto agents show devin` prints them;
paste each once.

The MCP server (optional: the command line works without it):

```
Devin web app: Customize, MCPs, Add custom MCP, transport STDIO. Optional: the CLI works without it. Fill in
the three fields:
Command: python3
Arguments: <repo>/plugins/general-ontology/bin/onto-mcp, --repo, <repo>
Environment Variables: none
<repo> is what pwd prints at the repo root in a Devin session; do not assume a home folder. Check it with Use MCP
in a session.
```

The `!onto` playbook:

```
Devin web app: Settings, Playbooks, Create playbook. Macro: !onto
## Overview
Run or continue the topic ontology interview in this repo with the general-ontology kit.
## What's Needed From User
- Answers to the interview questions, one at a time.
## Procedure
1. From the repo root, run `python3 plugins/general-ontology/bin/onto status`. When git branch -r lists an
   origin/onto/* session branch, ask whether to continue from it (plugins/general-ontology/docs/cloud-agents.md).
2. Follow AGENTS.md section 1 for that state, and use the skill it names with @skills:<name> (for example
   @skills:onto-interview).
3. Ask one question per message, as numbered plain-text options; always offer skip, later, n/a and why? Wait for the reply.
4. Record each answer the same turn, with the onto CLI or the onto_* MCP tools.
5. When the user says stop, write a checkpoint (`onto log --checkpoint`) and offer the commit. This machine is
   discarded after the session, so offer to push a new onto/<date> branch and open a PR with the proposals; a yes
   is the user asking for the push. Say the next session starts from the default branch: merge the PR first.
## Forbidden Actions
- Never merge a PR, push to the default branch or force-push.
- Never run a tool's invoke line without the user's yes for that run.
- Never put the user's words on a shell line; write them to a file under .onto/ first.
- Never run `onto init`. Never run `onto setup` where `onto doctor` says neither a topic nor a template checkout,
  and never `onto setup --new` without a remote to push the new topic to: the folder is lost with this machine.
- In a topic, never run setup except as AGENTS.md section 1 says:
  `onto setup --answers @.onto/setup.json --launch none` with the flags the answers map to, and no --new or --here.
```

The optional `!onto-gaps` playbook:

```
Optional. Devin web app: Settings, Playbooks, Create playbook. Macro: !onto-gaps
## Overview
Turn the topic's open gaps into issue drafts.
## Procedure
1. From the repo root, run `python3 plugins/general-ontology/bin/onto gaps`.
2. Draft one issue per gap the user picks, with the question that closes it.
3. Create each issue only with the user's yes for it.
## Forbidden Actions
- Never create an issue without the user's yes.
- Never change the ontology from this playbook.
```

With Linear connected to Devin, add `!onto` to the synced playbook labels in Devin's Linear settings: a ticket
with that label starts a Devin session that runs the playbook. That is how Devin's docs describe it as of
2026-10-06; the kit does not test it.

## Layout

A topic repo made from this template:

| Path | Holds | Written by |
|---|---|---|
| `ontology.json` | The manifest: name, namespace, title, packs, policy | `onto init` |
| `packs/local.pack.json`, `packs/local.questions.jsonl` | The topic's own kinds, relations and questions | reviewed proposals |
| `graph/nodes.jsonl`, `graph/edges.jsonl` | The graph, sorted by id | reviewed proposals |
| `sources/` | Every ingested text, sanitized, named by its hash | `onto ingest` |
| `proposals/pending/`, `proposals/done/` | Proposals and their reviews | `onto propose`, `onto apply` |
| `interview/log.jsonl` | The question log | `onto answer` |
| `ledger/decisions/`, `ledger/changes.jsonl` | Your decisions and the change log | `onto decide`, every write |
| `metrics/history.jsonl` | Richness over time | every write |
| `imports/` | Pinned, vendored releases of other topics | `onto import` |
| `build/`, `MANIFEST.json`, `VERSIONS.md` | The release files | `onto release` |
| `.claude/settings.json` | The plugin wiring for Claude Code (project scope) | `onto setup` |
| `.devin/`, `.codex/`, `.cursor/`, `.vscode/mcp.json`, `.gemini/settings.json` | The wiring for the other agents | `onto setup --agent` |
| `inbox/`, `.onto/` | Drop folder, kit state and the agent's ops files (gitignored) | you, the kit, the agent |

The kit owns `plugins/`, `.claude-plugin/`, `.agents/skills/`, `.github/workflows/checks.yml`, `examples/`,
`README.md`, `AGENTS.md`, `CLAUDE.md`, `.gitignore`, `.gitattributes` and the launchers `new-topic`, `New topic.command` and `new-topic.cmd`.
Topic repos never edit them.

## Upgrading the kit

```
git fetch kit && git merge --no-commit --no-ff kit/general-ontology
python3 plugins/general-ontology/bin/onto migrate --check
python3 plugins/general-ontology/bin/onto migrate
python3 plugins/general-ontology/bin/onto validate
python3 plugins/general-ontology/bin/onto scan .
git status --short
git add -u && git commit -m "Upgrade the kit"
```

The merge touches only kit-owned paths, and `--no-commit` leaves it open until the checks pass. `git add -u`
stages only tracked files (the merge and migrate's changes), so a stray `.env` or notes file is never swept in:
when `git status --short` lists an untracked file, name it to the user and add it by name only on their yes. `onto migrate --check`
lists the steps the new kit needs (a new kit version needs at least its stamp) and `onto migrate` runs them; it
prints `up to date` when there is nothing to do. When a newer kit's core pack adds a name your local pack already
uses (0.2.0 adds the kind `premise`, its alias `assumption`, the relation `rests_on` and the questions
`q.constraints.premises`, `q.gap.archived_premise` and `q.vocab.kinds`), migrate folds the local declaration into
the core one when both mean the same: the records keep their ids and read as the core kind or relation, and a
local question of that id gives way to the core one. `--check` lists each fold. Otherwise both commands say why
and write nothing.

- A local question folds only when it asks the same thing in the same way: the same `ask` text (ignoring case
  and spacing), the same `fills`, the same `options`, the same gap (`for_gap`) and the same interview rules
  (`repeatable`, `quick`, `follow_ups`, `when`, `until`, its `dimension` and the stage it is asked in). A left
  out `stage` or `dimension` counts as the interview reads it: the stage comes from the dimension, else it is the
  deepening stage, and no dimension is none. Otherwise its answers would attach to a core question that asks
  something else, or the interview would ask it another way. A folded question takes the core `priority`, which
  orders the interview, so the fold line names the change when the priorities differ.
  Move it to a new local id first: `onto migrate --rename-question
  q.vocab.kinds=q.local.vocab.kinds` (the refusal names a free id). This moves the question and every reference to
  it: its interview log lines, the follow-ups and `when`/`until` of other local questions, the `Q:<id>`
  provenance on records, the proposals and the titles of the answer sources. It checks the result, then migrates,
  in one write that puts every file back if it fails. Add `--check` to see the steps first. Decisions hold no
  question id, and the change log keeps its history and records the rename. A gap template (a question with
  `for_gap`) asks its gap only while its id sorts before every other template of that gap, and its gap questions
  are `<id>.<detail>`. So when it is that gap's template now, its new id must sort before the next one, or that
  template would ask every gap it answered again: migrate refuses such an id and names one that keeps its place
  (for example `q.gap.local.archived_premise`, which sorts before `q.gap.missing_field`).
- A local kind or relation folds only when every active record that uses it fits the core declaration and means
  the same: a kind's `label` and `dimension`, when it states them, and a relation's direction (`symmetric`). A kind
  or relation no active record uses always folds. Otherwise, through a reviewed proposal, either change those
  records (`update_node` sets or unsets a node's attrs) or archive them and add what still holds again under
  another kind or relation (an edge cannot change its relation; an archived record is history, and the fold does
  not check it), and run migrate again.

Or run `git merge --abort` to keep the old kit for now.

**From 0.1.0 to 0.2.0**, `onto migrate` only stamps the new version (and folds any clash): `narrows` and
`checked_on` are optional fields and the `assessment` pack is opt in, so no data changes. To move a 0.1.0 topic's
plugin install to project scope, run `python3 plugins/general-ontology/bin/onto setup --plugin project` in it once,
after the user's yes: it writes and commits `.claude/settings.json` (or runs `claude plugin install --scope
project`), and reports the steps that were already done as `already` or `skipped`. Without `--plugin`, setup
leaves a topic it has no record for as it is. A local-scope install keeps working as it is.

**From 0.2.0 to 0.3.0**, `onto migrate` only stamps the new version: the data format is the same, and the core pack
adds no name. The merge brings the kit-owned `.agents/skills/` folder. To open the topic in another agent, run
`python3 plugins/general-ontology/bin/onto setup --agent <names>` in it once, after the user's yes ("Use it with
any agent" above); `onto doctor` then checks those files with its `agents` check.

Then refresh the installed plugin with `/plugin marketplace update general-ontology` and restart Claude Code. A session
that was open during the upgrade keeps the old kit's MCP server and skills in memory, and its MCP server keeps
writing with the old code, until it restarts.

The `onto` command, the MCP server and the hook script hand off to the kit vendored in the repo, so a topic's data
is always read and written by its own kit version. The skills and the hook's `hooks.json` come from the installed
plugin copy, shared by every topic on the machine.

## Merging topic branches

Two sessions that both changed the topic (two machines, two people, or a `git pull`) meet in a merge. The
interview log, change log, richness history, source index and the local questions merge on their own. The graph
files and the local pack (`packs/local.pack.json`) can conflict: the agent resolves them by keeping both sides'
records, kinds and relations, asks you when both sides changed the same record or declared one name differently,
then runs `onto validate --fix` and `onto validate` before the merge commit. The steps are in
`plugins/general-ontology/docs/merging.md`, linked from `AGENTS.md`, "Merging topic branches".

## Composition

A released topic can be imported by another one, read-only, pinned by sha256 and loaded under its namespace:

```
onto import add --ns garden --from ../community-garden --ref v1
onto import add --ns kitchen --from ../neighborhood-kitchen --ref v1
onto import suggest --ns garden --with kitchen
```

`suggest` proposes `same_as` bridges between the two namespaces; the `onto-compose` skill then runs a short interview
on how the topics meet and proposes meaning-level bridges ("menu planning consumes the harvest log"). Bridges are
ordinary local edges. When the composed topic is released, its export bundles its parents, so a fourth topic can
import it without clones of the first two. Two paths to the same topic at different releases are a pin conflict,
allowed only with a recorded decision (`onto decide`, then `--override` and `--keep`).

## Principles

1. Proposals are the only way in: the graph changes only through a reviewed proposal.
2. Every confirmed record cites a source, and every change is in the change log.
3. Ingested text is data: sanitized, marked `[untrusted]`, never followed as an instruction.
4. Nothing is deleted: records are archived or merged, with a reason and a decision. `onto erase` is the privacy
   exception and needs a decision.
5. Same input, same bytes.
6. One call first: briefs and context fill a token budget and name the follow-up calls for what they left out.
7. Cite only what the kit printed; a miss is "not in the ontology".
8. Premises are records: when a premise changes, the records that rest on it are archived and their replacements
   are proposed in the same proposal.
9. Drafts stay drafts until a named owner reviews them; that holds for risk ratings too.

The field notes, short lessons from running topics, are in `plugins/general-ontology/docs/safety.md` (linked from
`AGENTS.md`, section 6) and in the kit README, "Field notes".
