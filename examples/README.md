# The demo

Four small, synthetic topics built end to end through the `onto` command line, each in its own temp git repo. People
appear only as roles. The notes carry a fake address (`.invalid`) and a fictional `555` phone number, so the
redactor has something to count, plus a prompt-injection line and a `</script>` line, so you can see both stay inert.

```
bash examples/demo.sh            # narrate: every command and its output
bash examples/demo.sh --check    # quiet: one line per checked step (what CI runs)
bash examples/demo.sh --keep     # keep the temp folder to look around
```

It needs `python3` 3.9 or newer (set `PYTHON` to pick one) and `git`, never the network, and takes well under a
minute. The clock is pinned (`ONTO_FIXED_NOW`) and git runs without your global config, so every run gives the same
bytes. Every step checks what it expects and the demo stops at the first surprise. `tests/test_e2e.py` in the kit
replays the same story in process with finer checks.

## The story

1. **garden** (`community-garden`, ns `garden`)
   1. `onto init`, then the local pack (`garden/local.pack.json`: kinds `plot` and `crop`, relations `tends` and
      `grown_in`, plus the questions in `garden/local.questions.jsonl`) as one proposal of `add_kind`,
      `add_relation` and `add_question` ops, accepted in full.
   2. Replay `garden/answers.jsonl` through `onto answer --apply`: 8 answers, the 5 quick-start questions first.
      They record the plot coordinator, the goal and its metric, the bed captains and the watering rota, the harvest
      log, the north bed, tomatoes and mint.
   3. `onto ingest` the handbook excerpt (`garden/inbox/handbook-excerpt.md`). The email and phone are redacted and
      counted; the injection line shows only inside untrusted fences.
   4. `onto propose` the prewritten ops (`garden/handbook.proposal.json`, standing in for the agent). It is refused:
      op 6 quotes a sentence the handbook does not hold.
   5. Drop op 6 and propose again. The new role "Bed steward" matches the existing `role:bed-captain` (its alias).
   6. `onto eval` scores the proposal against `gold/handbook.gold.json`.
   7. `onto apply --accept 1-4,7 --draft 5 --reject 6`: the steward, the no-pesticides rule and their links are
      accepted, one link stays a draft, the wrong "owns the harvest log" link is rejected, and the merge folds the
      bed captain into the bed steward.
   8. Richness has risen; `onto next` asks "Who keeps Harvest log up to date?"; `onto validate` is clean;
      `onto release --write --commit --notes first` tags `v1`.
2. **kitchen** (`neighborhood-kitchen`, ns `kitchen`): the same pattern with the menu planner, the weekly menu, menu
   planning, the recipe binder, tomatoes and basil, the tomato salad (a `dish` whose `serves` field says a batch feeds
   40) and, from the menu notes, the allergen-labels rule. Released as `v1`.
3. **garden-to-table** (`garden-to-table`, ns `g2t`)
   1. `onto import add` garden `v1` and kitchen `v1`.
   2. `garden-to-table/bridges.proposal.json` maps `garden/crop` to `kitchen/ingredient`.
   3. `onto import suggest --ns garden --with kitchen` puts `garden/crop:tomato same_as kitchen/ingredient:tomato`
      first; it is accepted.
   4. The stage C answers (`garden-to-table/answers.jsonl`) add the goal "Weekly harvest menu", its metric, and the
      bridge "kitchen menu planning consumes the garden's harvest log". A decision records when the menu locks.
   5. Checks: a path from the bed steward to the weekly menu crosses a bridge; `onto brief "weekly menu" --budget 800`
      names both namespaces and a bridge; `onto context "write the weekly menu"` keeps the template headings;
      `onto gaps` lists an `unbridged_import`. Released as `v1`.
4. **market** (`farm-market`, ns `market`)
   1. `onto import add` g2t `v1` while the garden and kitchen repos are moved away: the parents come from the g2t
      bundle (`via g2t`), no clones needed. The market answers (`market/answers.jsonl`) add a stall and its bridge.
   2. The garden gains one node (squash) and releases `v2`. Importing garden `v2` into the market is refused as a pin
      conflict; after `onto decide`, `--override <decision> --keep garden=<sha>` pins it.
5. **Tamper**: editing `imports/kitchen/export.json` in g2t makes `onto validate` fail with P15, and the version line
   says `mismatch`. The file is restored.
6. **Determinism**: every topic's export is built twice; the bytes are the same.

## Files

| File | What it is |
|---|---|
| `demo.sh` | The script above |
| `garden/local.pack.json`, `kitchen/local.pack.json` | Local packs, in the pack format (`pack.schema.json`); the demo turns them into additive ops |
| `garden/local.questions.jsonl`, `kitchen/local.questions.jsonl` | Local interview questions, applied as `add_question` ops with the pack |
| `*/answers.jsonl` | Scripted interview answers: one `{q, text, ops}` per line. Provenance entries give `loc: "Q:<question id>"` (all bank questions, so no `@` part) and a verbatim quote, and leave `src` out: `onto answer` fills in the answer's own source (and `loc`, when it is left out too) |
| `garden/inbox/handbook-excerpt.md`, `kitchen/inbox/menu-notes.md` | Notes to ingest |
| `garden/handbook.proposal.json`, `kitchen/menu.proposal.json` | Prewritten ops standing in for the agent; `@SRC@` stands for the id `onto ingest` returns |
| `garden-to-table/bridges.proposal.json` | The `map_kinds` op for the composed topic |
| `gold/handbook.gold.json` | What a careful reader extracts from the handbook, for `onto eval` (endpoints as `{kind, name}`) |
