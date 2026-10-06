# The setup interview

Read it before you ask the first setup question. It is the long form of `AGENTS.md`, section 1: the seven
questions, how to keep the interview going, and the hand-off to `onto setup`. Every command runs from the repo
root as `python3 plugins/general-ontology/bin/onto <command>`, written `onto <command>` here.

**In a cloud agent** (cloud Devin, the Copilot cloud agent, a Cursor cloud agent: a machine discarded after the
session), read `cloud-agents.md` first. Never create a topic with `--new` on that machine without a remote: the
folder is lost with it. Recommend that the user runs `./new-topic` on their own machine, pushes the topic to a
private remote and adds that repo to the agent. Run the interview here only with an `--origin` URL (question 4),
and push right after setup with the user's yes.

## The questions

Ask one question at a time, as `AGENTS.md`, section 2, says, and skip any the user already answered. Each question below
has why it matters, an example answer, its options, and what it records and where. Setup records a choice as a decision
(scope `setup`, `decided_by: user`) only when it came from the user: a flag passed together with `--answers`, or a typed
prompt answer. Defaults are never recorded.

1. "In a sentence, what is this ontology about?"
   - Why: the title names the topic everywhere, and the name and namespace come from it.
   - Example answer: "Running our community garden: plots, crops and the people who tend them."
   - Options: none. Suggest a title (such as "Community garden"), a name (a slug such as `community-garden`) and a
     namespace (lower case, starts with a letter, at most 32 characters, such as `garden`), and confirm all three in
     one line. Say that the title cannot be changed later: no command changes the title of a topic.
   - Records: the title (`title` in the `setup` object of `.onto/setup.json`), `--name` and `--ns` go into
     `ontology.json` and the root node `topic:<ns>`; the user's sentence itself (`summary` in the `setup` object)
     becomes the root node's summary, so the interview never asks what the topic is about again. No decision.
2. "What should it help you do or decide?"
   - Why: the goal ranks every later question and frames every deliverable.
   - Example answer: "Decide which beds to rotate each season."
   - Options: none.
   - Records: an `answers` item for `q.frame.goal` in `.onto/setup.json`, in the user's words. Setup replays it with
     `onto answer --apply`, so the quick start starts at 1 of 5. The goal's name and id come from the answer's
     first sentence; when that is not a short name of the goal (a reply that starts "Like when I ..."), suggest
     one ("Track hive treatments"), confirm it with the user in the same turn, and add it as the item's `name`.
3. "Where should it live?"
   - Why: git and imports hang on files that cloud sync evicts.
   - Example answer: "The default is fine."
   - Options: `~/Ontologies/<name>` (recommended), "this folder", "somewhere else". In a cloud agent, every folder
     is on the session machine: say so, and that the topic needs a remote and a push (question 4). Warn when the
     choice is in a cloud-synced folder (`onto doctor` names them); pass `--cloud-ok` only when the user keeps it
     after the warning.
     "This folder" turns this kit checkout itself into the topic (it leaves the `general-ontology` branch, and new
     topics then need another kit checkout), so it fits only a clone made for this one topic: say so and ask once
     more ("Use this checkout itself? y/N") before you pass `--here`, as the terminal prompt does.
   - Records: `--new ~/Ontologies/<name>`, `--here` or `--new <folder>`. Decision "Where should it live?" with the
     choice `home`, `here` or `elsewhere`; its text is `~/Ontologies/<name>` or the folder's last name, never the
     full path.
4. "Will anyone else work on it, and where is it backed up?"
   - Why: a private remote is the backup and the way to share; setup adds it but never pushes.
   - Example answer: "Just me for now."
   - Options: "just me, no remote for now", "a private git remote (paste the URL)", "decide later". In a cloud
     agent only the remote keeps the topic: offer just that one, or stop and send the user to their own machine.
   - Records: a remote gives `--origin <url>` (a URL that holds a password or a token is refused) and the decision
     with the URL, without any user name; a folder on this machine is recorded by its last name only. For "just
     me" or "decide later" there is no flag: add the decision to the `decisions` list of `.onto/setup.json`
     (`chosen` is `solo` or `later`).
5. "How sensitive is what you will feed it?"
   - Why: it sets what the kit redacts, or refuses, in everything you ingest.
   - Example answer: "Internal notes; some name volunteers."
   - Options: "public or my own notes" (`--personal keep`: nothing is redacted), "internal, may name people"
     (`--personal redact`, recommended: emails, phone numbers, ids and card numbers are redacted), "confidential"
     (`--personal refuse`: text holding those is refused). Say plainly that no setting finds people's names in
     prose: a name in an answer or a document is stored as written. Text whose names must not be stored needs
     the names taken out before it is fed in.
   - A free-text reply: pick the closest option, say which one and what it does, and ask the user to confirm it.
   - Records: `policy.personal` in `ontology.json`, the same value for every personal kind. Decision "How sensitive
     is what you will feed it?". Pass it only as `--personal`, never in the `decisions` list (setup refuses it
     there). Without the flag the kit's default applies: contact details redacted, user names in paths (a home
     folder such as `/Users/<name>/`, a `.netrc` login) kept, and names in prose kept. `redact` also redacts those
     user names; that is the only difference.
