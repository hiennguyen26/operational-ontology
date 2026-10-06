# Stage C: how the topics meet

Stage 8 opens when a topic has imports. It is done when `q.compose.meet` is answered and every import pair has at
least one bridge. Until then `onto_next` ranks its questions high, and `onto_gaps` shows an `unbridged_import` gap.

## The questions

Ask these in order, in plain words, with the parents' names filled in:

1. **Which outputs of A feed which processes of B?** (`q.compose.meet`) The garden's harvest log feeds the kitchen's
   menu planning.
2. **What is the new topic for?** The goal of the composed topic, in the user's words (`q.frame.goal`): "a weekly
   harvest menu: the kitchen cooks what the garden picks each week".
3. **How will you know it works?** A metric that `measures` the goal: "the share of local produce on the menu".
4. **Which things are the same?** Confirm or reject each `same_as` candidate from `onto_import action=suggest`.
5. **Where do they disagree?** For each `conflict` gap, ask which value holds here. Answering its question
   (`onto_answer q=q.gap.conflict.<field>@<id>`) records the decision, scoped `<id>#attrs.<field>`; a decision
   recorded with `onto_decide` needs that scope too. Only a decision with that scope settles the conflict, and a new
   disagreeing value opens it again.

## Turning answers into bridges

A bridge is an ordinary edge whose ends sit in different namespaces (the new topic counts as one). It needs
provenance like any other edge: the quote from the answer.

```json
{"op": "add_edge", "basis": "stated",
 "edge": {"src": "kitchen/process:menu-planning", "rel": "consumes", "dst": "garden/dataset:harvest-log"},
 "prov": [{"loc": "Q:q.compose.meet", "quote": "Menu planning in the kitchen reads the garden's harvest log",
           "by": "user"}]}
```

Good bridges carry meaning: `consumes`, `produces`, `serves`, `measures`, `owns`. `same_as` is for identity only
(the garden's tomato is the kitchen's tomato); `related_to` is a last resort.

## Local nodes of the composed topic

The composed topic owns what exists only because the parents meet: its goal (`goal:weekly-harvest-menu`), the
metrics that measure it, and any process that spans both. Link each one to the imported nodes it depends on, for
example `kitchen/deliverable:weekly-menu serves goal:weekly-harvest-menu`.

## Decisions

A choice made while composing (when the menu locks, which release to pin) is a decision with a scope that names the
imported ids or namespaces (`kitchen/deliverable:weekly-menu`, `garden/`). Briefs and context calls list it for
everyone who reads that scope.
