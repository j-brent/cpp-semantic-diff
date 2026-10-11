<!-- metadata
id: ADR-040
type: adr
kind: frozen
status: Pending Brent Runyan's review — decided by agent review during v0 orchestration, 2026-10-11. Becomes Accepted when the v0 branch merges to main.
decided: 2026-10-11
author: agent review during v0 orchestration
-->

# ADR-040 · Each caller of a changed signature is a derived change event, from a derive stage

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Pending Brent Runyan's review — decided by agent review during v0 orchestration, 2026-10-11. Becomes Accepted when the v0 branch merges to main.

## Context

Issue [#16](https://github.com/j-brent/cpp-semantic-diff/issues/16): when a function's signature
changes, each of its callers may be affected. `render(int)` becomes `render(double)` while a
caller still calls `render(2)`: the call compiles, and the argument is now converted silently.
The reviewer needs each such caller, filed under the signature change.

[ADR-035](ADR-035-signature-and-body-apply-to-functions.md) and
[ADR-036](ADR-036-functions-pair-by-name.md) report a changed signature as one
`NodeSignatureChanged`. A paired function's call edges map through the correspondence, so an
unchanged call produces no change event. As a stopgap the output listed the head-side callers
beside each signature change (`callers` in the JSON, "called by" in the terminal).

ARCH-001 named the change event `SignatureIncompatible` and wrote its cause rule,
`signature-change-breaks-callers`, but never designed its contents, and no stage could emit it:
it is derived, and §13 put derived change events out of v0. Diff may not compute what the models
do not state, and classify may not create a change event.

## Decision

- **The kind is `CallerOfChangedSignature`**, renamed from `SignatureIncompatible`. Kinds state
  observations. Whether a call still compiles, or behaves the same, is a verdict no stage makes.
- **Subject: the head call edge G → F.** So the change event's home is the caller G
  (`rank.subject_home`), and its provenance is the call sites, on both sides.
- **Only calls on both sides**: a head `calls` edge G → F where F has a `NodeSignatureChanged`,
  and a base `calls` edge whose endpoints map to G and F through the correspondence. A new
  caller, or a new call from an old caller, is left out: it is already an `EdgeAdded`, and it was
  written against the new signature.
- **Payload:** `caller`, `callee`, `old_signature`, `new_signature`, and `caller_edited`: whether G
  has its own `NodeBodyChanged` or `NodeSignatureChanged` in this diff. An unedited caller is the
  one the text diff never shows the reviewer.
- **Cause:** `NodeSignatureChanged(F)`, by `signature-change-breaks-callers`, now active. The
  locator is the subject edge's head destination, so each caller change event has one candidate
  cause, and a caller of two changed functions gets two change events.
- **Novelty 0.70**: below the signature change that causes it, above a body change.
- **Reading order:** under the signature change, unedited callers first, then edited ones, each
  in ranking order. The JSON has no cap and no aggregation; the terminal shows the first ten
  callers under each change and says how many more there are.
- **A derive stage**, between diff and classify. Input: the diff's change events, both models
  and the correspondence. Output: the same change events plus the derived ones. It sets no
  `caused_by`. Diff and classify keep their contracts.
- **The one exception to "derived change events are out of v0"** (ARCH-001 §13, under
  [ADR-030](ADR-030-what-v0-is.md)). The other derived kinds stay out.
- **The stopgap caller list is removed** from the JSON and the terminal view. These change
  events replace it.

## Consequences

- **The caller list is a lower bound.** It has only the calls the model has as edges. Calls that
  depend on a template parameter and calls from initializers or default arguments are missing
  (PLAN-006 step 8, [#13](https://github.com/j-brent/cpp-semantic-diff/issues/13)), and so are
  calls through a function pointer. Their callers get no change event, and nothing counts them.
- **Interaction with uncovered changes** ([#6](https://github.com/j-brent/cpp-semantic-diff/issues/6)).
  These change events never explain a changed token on their own: an unedited caller's call site
  did not change, and an edited caller's is already covered by its own change event. So they
  neither shrink nor grow the uncovered count. A missed caller is not a changed line either, so
  the uncovered count does not measure the lower bound above.
- One signature change can bring many change events. They collapse under it in every view, and
  `summary.events_reported` counts them all.
- The JSON loses `DiffDocument.callers` and gains the change event kind and `meta.timings.derive`,
  under schema 2.0.
- [#10](https://github.com/j-brent/cpp-semantic-diff/issues/10) is not needed
  ([ADR-041](ADR-041-v0-scope-of-steps-6-and-7.md)).

## Alternatives rejected

- **Keep the name `SignatureIncompatible`.** It claims the call no longer fits, which nothing
  checks.
- **Make the caller the subject.** A caller of two changed functions would need two change events
  on one subject, told apart only by payload, and the cause locator would have to guess.
- **Report new callers too.** A new call was written against the new signature, and is already
  reported as an `EdgeAdded`.
- **Emit the change events in diff, or synthesize them in classify.** Diff would compute what the
  models do not state, and classify would create change events, which its contract forbids.
- **Keep the caller list beside the change events.** Two records of one fact.
- **Cap or aggregate callers in the JSON.** JSON is the product; a view may shorten the list, the
  document may not.
