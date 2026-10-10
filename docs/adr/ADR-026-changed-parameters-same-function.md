<!-- metadata
id: ADR-026
type: adr
kind: frozen
status: accepted
decided: 2026-10-10
author: Brent Runyan
supersedes: [ADR-002, ADR-020]
-->

# ADR-026 · A function whose parameters changed is the same function

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Accepted, 2026-10-10.

## Context

Tier-1 identity pairs entities by USR, and a function's USR includes its parameter types. So when a
parameter is added, removed or changed, the tool reports the old function removed and the new one
added, and every call to it twice. In GitHub Actions demo run 38044461310 (2026-10-10), all 11
removals on current spdlog were this case, from an ordinary API extension. On current fmt, so were
the top three change events in the ranking. ADR-020 rested on at most 5 pairable leftovers per repository, on data frozen
in August. Recorded in `b934fc1` as known issue 1.

## Decision

The old and new versions are the same function and should be paired. The change is reported as one
change event, such as "signature changed from X to Y", with its call sites filed under it. This
supersedes in part [ADR-002](ADR-002-tier-1-identity-only-in-v0.md) (tier 1 only) and
[ADR-020](ADR-020-do-not-build-identity-tiers-2-4.md) (no better matching).

How to pair them is **not decided**:
- **Overloads.** Which old one corresponds to which new one, and whether an ambiguous pairing is
  made at all. The project's rule so far is never to force a match that might be wrong
  ([ADR-003](ADR-003-identity-pairing-is-one-to-one.md)).
- **Scope.** Parameters only, or also return type and qualifiers such as `const` and `noexcept`.
- **Templates.** Three of the spdlog cases are class-template constructors.

## Consequences

- A reviewer is told an API was extended, not deleted, and is asked whether every caller was
  updated.
- Prerequisite for a change event that flags callers affected by a signature change (known issue 5).

## Alternatives rejected

- **Keep removal plus addition** (ADR-020). It misdescribes the change, inflates the counts, and
  ranks the noise highest.
