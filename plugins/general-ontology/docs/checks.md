# Checks before a commit or a release

Read it before a release, and before you commit a change to the kit itself.

Run these from the repo root:

```
python3 plugins/general-ontology/bin/onto validate        # every problem (exit 1) and warning
python3 plugins/general-ontology/bin/onto scan .          # secrets and denylisted terms (exit 2 on hits)
python3 plugins/general-ontology/bin/onto release         # dry run of the release ladder
python3 plugins/general-ontology/bin/onto doctor          # the machine, the folder and the plugin wiring
```

A release (`onto release --write --commit --notes "..."`, only when the user asks) validates, scans, writes
`build/export.json`, `build/cards.json`, `MANIFEST.json` and the `VERSIONS.md` row, commits exactly those files and
tags `vN`. With `--push` and a denylist, it also scans the author, committer and message of every commit not yet
on origin (the kit's own history on `kit` is left out), since a push publishes them; without `--push` it only notes
them.

When you change the kit itself (on the template branch), also run:

```
python3 -m unittest discover -s plugins/general-ontology/tests -t plugins/general-ontology
bash examples/demo.sh --check
```

CI (`.github/workflows/checks.yml`) runs the tests, the scan, the demo and a non-blocking token benchmark.

## Checkpoint and commit

`AGENTS.md`, section 5, step 7, in full:

- **Checkpoint and commit**: when the user stops, `onto log --checkpoint` with what was done, what comes next and the
  open questions, each written to a file first, one item per line (`--done-file .onto/done.txt --next-file
  .onto/next.txt --open-questions-file .onto/open.txt`): they are often the user's words. Then offer to commit and
  commit when the user says yes: check that `inbox/` and `.onto/` are ignored (`git check-ignore -q inbox/ && git
  check-ignore -q .onto/`), run `onto scan .` (no hits), then `git status --short`. When it lists only topic files, run
  `git add -A && git commit -m "..."`. Name every other file to the user (a `.env`, notes, drafts) and commit only the
  ones they say yes to (`git add -- <file> ...`), never a file that may hold a password or a key. When `inbox/` and
  `.onto/` are not ignored, never run `git add -A`: the raw inbox would be committed. Commits are the undo, and `onto
  release --commit` refuses to run while topic files are uncommitted.