6. "Do you also want to track risks and controls?"
   - Why: the `assessment` pack adds risk and control records with rating checks; it is off unless asked for.
   - Example answer: "Yes, we have a few safety risks to watch."
   - Options: "yes" (`--packs assessment`), "no", "later".
   - Records: yes adds the pack to `ontology.json` as one logged `pack` change, and the decision "Which extra
     built-in packs should it use?" with `assessment`. For "no", add that decision to `.onto/setup.json` with
     `chosen` set to `none`; for "later", the same with `chosen` set to `later`. It can be turned on later with
     `onto pack add assessment`; then record the choice so it is not offered again:
     `onto decide --question "Which extra built-in packs should it use?" --chosen assessment --scope setup
     --supersedes <the later decision's id>`. When the user says "later" again on that offer, record
     `--chosen later --rationale "offered again; the user said later" --supersedes <id>` instead, so it is not
     offered again, and for "no", `--chosen none --rationale-file .onto/why.txt --supersedes <id>` with the
     user's words in that file. While the choice is "later", never declare a local kind or relation the pack
     declares (`risk`, `control`, `mitigates`, `threatens`): `onto pack add assessment` then refuses (P20) until
     that local declaration is taken out of `packs/local.pack.json` by hand, with the user's yes.
7. "Which agents will open this topic?"
   - Why: each agent reads its own files; setup wires the ones picked and prints what to set up in an agent's own
     app.
   - Example answer: "Claude Code and Devin."
   - Options, more than one allowed: Claude Code (`claude`), Devin (`devin`), Codex (`codex`), Cursor (`cursor`),
     Copilot (`copilot`), Gemini (`gemini`), another agent (`generic`). Pass the names as `--agent claude,devin`
     (or the list `setup.agent`). Without it, setup wires Claude Code alone.
   - Records: the decision "Which agents will open this topic?" with the names. The `agents` step writes each
     agent's files (`onto agents show <name>` lists them), commits them as "Wire the agents for <name>", and prints
     the parts set in the agent's own app (Devin's MCP server and playbooks, Copilot's cloud MCP) after `Next:`.
     A skipped 7 skips 7a too.
