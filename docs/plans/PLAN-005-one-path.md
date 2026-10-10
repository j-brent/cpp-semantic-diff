<!-- metadata
id: PLAN-005
type: plan
kind: living
status: in progress
author: Brent Runyan
created: 2026-10-10
dependencies: [ARCH-001]
-->

# One path: living and frozen documents, one list of work

*Living document: kept current until it is done, then deleted.*

Approved 2026-10-10. Moves 1–7 and 9 are done; move 8 is next. Delete this file when every move in §3 is
committed.

---

## 1 · Why

The 2026-10-10 documentation pass had to correct 32 files, because:
- `STATUS.md` mixes 778 lines of history with current state;
- the same facts are repeated in several places;
- code cites `STATUS.md` by section number, and its sections keep moving.

Open work is in five places: `STATUS.md` §6 and §7, PLAN-003 §4, PLAN-002, `known-issues.md` and
`model-analysis.md`.

---

## 2 · The rule

Every document is either **living** or **frozen**, and says which: a `kind` field in its metadata
block, and a line under its title.

- **Living** documents describe now, and stay true as the code changes. There are few of them,
  and none repeats another.
- **Frozen** documents describe one moment, which they state, and are never edited afterward. The
  one exception is a "Superseded by …" line at the top. They may be cited by section, because
  their sections never move.

| Document | Kind | Holds |
|---|---|---|
| `README.md` | living | what the tool is, setup, how to run it. No measured numbers: it links to the report that has them. |
| `ROADMAP.md` | living | where things stand, in a page; **the one ordered list of open work**; the decisions in force, with reasons |
| `CONTRIBUTING.md` | living | conventions, including the citation rules in §2.1 |
| `docs/ARCH-001-architecture.md` | living | the design. Section numbers are permanent (§2.1). |
| `docs/plans/PLAN-00n-<topic>.md` | living while active | one roadmap item being worked on; deleted when done |
| `docs/research/RESEARCH-00n-<topic>.md` | frozen | research |
| `reports/<date>--<topic>/` | frozen | reviews, measurements, findings |

File names carry the document ID, so `ARCH-001` and `PLAN-004` can be found in a file explorer.

### 2.1 · Code comments

- **A comment explains why, in its own words.** It must make sense without opening any document.
  A citation is a pointer for further reading, never the explanation.
- **A comment may cite a frozen document, or ARCH-001. It never cites any other living
  document.**
- **ARCH-001's section numbers are permanent.** A section that is removed leaves a stub saying it
  is retired and what replaced it. A test fails if a comment cites an ARCH-001 section that does
  not exist. When a section's *meaning* changes, search the code for its citations.
- **A measurement in a comment carries its date and the frozen report it comes from.** Otherwise
  it is cut down to the reasoning, and the number is dropped.

---

## 3 · Moves

1. **Freeze `STATUS.md`.** It moves verbatim, as of the commit before this one, to
   `reports/2026-08-01--v0-build/STATUS.md`, with a frozen header. It was the tracker of the
   original implementation from 2026-08-01 to 2026-10-10.
2. **Draft `ROADMAP.md`** (§4).
3. **Extract `CONTRIBUTING.md`** from `STATUS.md` §9, plus the rules in §2.1.
4. **Rename files to carry their IDs** (`git mv`, links updated):

   | Today | Becomes |
   |---|---|
   | `docs/architecture/overview.md` | `docs/ARCH-001-architecture.md` |
   | `docs/research/cpp-analysis-tool-landscape.md` | `docs/research/RESEARCH-001-tool-landscape.md` |
   | `docs/research/semantic-diff-for-review.md` | `docs/research/RESEARCH-002-semantic-diff-for-review.md` |
   | `docs/plans/bitcoin-fixture.md` | `docs/plans/PLAN-002-bitcoin-probe.md` |
   | `docs/plans/third-party-boundary.md` | `docs/plans/PLAN-004-third-party-boundary.md` |

5. **Label every document** living or frozen (§2).
6. **Retire PLAN-003.** Its open parts go into the roadmap draft, awaiting review, and the file is
   deleted. Git keeps it, as it kept PLAN-001.
7. **Remove `known-issues.md`.** Its seven issues go into the roadmap as confirmed work, with
   their full text. The links to it from `project-review.md` and `the-model.md` are repointed
   once, at removal.
8. **Code comments** (§2.1):
   - the 43 citations of `STATUS.md` are removed, or repointed to the frozen copy where the
     reasoning depends on its evidence;
   - measurements get a date and a source, or are reduced to the reasoning;
   - the ARCH-001 citation test is added.
9. **`README.md`** drops its measured numbers and links to the frozen report for them.

Moves 1–7 and 9 are one commit. Move 8 touches code files in bulk and is its own commit, so it can
be reviewed separately.

---

## 4 · The roadmap draft

Two groups:

- **Confirmed.** Known issues 1–7, found 2026-10-10 and decided to be worked on. PLAN-004 is the
  plan for issue 7 and goes first.
- **Awaiting review.** Everything carried over: the open items in `STATUS.md` §6 and §7, PLAN-003
  §4, PLAN-002 and the decisions in `STATUS.md` §8. Each one is reaffirmed, changed or dropped in
  a review with Brent before the roadmap is final.

`model-analysis.md` adds nothing until Brent has reviewed it. It is then committed as a frozen
report.

---

## 5 · Not in scope

`docs/playground-B/` is untracked and excluded locally. It is left as it is.
