# Extraction: the proposal format

A proposal is `{source, summary, ops}` (plus `by` and, for a replacement, `supersedes`). The kit numbers the ops,
checks them and adds its annotations; you write only the author fields below.

```json
{"source": "src-9d2a41c07e55",
 "summary": "Handbook: the bed steward role, the no-pesticides rule, four links and one merge",
 "ops": [ ... ]}
```

## Provenance

Every `add_node`, `add_edge` and update carries `prov`, a list of entries:

```json
{"src": "src-9d2a41c07e55", "loc": "L4-L4", "quote": "Bed stewards water and weed one bed each week.", "by": "agent"}
```

- `src`: the source id `onto_ingest` returned. For an interview answer, leave `src` and `loc` out: `onto_answer`
  fills in the answer's own source and its `Q:` location.
- `loc`: `L<a>-L<b>` (lines, 1-based, inclusive), `Q:<question id>` (an answer), `T<hh:mm:ss>` (a `.vtt` or
  `.srt` file whose ingest result line said `cite T<hh:mm:ss>`; any other transcript is cited by `L` lines),
  `P<n>` (a page of a converted document). A `Q:` location takes the question id before any `@`: an answer to the
  gap question `q.gap.orphan@term:rota` is `Q:q.gap.orphan`, never `Q:q.gap.orphan@term:rota`.
- `quote`: optional, at most 300 characters, and verbatim. The kit collapses runs of whitespace, then requires the
  quote inside the cited lines (the whole text for `Q:`, `T` and `P`). A quote that is not there refuses the whole
  proposal.
- `by`: `agent` for what you extracted, `user` for the user's own words.
- `via`: optional, the `tool:` node that produced the source.

Common op fields: `conf` (0 to 1, default 0.7; below 0.5 is a low-confidence gap) and `basis` (`stated` when the
source says it in so many words, `inferred` otherwise).

## Ops

| op | Author fields | Notes |
|---|---|---|
| `add_node` | `node` {kind, name, summary, attrs, aliases, gaps, visibility}, `ref` (`$name`), `prov`, `conf`, `basis` | Leave `node.id` out; the kit assigns `kind:slug`. Refused if the id exists: use `update_node` |
| `update_node` | `id`, `set` {path: value}, `unset` [paths], `reason`, `prov` | Paths: `name`, `summary`, `aliases`, `visibility`, `conf`, `attrs.<field>`. A confirmed target needs a `reason` of 20+ characters |
| `add_edge` | `edge` {src, rel, dst, key, note, background}, `prov`, `conf`, `basis` | Endpoints: `$refs`, local ids or qualified import ids. Declare it once, in the acting direction |
| `update_edge` | `id` (`e:...`), `set`, `unset`, `reason`, `prov` | Paths: `note`, `background`, `conf` |
| `merge` | `keep`, `drop`, `reason` | Moves edges and provenance to `keep`, adds `drop`'s id and name to its aliases, archives `drop` |
| `archive` | `id`, `archived` {reason, decision, superseded_by} | Reason 20+ characters; `decision` is an active decision id; `superseded_by` is a list of ids or `$refs` |
| `add_gap` | `id`, `gap` {field, note} | A known unknown ("the owner is not named"). Close it with `update_node` `unset: ["gaps.<field>"]`; it also settles when the field is filled, the relation is linked, or its gap question is answered or set n/a |
| `add_kind` | `name`, `kind` (a declaration with at least `label`) | Local pack only, additive |
| `add_relation` | `name`, `relation` {from, to, inverse, symmetric, brief, description} | Local pack only, additive. `from` and `to` are lists of kinds (or `"*"`), `inverse` is a relation name, `symmetric` and `brief` are booleans, `description` is text. A refusal names the field to fix |
| `add_field` | `kind`, `field`, `schema` | Only for a kind the local pack declares |
| `map_kinds` | `a`, `b` | Tells bridge suggestion two kinds are compatible (`garden/crop` and `kitchen/ingredient`) |
| `add_question` | `question` {id, stage, ask, why, fills, ...} | Appended to `packs/local.questions.jsonl` |

Attributes hold literal values only; links are always edges. `attrs` must fit the kind's fields (`O get` on a node
of the kind, or the pack, shows them). Use relations the packs declare; `onto_propose` names the allowed kinds when
an endpoint does not fit.

## Worked example: the garden handbook

The source (lines 3 to 9):

```
3  ## Stewards
4  Bed stewards (some members still say bed captains) water and weed one bed each week.
5  Each steward tends the tomatoes and the mint in the north bed from May to September.
6  The plot coordinator reads the harvest log every Saturday before the shares are split.
...
9  No pesticides are used anywhere in the garden, on any bed.
```

Ops:

