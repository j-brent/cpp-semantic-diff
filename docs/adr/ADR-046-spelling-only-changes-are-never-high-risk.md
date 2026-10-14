<!-- metadata
id: ADR-046
type: adr
kind: frozen
status: accepted
decided: 2026-10-11
author: agent review during v0 orchestration
-->

# ADR-046 · Spelling-only changes are reported but never high-risk

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-10-12. Decided by agent review during v0 orchestration, 2026-10-11; accepted by Brent Runyan by merging v0 to main.

## Context

Issue [#70](https://github.com/j-brent/cpp-semantic-diff/issues/70): on spdlog `HEAD~30`, 8 of
13 high-risk change events changed only how something is written, including the top two:

- 6 `sugared_signature` changes, where the canonical signature did not change
  ([ADR-038](ADR-038-entities-that-carry-values.md));
- 2 `default_arguments` changes that only dropped a namespace qualifier
  (`spdlog::details::os::default_eol` became `details::os::default_eol`). Clang prints a
  default as written ([ADR-043](ADR-043-default-arguments-and-base-order-are-attributes.md)).

They cleared the high-risk line on blast radius and exposure, not on novelty. Lowering a weight
does not stop that.

## Decision

- **A change of spelling only is never counted as high risk.** That covers `NodeAttrChanged`
  on an informational attribute (`sugared_signature`, `explicit_value`, `message`:
  `INFORMATIONAL_ATTRS` in `rank/weights.py`), and any change event whose payload has
  `spelling_only: true`. The gate is `RiskScore.is_high_risk`, and the score says so in
  `informational`.
- **The events stay listed and ordered as before.** Novelty, the score and the order do not
  change. Only `summary.high_risk_count` and the high-risk marker do.
- **A `default_arguments` change is compared again**, default by default. Where each old
  default is the new one with a leading qualifier added or removed, and that qualifier names a
  namespace the function is in, the event's payload has `spelling_only: true`. Stripping
  qualifiers from both sides is not enough: inside `ns::details`, `ns::x` and
  `ns::details::x` both reduce to `x` and name different entities. A leading `::` counts only
  together with such a qualifier, because `::x` and `x` can name different entities. This
  happens in the diff stage, so the model and the cache key do not change.

## Consequences

- spdlog `HEAD~30` reports 5 high-risk change events instead of 13.
- A dropped qualifier can in principle change which entity a default names, if a nearer scope
  declares the same name. That case is marked spelling-only too. It is still listed.
- A qualifier dropped inside a function body still changes the body hash and reads as
  `NodeBodyChanged`. v0 cannot tell it from a real body change.

## Alternatives rejected

- **Lower the novelty of these attributes.** They already sit at 0.30. Blast radius and exposure
  carry them over the line.
- **Leave the events out.** A spelling change is still a change in the diff, and the output
  reports every change event.
- **Normalize defaults in the extractor.** It would change the model and every cache entry, to
  answer a question only rank asks.
