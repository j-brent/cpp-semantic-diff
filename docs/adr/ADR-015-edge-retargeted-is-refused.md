<!-- metadata
id: ADR-015
type: adr
kind: frozen
status: accepted
decided: 2026-08-02
author: Brent Runyan
-->

# ADR-015 · `EdgeRetargeted` is refused, not deferred

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-08-02.

## Context

ARCH-001's first draft listed `EdgeRetargeted`: an edge that now points from B to C. An edge has
no identity of its own; its id *is* `src|kind|dst`. Recorded in `ade9c40`.

## Decision

The diff never emits `EdgeRetargeted`. A changed endpoint is an edge removed plus an edge added.
`EdgeKindChanged` is emitted, because the kind is state while the endpoints are identity.

## Consequences

- "Retargeted from B to C" would claim B and C are the same entity: a node-identity judgement the
  one-to-one matcher already declined ([ADR-003](ADR-003-identity-pairing-is-one-to-one.md)).
  Removed plus added carries the same information without the invented pairing.

## Alternatives rejected

- **Emit it,** remaking a pairing decision in the diff on weaker evidence.
- **Defer it** as unbuilt work. It is wrong, not unfinished.
