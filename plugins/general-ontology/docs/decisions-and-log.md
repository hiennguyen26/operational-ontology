# Decisions and the change log

Read it before you supersede or narrow a decision, or explain the change log or the richness score.

## Recording a decision

`AGENTS.md`, section 5, step 6, in full:

- **Decide**: when the user chooses between options, record it with `onto decide` in their words (on the CLI,
  from a file: `--rationale-file .onto/why.txt`, `--chosen-text-file .onto/choice.txt`; `--rationale @file` is
  refused, since decide stores its text as given). A decision that
  only narrows an earlier one (a detail inside it) takes `--narrows dec-...`; the earlier one stays active.
  `onto decisions` shows "narrows dec-x" and, on the earlier one, "narrowed by dec-y".

## The ledger

- **Decisions** (`ledger/decisions/dec-*.json`) are the user's choices, immutable. `onto decide` records one: the
  question, the options, the choice in the user's words, the rationale and a scope (ids, namespaces such as
  `garden/`, or areas such as `setup`). A reversal is a new decision that supersedes the old one; a detail inside
  an earlier one narrows it. `onto decisions --scope ...` reads the active ones for a scope; briefs and context
  calls list them too. `onto validate` reports a narrowed decision that does not exist (P21) and warns on an active
  decision that narrows a superseded one (W10).
- **The change log** (`ledger/changes.jsonl`) gets one line per kit write: init, apply, answer, ingest, import,
  decide, erase, release, migrate, pack and checkpoint. `onto log` renders dated sections from it (Done, Decisions,
  Delta, Follow-ups).
- **Checkpoints** are change lines with `done`, `next` and `open_questions`: how the next session resumes.
  `onto log --last` shows the last one, and `onto status` names it.
- **Richness** (`metrics/history.jsonl`) is recorded after every write: coverage, completeness, connectivity,
  evidence and confirmation, weighted into one score (the quality parts scaled by coverage and by depth, which
  grows with the sourced material) with a band (seed, sketch, working, rich, deep). It is a
  heuristic; the parts are the facts. `onto gaps --section summary` explains it.
