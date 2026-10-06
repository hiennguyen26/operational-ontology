# Setup questions in a fresh topic

Read it before you ask the setup questions in a fresh topic (`AGENTS.md`, section 1, the "Fresh topic" row).
Every command runs from the repo root as `python3 plugins/general-ontology/bin/onto <command>`, written
`onto <command>` here.

A topic made by one click (`./new-topic`, or `onto setup --yes`) has its title, name and folder but no setup
decisions. Before the quick start, ask the questions it still needs, one per turn:

- Skip 1 (the title is set) and 3 (it lives here). Ask 7a only when Claude Code is one of the agents (question 7),
  and skip it when the plugin's skills are already loaded. When the title is still the placeholder made from the
  folder name ("My topic" for `--yes` alone), tell the user: no command changes the title of a topic, so a
  one-click topic should be made with `--title` (a fresh topic holds nothing yet, so making it again with `--title`
  loses nothing; remove the old folder only with the user's yes).
- Skip what is recorded. `onto decisions --scope setup` lists the recorded choices (4 to 7a); a question skipped in
  the setup interview is recorded with the choice `skipped` and is not asked again. Question 2 is an answer, not a
  decision: it is open while `onto next` still lists `q.frame.goal` (`onto log` names the question of each answer;
  a goal skipped in setup is logged as skipped, and `onto next` holds it back for two days).
- Write the answers to `.onto/setup.json` as in the hand-off (`setup-interview.md`, "Hand off to onto setup"), then run
  setup in the topic with the flags they map to: `onto setup --answers @.onto/setup.json --launch none`, plus `--origin
  <url>` (4), `--personal <value>` (5), `--packs assessment` (6), `--agent <names>` (7) and `--plugin <mode>` (7a,
  only when asked). Without `--agent`, setup keeps the recorded agents, else wires Claude Code alone. Without
  `--plugin`, setup keeps the recorded plugin choice, else the mode an earlier setup run on this machine tried (it
  tries a failed install again), and with neither it leaves the wiring as it is. It replays the goal (also after an earlier
  skip), adds the pack and the remote, records the decisions and commits them as "Record the setup of <name>", unless
  other files were uncommitted before it ran.
- Sensitivity (5) is set at init, and no `onto` command changes it later. Never put question 5 in the `decisions`
  list: pass `--personal`, and setup records the decision only when the topic's policy already matches it. When it
  prints "--personal ... not applied", the policy differs (a one-click topic redacts contact details and keeps user
  names in paths, which is none of the three answers). Tell the user. With their yes, set every value under
  `policy.personal` in `ontology.json` to the chosen one, run `onto validate --fix` and `onto validate`, then run the
  same setup command again: setup kept `.onto/setup.json` for it, and it now records the decision. Its commit step
  then says `skipped`, because `ontology.json` was changed before it ran: run
  `git check-ignore -q inbox/ && git check-ignore -q .onto/`, show `git status --short`, and with the user's yes
  run `git add -A && git commit -m "Record the setup of <name>"`. Nothing is ingested yet in a fresh topic, so no
  stored text changes. Without a yes, record nothing and keep the default.
