<!-- metadata
type: conventions
kind: living
author: Brent Runyan
created: 2026-10-10
-->

# Contributing

*Living document: kept true as the project changes.*

How work on this repository is done. Extracted on 2026-10-10 from the frozen tracker
([`STATUS.md` §9](reports/2026-08-01--v0-build/STATUS.md#9--conventions)), with the documentation
and comment rules added.

---

## Documents

Every document is **living** or **frozen**, and says which: a `kind` field in its metadata block,
and a line under its title.

- **Living** documents describe now and stay true as the code changes. No two of them hold the
  same fact. A change updates the living documents it affects in the same commit.
- **Frozen** documents describe one moment, which they state, and are never edited afterward. The
  one exception is a "Superseded by …" line at the top. Link paths may be updated when a file they
  point to moves.

| Document | Kind | Holds |
|---|---|---|
| [`README.md`](README.md) | living | what the tool is, setup, how to run it |
| [`ROADMAP.md`](ROADMAP.md) | living | scope, order and the reasons for it; oversight that GitHub does not hold |
| `CONTRIBUTING.md` | living | this file |
| [`docs/ARCH-001-architecture.md`](docs/ARCH-001-architecture.md) | living | the design |
| `docs/plans/PLAN-00n-<topic>.md` | living while active | one roadmap item being worked on; deleted when done |
| `docs/adr/ADR-0nn-<topic>.md` | frozen | one decision, with its context, consequences and rejected alternatives |
| `docs/research/RESEARCH-00n-<topic>.md` | frozen | research |
| `reports/<date>--<topic>/` | frozen | reviews, measurements, findings |

- **Work and its status live on GitHub:** issues, the v0 milestone and the project board
  ([ADR-032](docs/adr/ADR-032-work-is-tracked-on-github.md)). No document tracks status.
- **Measured numbers live in frozen reports.** A living document links to the report rather than
  repeating the number.
- **Metadata block:** `id` (where the document has one), `type`, `kind`, `status`, `author`,
  `created`, `dependencies`. IDs are `RESEARCH-00n`, `ARCH-00n`, `PLAN-00n` and `ADR-0nn`, unique
  and never recycled. The file name starts with the ID.
- **A plan is deleted when it is done.** What it leaves behind goes into `ROADMAP.md`, ARCH-001,
  an ADR or a frozen report.

---

## Decisions

Every decision that constrains the design or the process is an **ADR** (architecture decision
record) in `docs/adr/`, one per file, numbered in the order decisions were made.

- **Sections:** Context, Decision, Consequences, Alternatives rejected. Short: enough to judge
  whether the decision still holds.
- **Metadata:** `id`, `type: adr`, `kind: frozen`, `status`, `decided` (the date the decision was
  made, not written), `author`, and `supersedes` / `superseded_by` where they apply.
- **Status:** accepted, superseded, superseded in part, or deprecated.
- **Frozen once accepted.** Only the status line ever changes. A decision that changes gets a new
  ADR, and the old one's status becomes "Superseded by ADR-0nn". A decision dropped without a
  replacement becomes "Deprecated".
- **No index.** The decisions in force are the ADRs whose status is accepted.
- **Other documents link to ADRs** rather than restating their reasons. Code comments may cite
  them, since they are frozen.

---

## Code comments

- **A comment explains why, in its own words.** It must make sense without opening any document.
  A citation is a pointer for further reading, never the explanation.
- **A comment may cite a frozen document, or ARCH-001** (`# ARCH-001 §4.3`), where a non-obvious
  choice traces to a design decision. **It never cites any other living document.**
- **A bare `§n` means ARCH-001.** Any other document is named: `RESEARCH-002 §3`,
  `v0 tracker §5 (reports/2026-08-01--v0-build/STATUS.md)`.
- **ARCH-001's section numbers are permanent.** A section that is removed leaves a stub saying it
  is retired and what replaced it. `tests/test_doc_citations.py` fails if a comment cites a
  section that does not exist, or cites a living document other than ARCH-001. When a section's
  meaning changes, search the code for its citations.
- **A measurement in a comment carries its date and the frozen report it comes from.** Otherwise
  it is reduced to the reasoning, and the number is dropped. A number asserted in prose has no
  owner and no expiry.

---

## Code

- **No hardcoded paths.** A tool that analyzes arbitrary repositories on arbitrary machines cannot
  carry assumptions about either. Ask the system, then fall back; document fallbacks as fallbacks.
- **Tests assert the design, not the implementation.** The merge-base test exists because getting it
  wrong is invisible in output; the fingerprint test exists because "a moved entity is the same
  entity" is a claim ARCH-001 makes; the cache-invalidation tests exist because a stale hit is the
  worst failure this system can have. Prefer tests of that kind. Do not pin float scores — it
  fossilizes weights that are meant to be tuned.
- **Reserve the shared contracts when work is split across concurrent agents.** `model/ir.py` and
  `model/events.py` are what every stage agrees on; letting several workstreams edit them
  concurrently produces four partial versions of one decision. Held as read-only with a
  stop-and-report rule, it caught four separate requests to change `Completeness` and `EventKind`,
  which were then done once as a coherent pass. **Commit coupled work together** — a stage emitting
  a new event kind and the ranker learning its weight are one commit, because either alone is a
  `KeyError`.

---

## Commits

- **Commit messages** say what changed and *why it matters*. Record findings discovered by running
  the code in a frozen report; every finding in the
  [v0 tracker's §5](reports/2026-08-01--v0-build/STATUS.md#5--findings-worth-keeping) was a real bug
  in waiting.
- Before committing, run
  `uv run ruff format . && uv run ruff check . && uv run mypy && uv run pyright && uv run pytest -q`.
