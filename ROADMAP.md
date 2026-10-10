<!-- metadata
type: roadmap
kind: living
status: draft
author: Brent Runyan
created: 2026-10-10
-->

# Roadmap

*Living document: where things stand, the work ahead, and the order it is done in.*

**Draft.** §2 and §3 were reviewed with Brent on 2026-10-10. Their items will move to GitHub
issues ([ADR-032](docs/adr/ADR-032-work-is-tracked-on-github.md)), and this file will keep only the
order and the reasons for it.

The history behind every item is in the frozen
[v0 tracker](reports/2026-08-01--v0-build/STATUS.md) (2026-08-01 to 2026-10-10) and the frozen
[2026-10-10 review](reports/2026-10-10--up-to-speed/project-review.md). Conventions, including how
documents and code comments may cite each other, are in [`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## 1 · Where things stand

Seven of the eight pipeline stages in [ARCH-001 §3](docs/ARCH-001-architecture.md#3--pipeline)
exist, and `uv run cppsd diff <repo> --base <ref>` runs them end to end. `enrich`, which attaches
evidence from external analysis tools, is deferred past v0 by design (ARCH-001 §13). Identity is
tier 1 only.

The 2026-10-10 review found that the tool misreports ordinary changes in the ways listed in §2.
None of them is fixed. Measurements of the tool on real repositories are in the
[v0 tracker, §2–§4](reports/2026-08-01--v0-build/STATUS.md#2--the-measurement-this-project-exists-to-take).

---

## 2 · Confirmed work

Found by the 2026-10-10 review, and decided to be worked on. The text of each issue is as the
review recorded it; the evidence it cites is in
[`reports/2026-10-10--up-to-speed/demo-run-a/`](reports/2026-10-10--up-to-speed/demo-run-a/), the
outputs of GitHub Actions run
[38044461310](https://github.com/j-brent/cpp-semantic-diff/actions/runs/38044461310).

**Order.**

1. **Issue 7, third-party code outside the model:**
   [PLAN-004](docs/plans/PLAN-004-third-party-boundary.md). Issue 3 is done in the same schema
   change.
2. **The model:** [PLAN-006](docs/plans/PLAN-006-model-sufficiency.md), from the findings in
   [`the-model.md`](reports/2026-10-10--up-to-speed/the-model.md). Its step 1, the change catalog,
   is built. Issue 2 is its step 5. Which steps are in v0 is in ADR-030.
3. **Not yet ordered:** issues 1, 4, 5 and 6. Issues 4 and 5 depend on issue 1.

### Issue 1 · A function whose parameters changed is reported as deleted and re-added

**Status:** open. Decided 2026-10-10 that this should be fixed. It reverses part of the earlier
decision not to build better matching (the [v0 tracker](reports/2026-08-01--v0-build/STATUS.md) §3, "do not build tiers 2–4").

**What happens.** To pair an entity in the "before" model with one in the "after" model, the
tool uses clang's identifier for the entity (its *USR*). For functions, that identifier
includes the parameter types. So when a parameter is added, removed or changed, the old function
finds no partner. The tool then reports the old function as removed and the new one as added.
Every call to the function is reported twice as well: once as a removed call, once as an added
call.

**Seen in both demo runs:**
- **spdlog:** all 11 removals are this case. Every file-logger factory function
  (`basic_logger_mt`, `daily_logger_st`, `rotating_logger_mt`, …) and three file-sink
  constructors gained a trailing buffer-size parameter. The six highest-ranked change events are all
  removals of this kind, and 12 of the top 20 are one half of such a pair. The other 8 are
  genuinely new methods, such as `set_buffer_size`. With the change events for their calls, the
  pairs make up 58 of the 62 change events in the largest component.
- **fmt:** the top three change events in the ranking. `fmt::v12::detail::write_buffer` gained a third
  parameter: removed at 0.86 and re-added at 0.82, each with eight change events for its calls
  nested beneath it.

**Why it matters.**
- **It misdescribes the change.** A reviewer is told a public API was deleted, when it was
  extended.
- **It inflates the counts.** Each changed signature becomes two top-level change events plus
  a duplicated set of change events for its calls.
- **It ranks noise highest.** Removals get maximum novelty, and an unmatched entity gets maximum
  uncertainty, so these pairs score at the top by construction.
- **It hides the real question.** The question a signature change should raise is whether every
  caller was updated, and that question gets lost.

**Why the earlier decision doesn't cover it.** The decision not to build better matching rested
on measuring at most 5 pairable leftovers on any repository tested, with all the test data
frozen in August. Current upstream spdlog shows 11. They come from an ordinary API change, not a
large refactor.

**Expected behavior.** The old and new versions are recognized as the same function. The change
is reported as one change event, along the lines of "signature changed from X to Y". The call sites are
filed under it rather than duplicated.

**Questions for planning** (not decided):
- **Overloads.** When several functions share a name, which old one corresponds to which new one?
  If the pairing is ambiguous, should the tool still pair them, or leave them unpaired and say so?
  The project's rule so far is never to force a match that might be wrong.
- **Scope.** Does this cover only parameter changes, or also return type and qualifiers such as
  `const` and `noexcept`?
- **Templates.** Templates and constructors are affected too: three of the spdlog cases are
  class-template constructors.

### Issue 2 · Enums, global variables and type aliases are not in the model

**Status:** open.

The design specifies these entity kinds, but the v0 extractor never produces them. So the tool
cannot report the changes that live in them:
- a changed enum value;
- a new enum value that a `switch` does not handle;
- a changed global constant;
- a changed type alias.

All of these are common and worth a reviewer's attention. Details, sources and the wider question
of whether the model is sufficient are in [`the-model.md`](reports/2026-10-10--up-to-speed/the-model.md).

### Issue 3 · The tool says "event" where the agreed term is "change event"

**Status:** open. The term "change event" was decided on 2026-10-10, because "event" alone does
not say what happened.

The tool still uses "event" in:
- **the JSON output:** the fields `events`, `events_reported`, `top_level_events` and
  `vendored_events`;
- **the terminal output:** for example "580 lines changed -> 181 semantic events";
- **the code:** `Event` and `EventKind` in `src/cpp_semantic_diff/model/events.py`, and
  throughout;
- **the design documents:** ARCH-001 and `ROADMAP.md`.

Update the wording when the tool is next changed. Renaming JSON fields changes the output format,
so it needs a new schema version.

### Issue 4 · "Signature changed" and "body changed" apply only to functions, but are named as if they applied to any entity

**Status:** open. Raised 2026-10-10. The naming is not decided yet; [`project-review.md`](reports/2026-10-10--up-to-speed/project-review.md)
keeps the current names until it is.

Most entity change events (added, removed, renamed, moved, attribute changed) can happen to any
kind of entity. Two cannot:
- **Signature changed.** Only functions and methods have a signature. The extractor leaves it
  empty for every other kind (`_signature` in `src/cpp_semantic_diff/produce/extract.py`).
- **Body changed.** Only a definition with a code body gets a body fingerprint, which in practice
  means functions and methods (`body_hash` in the same file).

Here "methods" covers constructors, destructors and conversion operators, which the model also
classifies as methods.

Two ways to address it:
- **Rename them** "function signature changed" and "function body changed".
- **Add a category of change events specialized by entity kind**, separate from the ones that
  apply to every entity. "Attribute changed" may belong in that discussion too: it applies to
  every entity, but each kind has its own attributes. For example, a method has `virtual` and
  `const`, a field has `type` and `mutable`, and a class has `abstract`.

### Issue 5 · No change event tells a reviewer that a caller may be affected by a signature change

**Status:** open. Raised 2026-10-10.

When a function's signature changes, each of its callers may be affected. For example,
`render(int)` becomes `render(double)` while a caller still calls `render(2)`. The call still
compiles, but the argument is now silently converted, so the caller may behave differently. The
reviewer needs to see each such caller, filed under the signature change.

The design names this change event `SignatureIncompatible`
([ARCH-001 §6.1](docs/ARCH-001-architecture.md)). The link rule that would file it under the
signature change is already written (`signature-change-breaks-callers` in
[`classify/rules.py`](src/cpp_semantic_diff/classify/rules.py)), but it can never fire:
- **There is no "signature changed" change event to link from.** Today a signature change is
  reported as a removal plus an addition ([issue 1](#issue-1--a-function-whose-parameters-changed-is-reported-as-deleted-and-re-added)).
- **No stage produces `SignatureIncompatible`.** It is a *derived* change event: the tool has to
  work it out rather than read it off the two models. v0 leaves derived change events out on
  purpose, and what this one should contain has never been designed.

Fixing issue 1 and designing this change event would make the rule work.

### Issue 6 · Uncertainty treats every genuinely new or deleted entity as a doubtful pairing

**Status:** open. Raised 2026-10-10.

Uncertainty is one of the four risk-score factors (step 4B in [`project-review.md`](reports/2026-10-10--up-to-speed/project-review.md)).
It is meant to say *the tool may have paired this wrongly*, so that a doubtful pairing ranks
higher and a human checks it. In practice it says *this entity has no partner*: an unpaired
entity always scores the maximum, 1.0 (`UNCERTAINTY_BY_TIER` in
[`rank/weights.py`](src/cpp_semantic_diff/rank/weights.py)).

Every genuine addition and removal has no partner, by definition. So each one gets a boost meant
for doubt, and so does every relationship change event that touches it. In the example, nothing
about `log` is uncertain, yet "`log` added" and the added call to `log` both score 1.0. That will
not change once pairing is fixed ([issue 1](#issue-1--a-function-whose-parameters-changed-is-reported-as-deleted-and-re-added)):
better pairing only takes the false additions and removals out of this group.

The code already works around part of this. The high-risk count is judged on *impact*, which
leaves uncertainty out, because counting it made 40 % of change events on one fmt range
"high-risk" (the `is_high_risk` notes in [`rank/score.py`](src/cpp_semantic_diff/rank/score.py)).
The ordering still uses it, so an addition or removal still ranks above an otherwise equal change
to a paired entity.

Open question: what should uncertainty mean for an entity with no partner? For example, it could
be how likely it is that a partner exists but was missed, such as a similar entity on the other
side.

### Issue 7 · The tool analyzes third-party code instead of excluding it

**Status:** open. Decided 2026-10-10: in v0, third-party and vendored code must not change.
Only code this repository owns and contains is analyzed. This prerequisite is recorded in
[ARCH-001 §13](docs/ARCH-001-architecture.md#13--v0).

**Planned:** [`docs/plans/PLAN-004-third-party-boundary.md`](docs/plans/PLAN-004-third-party-boundary.md)
(PLAN-004). The three planning questions below were decided there on 2026-10-10.

**What the tool does today.**
- **It analyzes third-party code like the project's own.** It models, compares and scores
  vendored code exactly like first-party code.
- **It only pushes it to the end.** It recognizes vendored code from directory names such as
  `third_party`, `vendor` and `external`, plus known bundled libraries such as googletest
  (`VENDOR_SEGMENTS` in [`rank/exposure.py`](src/cpp_semantic_diff/rank/exposure.py)). It then
  ranks those change events last instead of leaving them out. That was a recorded decision
  (the [v0 tracker](reports/2026-08-01--v0-build/STATUS.md) §8, "Vendored code is partitioned out of the ranking, not merely down-weighted"),
  and the prerequisite above replaces it.
- **The output still carries them.** The JSON's `vendored_events` field and the terminal's
  "… more in bundled dependencies, ranked last" summary both report these change events.

**Why it matters.** On leveldb `HEAD~10`, a commit that updated the bundled googletest and
benchmark libraries produced 3,660 change events in that bundled code: 96 % of the total
(the [v0 tracker](reports/2026-08-01--v0-build/STATUS.md) §2, not re-measured in this review).

**Questions for planning.**
- **Parsing.** Third-party code still has to be parsed, because the project's own code calls
  into it, for example tests calling googletest. Should third-party entities stay in the model
  only as the far end of relationships, and never produce change events of their own?
- **A broken prerequisite.** What should the tool do when third-party code does change between the
  two points, as in the leveldb case: refuse, warn, or ignore the change? That same leveldb
  update also produced 134 change events in leveldb's own code, which the v0 tracker attributes
  mostly to test code that moved because googletest moved under it.
- **Recognizing third-party code.** Directory names are a guess. A copied library under an
  unconventional name is missed (the module notes in `rank/exposure.py` say so). An explicit
  list, given by the repository, may be needed.

---

## 3 · Carried over from the v0 tracker and PLAN-003

Reviewed with Brent on 2026-10-10. Sources: `T` is the
[frozen v0 tracker](reports/2026-08-01--v0-build/STATUS.md); PLAN-003, "Shipping v0", was retired
into this list and is in git at `4f083c1`.

| Item | Source | Outcome |
|---|---|---|
| `internal_linkage_residue` counts vendored code; components are numbered by a score that includes vendored events | T §6; PLAN-003 C | **Folded into PLAN-004**, as checks that it is done |
| Whether to build identity tiers 2–4 | T §3, §7 | **Dropped.** Issue 1 and PLAN-006 step 4 replace it |
| No cause rule for a method that becomes `virtual` | T §6; PLAN-003 C | **Dropped.** Revisit with issue 4 |
| Each parser worker takes ~0.6 s to start | T §6; PLAN-003 C | **Kept,** post-v0 |
| `cppsd cache gc` collects by age, not identity | T §6 | **Dropped** as a defect; an accepted design |
| `diff.renameLimit` can silently truncate the rename map | T §7; PLAN-003 C | **Kept, v0:** at least detect and report it |
| Unnamed records have an unstable USR; two same-named `static`s merge into one node | T §6, §7; PLAN-003 D | **Folded into PLAN-006 step 4** (identity). ADR-022 stands until step 4 decides otherwise |
| Four runtime strings cite the old tracker; false JSON field descriptions; the demo's "numerator" row | 2026-10-10 documentation pass | **Folded into PLAN-004's schema change** |
| Four cause rules are tagged as missing from ARCH-001 §6.3, and a test enforces it | 2026-10-10 documentation pass | **Kept,** small cleanup |
| ARCH-001 marked the JSON schema as done | 2026-10-10 documentation pass | **Fixed** in ARCH-001 §13 |
| A parallel-parsing test failed once under load | 2026-10-10 documentation pass | **Kept,** to investigate |
| Generated sources: ARCH-001 §3.1 states the problem, not the policy | T §7 | **Kept** as an open question, post-v0 |
| Show the model fingerprint; exposure using `Node.tu` | T §7 | **Dropped.** No use yet |
| Bitcoin Core probe ([PLAN-002](docs/plans/PLAN-002-bitcoin-probe.md)) | PLAN-002; PLAN-003 E | **Deferred** until the model is fixed and the confirmed issues are addressed |
| `enrich`, the evidence layer | T §1; ARCH-001 §8, §13 | **Post-v0,** by design |
| Where the reviewer reads the output | T §7; PLAN-003 F | **Out of scope for now.** Ad hoc views, such as the renderer in `reports/2026-10-10--up-to-speed/demo-run-a/`, suffice while developing |

---

## 4 · Decisions

Decisions and their reasons are architecture decision records in [`docs/adr/`](docs/adr/). The
ones in force are those whose status is accepted. What v0 is:
[ADR-030](docs/adr/ADR-030-what-v0-is.md).
