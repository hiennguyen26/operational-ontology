@AGENTS.md

## Claude Code specifics

### Start

- The session-start hook prints where things stand and names the skill to use next: `onto-interview` in a template
  checkout (a fresh clone still on the template branch included) or a young topic, `onto-review` when proposals are
  pending, otherwise `onto`. Start there, and run the setup interview before `onto setup`, even when the hook names
  `onto setup` too. Run `onto status` (or `onto_status`) when the hook printed nothing.
- In a template checkout, run the setup interview (AGENTS.md, section 1, and
  `plugins/general-ontology/docs/setup-interview.md`), write `.onto/setup.json` (the user's
  title and sentence go in its `setup` object, never on the command line), then run
  `python3 plugins/general-ontology/bin/onto setup --new ~/Ontologies/<name> --name <name> --ns <ns> --answers @.onto/setup.json --launch none`
  with the flags the answers map to; leave `--new ~/Ontologies/<name>` out when the user typed a folder (it is in
  `setup.new`) or picked this one (`setup.here`). Setup does README step 1, `init`, the first commit and the
  plugin wiring for the new repo. Pass `--launch none` from inside a session; run from a terminal, setup starts Claude Code in the new
  folder itself.
- Prefer the MCP tools (`onto_*`) over the CLI when the plugin is loaded.

### Asking questions

Ask with the question tool (AskUserQuestion) whenever a question has options or a choice is the user's: one
question per call. Put the why line and the example answer in the question text, or in the turn's text just before
the call. A question without options stays plain text, with the why line and the example answer.

- The tool takes 2 to 4 options and adds a free-text "Other" itself. Put the kit's `options` first, the
  recommended one first and marked "(recommended)". Fill a spare place with "skip".
- Say in the question that "later", "n/a", "why?" or the user's own words go in "Other". When a question has more
  than 4 options, show the first 3 and "skip"; the rest go in "Other" too.
- Setup question 7 ("Which agents will open this topic?") takes several answers: ask it with `multiSelect: true`
  and the options Claude Code (recommended), Devin, Codex and Cursor, and say that Copilot, Gemini, another agent
  or "skip" go in "Other". Pass the picks as `--agent` (for example `--agent claude,devin`). Ask 7a, the plugin
  question, only when Claude Code is one of them.
- Map the reply: "skip" records `status=skipped`, "later" records `later`, "n/a" records `na`, and "why?" gets the
  why line, then the same question again. An option or the user's words is the answer.
- Record the answer the same turn (`onto_answer`, and `onto_decide` for a choice), and put the progress line and
  the "Noted: ..." recap in the text of the next turn.

### Wiring the plugin

- **Project scope** (the default): `onto setup --plugin project` runs `claude plugin marketplace add
  "<kit-url>#general-ontology" --scope project` and `claude plugin install general-ontology@general-ontology --scope
  project`, or writes the same entries to `.claude/settings.json`, and commits them. Everyone who trusts the folder
  gets the plugin.
- **Local scope**: `onto setup --plugin local`, or by hand (README step 2): the marketplace once per machine from
  the template's remote (`/plugin marketplace add <repo-url>#general-ontology`, where `git remote get-url kit`
  prints `<repo-url>`), then `/plugin install general-ontology@general-ontology` in each topic with "Install for
  you, in this repo only (local scope)". Only when that URL is a git URL: when the plugin step said the kit URL is
  a local folder (or the template has no git remote), use **No install** below instead.
- **No install**: `onto setup --plugin plugin-dir`, then start each session with
  `claude --plugin-dir ./plugins/general-ontology`.
- Never `/plugin marketplace add ./` in a topic: it takes over the one marketplace every topic loads its skills
  from. `onto doctor` checks the wiring and names the repair.

### Stopping and safety

- When the user stops, write a checkpoint so the next session can resume (`onto log --checkpoint --done-file
  .onto/done.txt --next-file .onto/next.txt --open-questions-file .onto/open.txt`, each file one item per line, so
  the user's words never pass through a shell), then offer to
  commit the session once `git check-ignore` confirms `inbox/` and `.onto/` are ignored and `onto scan .` finds
  nothing: `git add -A && git commit -m "..."` when `git status --short` lists only topic files; any other file
  (a `.env`, notes) is named to the user and committed only on their yes (AGENTS.md, section 5).
- Never run a tool node's `invoke` line without the user's explicit yes for that run, and never run a draft or
  untrusted one (AGENTS.md, section 6).
