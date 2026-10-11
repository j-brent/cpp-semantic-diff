<!-- metadata
id: ADR-045
type: adr
kind: frozen
status: Pending Brent Runyan's review — decided by agent review during v0 orchestration, 2026-10-11. Becomes Accepted when the v0 branch merges to main.
decided: 2026-10-11
author: agent review during v0 orchestration
-->

# ADR-045 · Same-named statics and unnamed records stay as they are in v0

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Pending Brent Runyan's review — decided by agent review during v0 orchestration, 2026-10-11. Becomes Accepted when the v0 branch merges to main.

## Context

Issue [#7](https://github.com/j-brent/cpp-semantic-diff/issues/7) covers three identity
problems: parameter types ([ADR-036](ADR-036-functions-pair-by-name.md)), inline namespaces
([ADR-044](ADR-044-inline-namespace-bump-pairs.md)), and a third: unnamed records, whose USR is
unstable, and two same-named `static`s in same-named files, which merge into one node.
[ADR-022](ADR-022-same-basename-statics-merge.md) left the statics merged "until this step
decides otherwise". #7 is done when every `step 4` case in the change catalog passes, and no
case covers the third problem.

## Decision

Leave both as they are in v0. ADR-022 stands, with its trigger: an observed instance. Issue
[#49](https://github.com/j-brent/cpp-semantic-diff/issues/49) counts the collisions where
`merge_node` sees them, so the trigger can fire.

## Consequences

- #7 closes on its two measured cases. The third stays open, as ADR-022 and #49.
- An unnamed record whose USR changes still reads as a removal plus an addition.
- Fixing the statics still means keying the model on `(usr, tu)`, through every shared
  contract. That cost is only paid once the count shows it happens.

## Alternatives rejected

- **Fix both in #7.** Neither has an observed instance or a catalog case, and the statics fix
  reaches every node id, edge, cache entry and the JSON contract.
- **Leave it implicit.** ADR-022 says this step decides, so not deciding would leave it unclear
  whether anyone did.
