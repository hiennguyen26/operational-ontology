# How to interview: the long form

Read it before your first interview turn. `AGENTS.md`, section 2, has the rules; this page has the cases that
need more words, the plain-text form of one question and a short example.

## The rules in full

- **Map the reply.** A number picks that option. "skip" records `status=skipped`, "later" records `later`, "n/a"
  records `na`: for a stage question nothing more is asked about its dimension, and for a gap question (an id that
  starts with `q.gap.`) it closes only that question. "why?" gets the question's why line, then the same question
  again. Anything else is the answer, in the user's words.
- **Set what the question fills.** A question's `fills.fields` (`sets:` in `onto next`) are the fields its answer
  sets, in `attrs` of the record it adds (`audience` for `q.frame.deliverable`, `success` for `q.frame.goal`).
  Set them from the same answer; a field left empty comes back as a gap question about what the user just said.
- **Record every answer in the same turn**, in the user's words (MCP `onto_answer apply=true`; on the CLI, write the
  words to `.onto/answer.txt` and run `onto answer <q> --text-file .onto/answer.txt --apply`; for a choice, write the
  user's words to `.onto/why.txt` and run `onto decide ... --rationale-file .onto/why.txt`, or `--chosen-text-file` for
  a choice in their words). Never put the user's words on a shell line: the shell reads `$`, backticks and quotes in
  them. Write the file with a file tool, or with a quoted heredoc (`cat > .onto/answer.txt <<'EOF'`, the words, then
  `EOF`). Fold the recap of the last answer into the next turn ("Noted: ..."), never a separate OK turn, unless the
  answer changes something already confirmed or adds a kind, a relation or a question (pack changes wait for
  `confirm=true`): then show the preview and ask first. A preview that says "would be refused" is never put to the user
  to approve: fix the problem with them first.
- **Probe once** when an answer is vague or a list is cut short ("Anyone else?", "Can you give one example?"). The
  probe is the one turn that records nothing: the next turn records the first answer and the reply to the probe
  together, in the user's words, the more specific of the two first (a goal is named after the first sentence of
  its answer, so in setup give the goal item a `name` the user confirmed when that sentence is an example).
- **Follow what is new.** When an answer names something new, ask the kit's follow-up for it next
  (`onto_next about=<id>`, CLI `onto next --about <id>`). The answer result names the best next question
  (`ask next:`).
- **After the quick start, ask for the main kinds.** Once the five quick questions are handled, `onto next` (and the
  answer result) names `q.vocab.kinds` before stage 1 and before any gap question: "What are the main kinds of
  things in {topic}? Name a few, with one example of each." The kinds the user names become the topic's own
  vocabulary. For a garden, an example answer: "Beds, crops and harvests. The north bed is a bed, tomatoes are a
  crop, and the July harvest is a harvest." Propose each kind as a local kind (an `add_kind` op: `name`, and a
  `kind` with `label` and `plural`) and one node per example, linked in the same answer (`part_of` the topic, or the
  link the user named). Reuse a kind the topic already has, core or local, rather than add a twin. The `add_kind`
  ops wait for `confirm=true`: show the preview and ask first (the one OK turn), then record it with
  `confirm=true`. Then continue with `onto next`.

- **Show the progress line every turn.** `onto next` prints it: `stage 0 frame | quick start 1/5 | answered 1,
  skipped 0`.
- **The user can say "stop" at any time.** Write a checkpoint (`onto log --checkpoint --done-file .onto/done.txt
  --next-file .onto/next.txt --open-questions-file .onto/open.txt`, each file one item per line), then offer the commit
  (`AGENTS.md`, section 5, step 7). A "stop" on the OK turn of a preview: the preview stored nothing, not even the
  answer. First record the answer in the user's words with only the ops that need no confirmation and do not use the
  held change (`onto answer <q> --text-file .onto/answer.txt --ops @.onto/ops.json --apply`, or without `--ops` when
  none are left), then name the held change in the checkpoint (a line "confirm the new kind hive" in `.onto/open.txt`,
  passed as `--open-questions-file .onto/open.txt`), so the next session offers it again.

## Answers in the work loop

`AGENTS.md`, section 5, step 1, in full:

- **Interview** (`onto-interview`): `onto next` ranks the questions; ask them as `AGENTS.md`, section 2, says, then
   `onto answer <q> --text-file .onto/answer.txt --apply`. Stated facts quoted from the answer are accepted; the
   rest become drafts. A drafted link comes back as a question (`q.gap.draft_link.<key>@<id>`, one per set of
   drafted links, "Are these drafted links ... right?"); answer it with an `add_edge` op of the same link,
   `basis: "stated"`, quoting the user's yes, to confirm it, and an `archive` op (with the user's yes) for a wrong
   one.
   An inferred change to a confirmed record is held for review, and the other ops of that answer wait with it, so
   record a guess about a confirmed record in its own answer.
   Leave `src` and `loc` out of an answer's provenance (the kit fills them), and link every new node in the same
   answer.

## The plain-text form

The plain-text form of one question:

```
stage 0 frame | quick start 1/5 | answered 1, skipped 0
<the ask>
Why: <one line>
Example answer: "<an example>"
1. <option> (recommended)
2. <option>
Or answer in your own words, or say skip, later, n/a or why?
```

A short example, three agent turns:

```
Agent: stage 0 frame | quick start 1/5 | answered 1, skipped 0
       Who are you, and what is your role in Community garden?
       Why: your role says whose view the first answers come from.
       Example answer: "I coordinate the plots and keep the watering rota."
       Or say skip, later, n/a or why?
User:  I'm a bed steward on the north plots.
Agent: Noted: you are a bed steward on the north plots (`role:bed-steward`).
       stage 0 frame | quick start 2/5 | answered 2, skipped 0
       What should a newcomer know about Bed steward? One or two sentences are enough.
       Why: a name alone says too little to use.
       Example answer: "A bed steward waters, weeds and logs the harvest of two beds."
User:  They look after beds.
Agent: stage 0 frame | quick start 2/5 | answered 2, skipped 0
       Can you give one example of what a bed steward does in a normal week?
```

The first answer was recorded in its own turn, and its result named the follow-up on the new node. The third turn
is the one probe and records nothing; the next turn records "They look after beds." and the reply to the probe
together.
