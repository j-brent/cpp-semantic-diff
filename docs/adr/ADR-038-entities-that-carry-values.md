<!-- metadata
id: ADR-038
type: adr
kind: frozen
status: Pending Brent Runyan's review — decided by agent review during v0 orchestration, 2026-10-11. Becomes Accepted when the v0 branch merges to main.
decided: 2026-10-11
author: agent review during v0 orchestration
-->

# ADR-038 · The entities that carry values are in the model, identified by their USR

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Pending Brent Runyan's review — decided by agent review during v0 orchestration, 2026-10-11. Becomes Accepted when the v0 branch merges to main.

## Context

ARCH-001 §4.1 lists enum, enumerator, alias and variable as node kinds, and the v0 extractor
produced none of them, so a changed enumerator value, global constant or type alias produced no
change event (PLAN-006 step 5, issue
[#9](https://github.com/j-brent/cpp-semantic-diff/issues/9)). Each needs an identity, the facts a
reviewer compares, and a novelty weight for each fact.

## Decision

**Identity is the USR**, as for every other kind. Tier 1 pairs these entities; the name tier
([ADR-036](ADR-036-functions-pair-by-name.md)) does not, because their USR holds nothing beyond
kind, name and scope.

**Enum** (`enum`): `underlying_type`, canonical; `scoped`, whether it is an `enum class`.

**Enumerator** (`enumerator`): `value`, the integer it stands for; `explicit_value`, whether the
value is written or implied by the one before. Contained by its enum.

- **No `order` attribute.** For an implied value, the value already carries the order: move an
  enumerator and its value changes. Where every value is written, order changes nothing a program
  can observe. Order as such is the field-order cascade
  ([ADR-041](ADR-041-v0-scope-of-steps-6-and-7.md), issue
  [#34](https://github.com/j-brent/cpp-semantic-diff/issues/34)).
- **Inside a template, `value` is None.** Clang computes no enumerator of an enum declared in a
  template: every one reads 0, even one written `= 4`.
- **One cause rule, `enumerator-implicit-value-shifted`.** An enumerator whose value is implied
  on both sides and changed is caused by the nearest change above it: an enumerator inserted,
  removed, or whose value changed. Changes farther up are linked by the enumerators between, one
  step per fact. A value written on either side is an edit of its own and is not explained.
  "Above" is source order, read from locations. Location orders siblings and nothing else; it is
  never identity (ARCH-001 §4.1).

**Weights** (`rank/weights.py`): `underlying_type` 0.95, `scoped` 0.90, `value` 0.95,
`explicit_value` 0.30.

## Consequences

- `enumerator-value` and `enumerator-added` in the change catalog pass.
- Inserting an enumerator reports one addition, with the value changes it caused under it.
- An unnamed enum's USR is named after its first enumerator (`c:@Ea@Z`), so renaming or inserting
  a first enumerator reports the whole enum as removed and added. That is tier 3's case (ARCH-001
  §5), which v0 does not build.
- A changed value is reported without the code that uses it, and an added enumerator is not
  checked against the `switch` statements over its enum: both need the uses relationship, out of
  v0 ([ADR-041](ADR-041-v0-scope-of-steps-6-and-7.md)). ARCH-001 §6.3 records both rules for when
  it lands.

## Alternatives rejected

- **Identify an enumerator by its enum and position.** An insertion would then read as every
  later enumerator renamed.
- **Three cause rules, one per kind of cause.** The first rule to find a cause wins, so a farther
  insertion would win over a nearer value change. One rule tries every kind at each enumerator,
  nearest first (`CauseRule.also_caused_by`).
- **Link every shifted value straight to the insertion.** Correct for one insertion, and wrong
  once two changes stand above: each value is the one before plus one, so the nearest change is
  what it follows.
