# Merging topic branches

Read it before you resolve a merge, and before any other `onto` command while one is open. Every command runs
from the repo root as `python3 plugins/general-ontology/bin/onto <command>`, written `onto <command>` here.

Two sessions that both changed the topic (two machines, two people, or a `git pull` from `origin`) meet in a merge.
The four logs and `packs/local.questions.jsonl` merge by keeping both sides' lines (`.gitattributes`). The graph
files and `packs/local.pack.json` do not, so git can stop with a conflict in them. Resolving it is the one time you
edit topic files by hand. Until it is resolved the kit refuses writes that touch the graph, so resolve it before any
other `onto` command:

1. List the conflicted files: `git diff --name-only --diff-filter=U`. If any of them is not `graph/nodes.jsonl`,
   `graph/edges.jsonl` or `packs/local.pack.json` (for example `ontology.json`, `imports/lock.json`, or a file under
   `sources/` or `proposals/`), run `git merge --abort` now, before any `onto` command (a kit write blocks the
   abort), and ask the user.
2. Show the merge base in the markers: `git checkout --conflict=diff3 -- <the conflicted files>`.
3. Graph files: in each conflict, delete the marker lines and the base section (from `|||||||` up to `=======`), and
   keep every line of both sides. Each line is one record, so records with different ids simply sit side by side.
4. When both sides hold a line for the same id with different content: if one of them is identical to that id's
   line in the base section, that side did not change the record, so delete it and keep the other. If both
   changed it, show the user both lines and keep the one they choose (the other side's answer stays in the
   interview log; ask it again to record it).
5. `packs/local.pack.json`: pack ops only add, so the merged pack keeps every entry of both sides: each kind (with
   every field either side gave it), each relation and each `kind_map` pair. In each conflict, write the two sides'
   entries as one JSON object (or list), then delete the marker lines and the base section. The two sides whole:
   `git show :2:packs/local.pack.json` (yours) and `git show :3:packs/local.pack.json` (the merged branch). The
   file must parse as JSON; order and spacing do not matter. When one name has two different declarations, show the
   user both and keep the one they choose.
6. Run `onto validate --fix`: it restores the sort order of the graph and the logs, rewrites the pack as canonical
   JSON and drops duplicate lines. It also collapses source index lines that hold the same text (the same id and
   sha256, as both sides ingesting one text leaves them) and drops a local edge that an import already holds. It
   never chooses between two versions of a record: a `P06 id ... with other content` it still reports is step 4.
7. Run `onto validate` until it prints `ok`. Then commit as in `AGENTS.md`, section 5, step 7: run `onto scan .` (no
   hits) and `git status --short`, stage the files the merge and the fixes touched with `git add -u` (tracked files
   only, so a stray `.env` or notes file is never swept in), name any untracked file to the user and add only the ones
   they say yes to (`git add -- <file> ...`), then `git commit --no-edit` completes the merge.

A merge that git completes on its own still needs steps 6 and 7; commit what they change the same way, with `git add -u
&& git commit -m "..."` after the scan and the status check. A `P20 duplicate question id` there means both sides added
a question under one id to `packs/local.questions.jsonl`: show the user both lines and delete the one they do not
choose.

For a topic made with `onto init --path`, the same files sit under its folder; add `--repo <folder>` to the `onto`
commands.