7a. "Install the plugin for this repo (recommended), or run it without installing?" Ask it only when Claude Code is
   one of the agents.
   - Why: the plugin brings the skills, the MCP tools and the session-start hook to Claude Code in this repo.
   - Example answer: "Install it."
   - Options: "install it for this repo" (`--plugin project`, recommended), "run it without installing"
     (`--plugin plugin-dir`). Two more exist: `--plugin local` (for this user only) and `--plugin skip` ("do not
     wire it"). Pass `--plugin skip` only when the user picked it; a flag passed with `--answers` is recorded as the
     user's decision. When the question is skipped, leave `--plugin` out. Without Claude Code among the agents,
     setup skips the plugin step.
   - Records: the decision "Install the plugin for this repo (recommended), or run it without installing?". With
     `project`, setup writes `.claude/settings.json` (or runs `claude plugin install --scope project`) and commits
     it as "Wire the general-ontology plugin".

## During the interview

- **During the setup interview** there is no topic yet, so `onto next`, `onto answer` and checkpoints do not work.
  Show `setup 3/7` in place of the progress line. Update `.onto/setup.json` every turn with what the user said
  (`.onto/` is gitignored in a template clone). "skip" and "n/a" leave that question's flag out, so the default
  applies (for question 3, `~/Ontologies/<name>`), and add its number to `setup.skipped`, so a resumed interview
  does not ask it again. The topic records a skipped 2 as a skipped answer to `q.frame.goal` and a skipped 4 to 7
  as decisions with the choice `skipped`; a skipped 1 or 3 only takes its default. "later" does the
  same, except where the question offers "later" as an option and records it. Put the answers that map to flags in
  the file's `setup` object as you go. On "stop", keep `.onto/setup.json` and say the
  next session resumes from it: it asks only what the file still lacks. A run with no failed step moves the file
  to `.onto/setup.<name>.done.json`, so a `.onto/setup.json` that is still there is always a stopped interview; a
  new topic from the same kit checkout starts with no file. The kept copy has personal data redacted as the
  topic's policy says (under `keep` it is kept as typed); it sits outside the topic, where `onto erase` cannot
  reach it, so delete it once the topic is set up.

## Hand off to onto setup

1. Write the answers to the gitignored `.onto/` folder as `.onto/setup.json`, in the user's words:

   ```json
   {"setup": {"title": "Community garden",
              "summary": "Running our community garden: plots, crops and the people who tend them."},
    "answers": [{"q": "q.frame.goal", "text": "Decide which beds to rotate each season"}],
    "decisions": [{"question": "Will anyone else work on it, and where is it backed up?",
                   "options": ["solo=just me, no remote for now", "remote=a private git remote", "later=decide later"],
                   "chosen": "solo"}]}
   ```

   An `answers` item takes `q`, `text` and an optional `status` (`answered`, `skipped`, `na`, `later` or `skip_stage`);
   the `q.frame.goal` item also takes `name`, the goal's short name the user confirmed. A `decisions` item takes
   `question` and `chosen`, plus optional `options`, `chosen_text`, `rationale`, `recommended` and `scope` (default
   `["setup"]`). Every value is a string, and setup checks them all before it writes anything. A choice passed as a flag
   in the same run wins over a file item for the same question, so a changed choice supersedes the old decision. A
   decisions item for a choice that has a flag (the location, sensitivity, a remote URL, a pack name, the agents, the
   plugin mode) is refused: pass the flag. The optional `setup` object holds the answers that map to flags, so a
   stopped interview resumes from the file: `title`, `summary` (the user's sentence from question 1), `name`, `ns`,
   `new` (a full path starting with `/` or `~`), `here` (true), `origin`, `personal`, `packs` (a list), `agent` (a
   list of agent names), `plugin` and `cloud_ok`, plus `skipped` (the numbers of the questions the user skipped,
   such as `[5]`). Flags on the command line win over it. Put the user's own words (the title, the summary, a folder
   they typed) there, never on the command line: a shell reads `$`, backticks and quotes in them.
2. Run setup from the template checkout with the flags the answers map to:

   ```
   python3 plugins/general-ontology/bin/onto setup --new ~/Ontologies/<name> --name <name> --ns <ns> --personal redact --packs assessment --agent claude --plugin project --answers @.onto/setup.json --launch none
   ```

   The title and the summary come from the `setup` object. Add `--origin <url>` for a remote, `--cloud-ok` for an
   accepted synced folder, and use `--here` instead of `--new` for "this folder". Quote every path: it may hold
   spaces. A folder the user typed goes in `setup.new`, and then the command has no `--new` (nor `--here`): a
   `--new` that names another folder wins, and setup records no location decision for it. A skipped question 3
   takes the default, `~/Ontologies/<name>`. Setup checks every value of the file before it writes anything: a
   question id no pack asks, a choice that is not one of its options, credential-like text, personal data the
   chosen `--personal` refuses, or a control character in the summary fails the run at once, naming each place.
3. Show its checklist. Each step (preflight, clone, branch, init, packs, answers, commit, plugin, agents, launch)
   says `done`, `already`, `skipped (reason)` or `failed`, so running it again is safe and does only what is missing. It commits
   the topic as "Start <name>", and only once `inbox/` and `.onto/` are ignored. Do not run `onto init` yourself.
   - **A step failed** (setup exits 1): give the user that step's detail, which names the fix, and run the same
     command again once it is fixed. Do not hand over the `Next:` lines as a finished topic; on a failure they start
     with the fix line. One exception: when only the `plugin` step failed (offline, or a private template without
     git credentials claude can use), the topic is made and committed, and the `Next:` lines open it with
     `claude --plugin-dir ./plugins/general-ontology`. Hand those over, and say the plugin step's detail: how to
     install it later, or to rerun with `--plugin plugin-dir`.
     A new machine often has no git name and email: preflight then fails before anything is written, and the fix
     is `git config --global user.name "<their name>"` and `git config --global user.email "<their email>"`, with
     values the user gives you.
   - **The commit step was skipped, or says "not committed: N files setup did not write"**: setup commits only the
     topic files it wrote, and never sweeps in other files in the folder or files that were uncommitted before it
     ran, also inside the topic folders (notes, drafts, a `.env`, a `sources/notes.txt`). A rerun after a failed
     commit still commits the files setup wrote, except the ones the user changed since ("changed since setup
     wrote them"). Run `git check-ignore -q inbox/ && git check-ignore -q .onto/`, show
     `git status --short`, name each file to the user, and commit only the ones they say yes to
     (`git add -- <file> ... && git commit -m "<message>"`). Never commit a file that may hold a password or a key.
     When it says a file holds credential-like text, ask the user to take that text out, then run setup again;
     the rerun commits that file.
4. Go on, once no step failed. With `--here` the session is already in the topic: start the quick start in the
   same conversation (`onto next`). With `--new`, give the user setup's `Next:` lines: the folder, then one line
   per chosen agent on how to open the topic there (for Claude Code, "Open it in Claude Code (terminal `claude`, or
   the desktop app's Code tab) and say: start the ontology"; for another agent, "open the folder and ask the agent
   to follow AGENTS.md"), then the paste blocks for the parts set in an agent's own app. There the first turn
   finds a fresh topic whose setup questions are already recorded (a skipped one as `skipped`), so it goes straight
   to the quick start. Run from a terminal, setup starts Claude Code in the new folder itself when it can and
   Claude Code is one of the agents.
