<!-- metadata
id: ADR-037
type: adr
kind: frozen
status: pending Brent Runyan's review
decided: 2026-10-11
author: agent review during v0 orchestration, pending Brent Runyan's review
-->

# ADR-037 · Only files git tracks at a revision are modelled

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Pending Brent Runyan's review — decided by agent review during v0 orchestration, 2026-10-11. Becomes Accepted when the v0 branch merges to main.

## Context

The build directory is inside the worktree: `acquire/workspace.py` configures into
`<worktree>/build`. It can hold generated headers and dependencies CMake downloads
(`build/_deps`). Extraction keeps every declaration under the repository root, so a downloaded
library would be modelled as first-party code. That is the defect [#3](https://github.com/j-brent/cpp-semantic-diff/issues/3) fixes for third-party code
the repository contains, reached through files it does not contain.

[PLAN-004 §3.1](https://github.com/j-brent/cpp-semantic-diff/blob/62dc064/docs/plans/PLAN-004-third-party-boundary.md#31--recognizing-third-party-code) proposed a rule and left it to step 1's measurements. On fmt `HEAD~12`, spdlog
`HEAD~30` and leveldb `HEAD~9..~1`, no node comes from an untracked file
([step 1 report §2](../../reports/2026-10-10--plan-004-baseline/measurements.md#2--nodes-per-label)).

## Decision

Only files git tracks at the revision under analysis are modelled. Declarations in any other
file under the worktree are outside the model, like declarations outside the repository.
Relationships into them are boundary crossings labelled `untracked`.

Third-party paths take precedence: a file in a submodule is labelled with the submodule's path,
although the superproject does not track it.

## Consequences

- Downloaded dependencies and generated declarations cannot enter the model as first-party code.
- No fixture result changes: the rule removes no node from any of the three.
- The fixtures do not exercise the rule, so its tests need a repository with an untracked header.
- Extraction needs each revision's tracked-file list, read from the revision's tree
  (`git ls-tree`). The per-TU cache key does not contain the revision: it hashes each header's
  content under its repository-relative path. A header untracked at one revision and committed
  unchanged at another would hash the same both times, so the key also records whether git
  tracks each repository header the TU reads. Tracking a file the TU does not read changes
  nothing in its key.

## Alternatives rejected

- **No rule for now.** Also changes nothing today, but leaves the defect open with nothing to
  report it.
- **Exclude only `<worktree>/build`.** Simpler, but misses files generated or downloaded
  anywhere else in the worktree.
