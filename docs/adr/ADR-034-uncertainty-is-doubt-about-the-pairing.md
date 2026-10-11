<!-- metadata
id: ADR-034
type: adr
kind: frozen
status: Pending Brent Runyan's review — decided by agent review during v0 orchestration, 2026-10-10. Becomes Accepted when the v0 branch merges to main.
decided: 2026-10-10
author: agent review during v0 orchestration
-->

# ADR-034 · Uncertainty is doubt about the pairing, not the absence of a partner

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Pending Brent Runyan's review — decided by agent review during v0 orchestration, 2026-10-10. Becomes Accepted when the v0 branch merges to main.

## Context

Uncertainty, one of the four risk-score factors ([ADR-005](ADR-005-low-confidence-ranks-up.md)),
is meant to say *the tool may have paired this wrongly*. It said *this entity has no partner*: every
unpaired entity scored the maximum, 1.0, and so did every edge change event touching one. Every
genuine addition and removal has no partner by definition, so each got a boost meant for doubt.
Issue [#17](https://github.com/j-brent/cpp-semantic-diff/issues/17).

## Decision

Uncertainty means the pairing may be wrong. The match stage records why each leftover is unpaired,
and rank values the reason:

| Reason (`Confidence.unpaired`) | When | Uncertainty |
|---|---|---|
| `none` | no unpaired entity of the same kind under the same parent on the other side | 0.0, as a tier-1 match |
| `candidate` | at least one such entity: ARCH-001 §5's tier-4 candidate set, counted, not scored | 0.5 |
| `ambiguous` | a tier found partners and refused to choose, such as a key collision in tier 1 | 1.0 |

"Same parent" is asked through the matches: a base parent stands for its head partner. Children
of an unpaired parent are never candidates. A key collision is `ambiguous` only if the other side
has the key too. The +0.25 for incomplete models still applies.

`Confidence.unpaired` is set exactly when `tier` is None. `tier` keeps its meaning: None when the
subject, or either endpoint of an edge, is unpaired. An edge event's `unpaired` is the more doubtful
of its unpaired endpoints'.

## Consequences

- A plain addition or removal, and the edges to it, no longer rank above an equal change to a
  paired entity. Possible renames (`candidate`) and refused pairings (`ambiguous`) still rank up.
- The JSON changes: `events[].confidence.unpaired` is new, and `scores[].factors` uncertainty
  values and `detail` change meaning. It lands with the other v0 schema changes under 2.0.
- With `tier` None, an edge does not carry the tier of its paired endpoint. An edge from a tier-4
  match to a plain addition scores 0.0, not 0.75. It cannot happen in v0, where only tier 1 runs
  and tier 1 has no doubt. Revisit when tiers 2–4 land.
- High-risk stays judged on impact (`rank/score.py`). Its original cause, a constant offset on
  every addition and removal, is gone; the reason it gives still holds.

## Alternatives rejected

- **Keep 1.0 for every unpaired entity.** Treats being new as being doubtful.
- **Score candidates by similarity.** That is tier 4, which v0 does not build
  ([ADR-020](ADR-020-do-not-build-identity-tiers-2-4.md)); counting is enough to say doubt exists.
- **Give an edge event the tier of its paired endpoint.** Changes what `tier` means: an
  unpaired endpoint would hide behind the paired one.
