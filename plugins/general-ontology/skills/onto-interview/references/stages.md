# Stages and the quick start

Stage and progress are derived from the interview log and the graph, never stored. `onto_status` shows the current
stage; `onto_next` ranks the questions. The quick start's open questions come first, then the main kinds
(`q.vocab.kinds`). After them, the questions of the
earliest unfinished stage get a lift, but a gap question on one record (a missing field or relation, most often near
the goal or on a well-connected record) can rank above them, so the stage `onto_status` names is not always the one
asked next. Ask in the kit's order; it does not have to follow the stage numbers.

## The quick start (stage 0, about 5 minutes)

Five questions, asked first, in this order when the user has no preference:

| Id | Asks about | Usually records |
|---|---|---|
| `q.frame.you` | Who you are and your role | a `role` (or `person`, kept local) and a `part_of` edge to the topic (`works_on` does not reach a topic) |
| `q.frame.goal` | The goal | a `goal`, and a `metric` that `measures` it when the user says how success is judged |
| `q.frame.deliverable` | The first deliverable and who reads it | a `deliverable` that `serves` its readers, or links to what it needs |
| `q.people.key` | The key people or roles | `role` nodes, `owns` and `works_on` links, often a `process` |
| `q.data.where` | Where the data lives | `dataset` or `tool` nodes with `location` and `format` |

Stage 0 is done when all five are answered, skipped or marked n/a.

## Right after the quick start: the main kinds

`q.vocab.kinds` (stage 3, vocabulary; not a quick question) comes next, before stage 1 and before any gap question
on one record, until it is answered, skipped or n/a: "What are the main kinds of things in {topic}? Name a few,
with one example of each." The kinds the user names become the topic's local kinds (`add_kind` ops, which wait for
`confirm=true`), with one node per example. In the garden: "Beds, crops and harvests" gives the kinds `bed`, `crop`
and `harvest` and the nodes North bed, Tomatoes and July harvest.

## Stages

| Stage | Name | Goal | Done when |
|---|---|---|---|
| 0 | frame | Who asks, what for, the first deliverable | the quick start is answered, skipped or n/a |
| 1 | people | Roles, people (kept local), organizations, who owns what | every mapped dimension reaches `policy.stage_done_at` (0.6 by default) or is n/a, or a `skip_stage` answer exists |
| 2 | data | Datasets, tools, where they live, who keeps them current | same rule; sources cited at least once count toward data |
| 3 | vocabulary | The words the topic uses, as `term` nodes, each with a `defines` edge from the topic or from what uses the word | same rule |
| 4 | process | Recurring processes and their steps, who runs them | same rule |
| 5 | constraints | Rules, limits and requirements, and what they constrain; premises (`q.constraints.premises`: what the plan assumes is true), linked with `rests_on`; with the `assessment` pack, risks and controls (`q.risks.*`) | same rule |
| 6 | deliverables | Deliverables and the metrics that measure goals | same rule |
| 7 | questions | Open questions and claims still to check | same rule |
| 8 | compose | How the imported topics meet (only with imports) | `q.compose.meet` is answered and every import pair has a bridge |
| 9 | deepen | Gap-driven questions, never done | never |

Coverage of a dimension is the number of substantive nodes of its kinds (a summary, at least one source, at least one
link) over the dimension's target, capped at 1. A thin node (a name and nothing else) does not raise coverage,
and neither does a node with no link: add its link in the same answer (a term's `defines` edge, a dataset's owner).

## After the stages

`onto_next` keeps producing questions from gaps: a missing expected field, a missing expected relation ("Who keeps
Harvest log up to date?"), an orphan node, a draft hub to confirm, a low-confidence fact, a contradiction or a
`same_as` conflict. Questions near the goal and on well-connected nodes rank higher. Questions asked or skipped in
the last 2 days rank lower, and so do questions skipped often.

## Local questions

A topic can add its own questions with an `add_question` op in a proposal (they land in
`packs/local.questions.jsonl`). The garden demo adds `q.garden.plots`, `q.garden.crops` and `q.garden.watering`.
Use the same fields as the built-in banks: `id` (`q.<area>.<name>`), `stage`, `dimension`, `priority`, `ask`,
`why`, `fills`, `when` and `until` predicates, `follow_ups`, `options`, `repeatable`.
