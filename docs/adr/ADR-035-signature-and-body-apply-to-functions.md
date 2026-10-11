<!-- metadata
id: ADR-035
type: adr
kind: frozen
status: accepted
decided: 2026-10-11
author: agent review during v0 orchestration
-->

# ADR-035 · Signature and body changes keep their names and state that they apply to functions

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-10-12. Decided by agent review during v0 orchestration, 2026-10-11; accepted by Brent Runyan by merging v0 to main.

## Context

Issue [#15](https://github.com/j-brent/cpp-semantic-diff/issues/15): most node change events can
happen to any kind of entity, but a signature change and a body change can only happen to a
function or a method. Only those have a signature or a code body. #15 offered two fixes: rename
the two kinds after functions, or add a category of change events specialized by entity kind.

Issue [#8](https://github.com/j-brent/cpp-semantic-diff/issues/8) needs a "signature changed"
change event ([ADR-026](ADR-026-changed-parameters-same-function.md)), and none existed. The
signature is a field of the node, not an attribute, so the attribute diff never reported it. A
changed return type was reported as `NodeAttrChanged(return_type)`, because the USR does not
encode the return type and tier 1 paired the two versions.

## Decision

- **Names keep the `Node` prefix.** The new kind is `NodeSignatureChanged(old_signature,
  new_signature)`, beside `NodeBodyChanged`. Where a kind applies is stated as data, not in its
  name: `APPLIES_TO` in `model/events.py`, mirrored by a table in ARCH-001 §6.1. Both kinds apply
  to `function` and `method`, which includes constructors, destructors and conversion
  operators. Every other node change event applies to every node kind. The diff stage reads the
  table rather than trusting the producer to leave the fields empty.
- **The signature has its own kind**, not `NodeAttrChanged(signature)`.
- **Where the signature changed, `NodeAttrChanged` is not reported for `return_type` or
  `const`.** The canonical signature is the function type, so it already spells both.
- **Side effect, accepted:** a change to the return type alone is now `NodeSignatureChanged`, not
  `NodeAttrChanged(return_type)`.
- **Call sites.** ADR-026 asks for the call sites to be "filed under" the change event. They are
  met this way: the call edges of a paired function map through the correspondence, so they
  produce no edge change events, and the output lists the head-side callers beside each
  `NodeSignatureChanged` as evidence (`callers` in the JSON, a "called by" line in the terminal).
  They are not change events, and nothing says whether each caller still compiles. That is a
  stopgap until derived change events for the callers exist
  ([#16](https://github.com/j-brent/cpp-semantic-diff/issues/16)).
- The cause rule `signature-change-breaks-callers` is keyed on `NodeSignatureChanged`. It stays
  dormant: its consequence is a derived change event, and none is emitted yet.
- Novelty 1.00, level with a removal: every caller was compiled against the old signature.

## Consequences

- The change event set grows by one kind, and the JSON gains `callers`. Both land under schema
  2.0.
- The exception specification is stripped from the signature for a separate reason (see
  `_signature` in `produce/extract.py`), so gaining `noexcept` stays
  `NodeAttrChanged(exception_spec)`.
- Not decided here: whether attribute changes should be specialized by entity kind.
  `NodeAttrChanged` is unchanged.

## Alternatives rejected

- **Rename them `FunctionSignatureChanged` and `FunctionBodyChanged`.** The name would hold a fact
  that belongs in a table, and every future restricted kind would need a naming rule of its own.
- **A category of change events specialized by entity kind.** A larger change to the closed set
  (ARCH-001 §6.1) for two members, and every consumer would switch on a second axis.
- **`NodeAttrChanged(signature)`.** The signature is not an attribute (ARCH-001 §4.1), and the
  question it raises, whether every caller was updated, is not one an attribute change raises.
- **Report `return_type` and `const` beside the signature change.** One change, counted twice.
