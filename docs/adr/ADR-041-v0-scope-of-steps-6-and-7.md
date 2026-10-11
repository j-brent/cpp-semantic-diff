<!-- metadata
id: ADR-041
type: adr
kind: frozen
status: accepted
decided: 2026-10-11
author: Brent Runyan
supersedes: [ADR-030]
-->

# ADR-041 · v0 takes none of PLAN-006 step 6, and nine of step 7's ten catalog cases

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-10-11.

## Context

[ADR-030](ADR-030-what-v0-is.md) put into v0 "as much of steps 6 and 7 as issues 4 and 5 need",
issues 4 and 5 being roadmap issues [#15](https://github.com/j-brent/cpp-semantic-diff/issues/15)
and [#16](https://github.com/j-brent/cpp-semantic-diff/issues/16). That left the scope of
[#10](https://github.com/j-brent/cpp-semantic-diff/issues/10) (step 6, the uses relationship) and
[#11](https://github.com/j-brent/cpp-semantic-diff/issues/11) (step 7, compare more on the entities
that exist) undefined:

- **#16 needs nothing from step 6.** It needs the callers of a function whose signature changed,
  and those come from the `calls` edges the model already has. A function passed by address whose
  signature changes fails to compile, so it is not the silent risk #16 is about.
- **#15 needs nothing from step 7.** It is about naming only. Tying step 7 to #15 was a drafting
  slip.

## Decision

This supersedes ADR-030's sentence on steps 6 and 7. The rest of ADR-030 stands.

**Step 6 (#10) is out of v0.**

**Step 7 (#11) is in v0, for nine of its ten catalog cases** in `tests/test_change_catalog.py`:
`default-argument-value`, `constructor-initializer-list`, `default-member-initializer`,
`constexpr-removed`, `class-becomes-final`, `nodiscard-added`, `deprecated-added`,
`base-becomes-virtual` and `base-order`.

**Out of v0:**
- `field-order`, and the layout facts: field offset, class size and alignment. Offsets cascade:
  insert one field and every later field's offset changes, and so does the size of every class
  that contains it. Each cascade needs a cause rule
  ([ADR-023](ADR-023-arch-lists-every-cause-rule.md)). Moved to
  [#34](https://github.com/j-brent/cpp-semantic-diff/issues/34).
- `inline` and `consteval`. No catalog case covers them.

## Consequences

- v0 reports a changed constant or enumerator without the entities that use it.
- An added enumerator is not checked against the `switch` statements over its enum.
- The catalog's step 6 case, `callback-replaced`, stays an expected failure in v0, as does
  `field-order`.
- #16 no longer depends on #10.

## Alternatives rejected

- **Keep the part of step 6 that #16 was thought to need.** #16 needs none of it.
- **All ten step 7 cases in v0.** `field-order` brings the layout facts with it, and every cascade
  they start needs its own cause rule.
