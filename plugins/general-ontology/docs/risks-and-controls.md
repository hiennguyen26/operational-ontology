# Risks and controls

Read it before you rate a risk or settle a P23. It applies only with the `assessment` pack on.

**Risks and controls** (only with the `assessment` pack on). Stage 5 then asks what could go wrong, which dimensions the
topic rates on (regulatory, financial, reputational and customer are suggested), the ratings, who owns each risk and
what already reduces it (`q.risks.*`). Ratings stay draft until a named owner reviews them. A proposal that would be
refused for a P23 says so in `onto propose` and `onto review` ("would be refused if accepted"); a draft verdict leaves
`attrs.status` as it is, so keeping a risk draft means an edit that sets `attrs.status` to `draft`. `onto validate`
warns (W09) on a draft risk whose ratings do not add up, and reports a problem (P23) on a reviewed or approved one; a
write that would add a P23 is refused before anything is written. The answer result names each new W09, and the preview
of an answer that would approve such a risk already says it would be refused; an answer refused for a P23 stores
nothing, so the question stays open. Offer the four ways out: keep the risk draft, link an implemented control, raise
the residual, or rate the missing measures. A reviewed or approved risk must rate impact, inherent and residual on every
dimension it rates, and rate at least one; a P23 that says a measure "is not rated" is fixed by rating it. A control
counts for a dimension only when its `covers` list names that dimension or is empty (it then covers every dimension); a
P23 that names a linked control and says "its covers leaves out" a dimension is fixed by adding the dimension to that
control's `covers`. `onto erase` is never refused for a P23: the erase goes through and names the P23 it leaves as a
follow-up. Until that P23 is settled (one of the four ways out, with the user's choice), `onto build` is refused, so
`build/export.json` and `build/cards.json` still hold the erased text: settle it, rebuild, and never commit or push
`build/` before. Archive a risk that no longer applies, with a reason and a decision; never delete it.