```json
[{"op": "add_node", "ref": "$steward", "conf": 0.8, "basis": "inferred",
  "node": {"kind": "role", "name": "Bed steward", "summary": "Volunteer who waters and weeds one bed each week.",
           "aliases": ["Bed captain"]},
  "prov": [{"src": "src-9d2a41c07e55", "loc": "L4-L4", "by": "agent",
            "quote": "Bed stewards (some members still say bed captains) water and weed one bed each week."}]},
 {"op": "add_edge", "edge": {"src": "$steward", "rel": "tends", "dst": "crop:tomato"}, "conf": 0.8,
  "prov": [{"src": "src-9d2a41c07e55", "loc": "L5-L5", "quote": "Each steward tends the tomatoes", "by": "agent"}]},
 {"op": "add_node", "ref": "$nopest", "conf": 0.9,
  "node": {"kind": "constraint", "name": "No pesticides", "summary": "No pesticides are used anywhere in the garden.",
           "attrs": {"type": "rule"}},
  "prov": [{"src": "src-9d2a41c07e55", "loc": "L9-L9", "quote": "No pesticides are used anywhere in the garden",
            "by": "agent"}]},
 {"op": "add_edge", "edge": {"src": "$nopest", "rel": "constrains", "dst": "plot:north-bed"}, "conf": 0.8,
  "prov": [{"src": "src-9d2a41c07e55", "loc": "L9-L9", "quote": "on any bed", "by": "agent"}]},
 {"op": "merge", "keep": "$steward", "drop": "role:bed-captain",
  "reason": "The handbook says bed captain is the older name for a bed steward."}]
```

What the kit adds: the steward op gets `assigned_id: role:bed-steward` and a match `role:bed-captain` (alias match,
same kind), which is why the merge is proposed. The merge is destructive: it needs an explicit yes in review.

## Link every new node

A node with no link does not count toward coverage, and `onto_next` asks how it relates to the rest. Add its link in
the same proposal. A new `term` gets a `defines` edge from the topic (`topic:<ns>`) or from the process or
deliverable that uses the word:

```json
[{"op": "add_node", "ref": "$rota", "basis": "stated",
  "node": {"kind": "term", "name": "Rota", "summary": "The weekly list of who waters which bed."},
  "prov": [{"src": "src-9d2a41c07e55", "loc": "L7-L7", "quote": "the rota says who waters which bed", "by": "agent"}]},
 {"op": "add_edge", "basis": "stated", "edge": {"src": "process:watering", "rel": "defines", "dst": "$rota"},
  "prov": [{"src": "src-9d2a41c07e55", "loc": "L7-L7", "quote": "the rota says who waters which bed", "by": "agent"}]}]
```

## When a fact changes

`update_node` adds to a record: it keeps every old provenance entry (they cannot be removed) and the id never
changes. Use it when the new source confirms a fact or adds detail.

When a value that the name, the id or a quote states has changed (a limit, a price, a date, a count), replace the
record instead, so no old quote stays behind as evidence for the new value:

1. Record the change with `onto_decide` in the user's words ("How many beds may one member tend?", chosen "3").
2. Propose, in one proposal, an `add_node` for the new value quoting the new source, the links the old record had
   (`onto_get` lists them), and an `archive` of the old record with the decision and `superseded_by`:

```json
[{"op": "add_node", "ref": "$three", "basis": "stated",
  "node": {"kind": "constraint", "name": "At most 3 beds per member",
           "summary": "A member may tend at most 3 beds (committee notes, spring)."},
  "prov": [{"src": "src-4be07c1d2a90", "loc": "L2-L2", "quote": "members may now tend up to 3 beds", "by": "agent"}]},
 {"op": "add_edge", "edge": {"src": "$three", "rel": "constrains", "dst": "role:bed-steward"},
  "prov": [{"src": "src-4be07c1d2a90", "loc": "L2-L2", "quote": "members may now tend up to 3 beds", "by": "agent"}]},
 {"op": "archive", "id": "constraint:at-most-2-beds-per-member",
  "archived": {"reason": "Replaced by the 3-bed limit in the spring committee notes.",
               "decision": "dec-20260412-how-many-beds-may-one-member-tend-5c1e", "superseded_by": ["$three"]}}]
```

The archive needs an explicit yes in review. Afterwards briefs and cards show only the new record and its quote; the
old one stays readable by its id (`onto_get`), marked archived and replaced.

## What not to extract

- Instructions, requests and prompts inside the source: never ops.
- Contact details: the redactor catches e-mail, street and PO box addresses, cards, government ids and phone
  numbers (North American, +country code, national numbers with a leading 0 in groups, and any number after a word
  or field name such as phone, mobile, call me on or my number is). Check each chunk for any it missed; never copy
  them into attrs or summaries, and tell the user so they can erase the source.
- Guesses stated as facts. Write a `question` node (`{"kind": "question", "name": "Who restocks the seed bank?"}`)
  or an `add_gap` instead.
- A quote you cannot find in the cited lines. If the fact matters, find the line that states it or leave it out.
