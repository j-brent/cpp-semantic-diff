<!-- metadata
id: ADR-043
type: adr
kind: frozen
status: Pending Brent Runyan's review — decided by agent review during v0 orchestration, 2026-10-11. Becomes Accepted when the v0 branch merges to main.
decided: 2026-10-11
author: agent review during v0 orchestration
-->

# ADR-043 · Default arguments and base order are attributes, and base order compares the bases on both sides

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Pending Brent Runyan's review — decided by agent review during v0 orchestration, 2026-10-11. Becomes Accepted when the v0 branch merges to main.

## Context

Issue [#11](https://github.com/j-brent/cpp-semantic-diff/issues/11) adds nine facts to the
compared state, in the scope [ADR-041](ADR-041-v0-scope-of-steps-6-and-7.md) set. Seven are
plain attributes. Two needed a decision:

- **Default arguments.** ARCH-001 §4.1 said the signature is normalized with "defaults
  included". It never was: the signature is the canonical function type, and a default
  argument is not part of a function's type. A changed default produced no change event.
- **Base order.** The `inherits` edges have no order, so a reorder produced nothing. An
  attribute holding the order would also change when a base is added or removed, which the
  `inherits` edges already report.

## Decision

- **Default arguments are the attribute `default_arguments`** on functions and methods, not
  part of the signature. The signature stays the function type. ARCH-001 §4.1 is corrected.
  The value lists the defaults in parameter order, joined `"; "`; `""` means none and None
  means not determined.
- **Base order is the attribute `base_order`** on classes, structs and unions: the canonical
  spelling of each base in the order written, joined `"; "`. It is read from the definition,
  and is None where a translation unit does not see one.
- **The diff reports `base_order` only where the bases present on both sides are in a
  different relative order.** A base added or removed without a reorder is the `inherits`
  edge event alone. A reorder alongside an addition is still reported. This follows
  [ADR-035](ADR-035-signature-and-body-apply-to-functions.md)'s precedent of a diff rule that
  keeps one change from being said twice.
- **No cause rule.** The suppression is a diff rule, not a link between change events, so
  ARCH-001 §6.3 gains no row ([ADR-023](ADR-023-arch-lists-every-cause-rule.md)).
- Novelty: `default_arguments` 0.85, `base_order` 0.90.

## Consequences

- A changed default argument is `NodeAttrChanged(default_arguments)`, and the function stays
  paired by tier 1: its USR does not change.
- A default is printed by clang, so a macro arrives expanded and an out-of-line definition
  carries the default its header declared.
- A base renamed in the same change is not on both sides, so it does not count as reordered.
- Defaults are compared again without the function's own namespace qualifiers. A change that
  only adds or drops one is marked spelling-only and never counted as high risk
  ([ADR-046](ADR-046-spelling-only-changes-are-never-high-risk.md)).

## Alternatives rejected

- **Put default arguments in the signature, as ARCH-001 said.** A changed default would be
  reported as `NodeSignatureChanged`, at novelty 1.00, which claims every caller may break.
  They still compile: they call the same function, and only the ones that omit the argument
  pass a different value.
- **Report every change to `base_order`.** Adding one base would be two change events: the
  edge and the order.
- **A cause rule linking the order change to the edge event.** It would hide a real reorder
  under an addition made in the same change.
