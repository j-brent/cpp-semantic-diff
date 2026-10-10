<!-- metadata
type: roadmap
kind: living
status: draft
author: Brent Runyan
created: 2026-10-10
-->

# Roadmap

*Living document: where things stand, the one list of open work, and the decisions in force.*

**Draft.** The work in §2 is confirmed. Everything in §3 and §4 was carried over from the v0
tracker and the plans it ran, and is awaiting review: each item will be reaffirmed, changed or
dropped before this draft becomes final.

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
[`reports/2026-10-10--up-to-speed/demo-run/`](reports/2026-10-10--up-to-speed/demo-run/), the
outputs of GitHub Actions run
[38044461310](https://github.com/j-brent/cpp-semantic-diff/actions/runs/38044461310).

**Order.**

1. **Issue 7, third-party code outside the model:**
   [PLAN-004](docs/plans/PLAN-004-third-party-boundary.md). Issue 3 is done in the same schema
   change.
2. **Brent reviews `reports/2026-10-10--up-to-speed/model-analysis.md`**, alongside item 1. It is
   not committed yet. It adds nothing to this roadmap until then. Issue 2 is decided together
   with it.
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
  update also produced 134 change events in leveldb's own test code. The project's code was
  unchanged, but the googletest code it uses had changed underneath it.
- **Recognizing third-party code.** Directory names are a guess. A copied library under an
  unconventional name is missed (the module notes in `rank/exposure.py` say so). An explicit
  list, given by the repository, may be needed.

---

## 3 · Carried over, awaiting review

Open work from the v0 tracker (`T`, the [frozen `STATUS.md`](reports/2026-08-01--v0-build/STATUS.md))
and from PLAN-003, "Shipping v0", which was retired into this list on 2026-10-10 (git keeps it at
`4f083c1`). Each item says where it came from, so its full history can be read there.

Every item has the status **awaiting review**.

### 3.1 · Settled by confirmed work

Kept so that their closing is checked rather than assumed.

| Item | Source | Note |
|---|---|---|
| `internal_linkage_residue` is computed over the whole model, vendored code included | T §6; PLAN-003 C | PLAN-004 takes third-party code out of the model |
| Components are numbered by a top score that includes vendored events, so the terminal can open with an event that is not the highest-ranked | T §6 | PLAN-004 removes the vendored class |
| Whether to build identity tiers 2–4 | T §3, §7 | reopened in part by issue 1 |

### 3.2 · Defects and gaps

| Item | Source |
|---|---|
| No cause rule links `NodeAttrChanged(virtual)` to the `EdgeAttrChanged` on each call into the method, so a method gaining `virtual` with N callers is N+1 top-level events. PLAN-003 recommended skipping it: `EdgeAttrChanged` fired zero times across 300 commits of each fixture. | T §6; PLAN-003 C |
| Each parser worker takes ~0.6 s to start, mostly rediscovering the toolchain the parent already found. Wants `Toolchain.adopt(library, expected_id)`. | T §6; PLAN-003 C |
| `cppsd cache gc` collects by age, not by identity, so a superseded cache generation survives until it is `--older-than` days old. | T §6 |
| `diff.renameLimit` can silently truncate git's rename map on a large diff, and `-l0` is potentially quadratic. | T §7; PLAN-003 C |
| Unnamed (not anonymous) records have a USR that moves on any edit above them, which breaks tier-1 identity. No fixture has one. | T §7 |
| Two `static`s with the same name in same-named files merge into one node. Recorded as a standing decision: no instance in any fixture, and the fix reaches every contract. | T §6; PLAN-003 D |

### 3.3 · Found by the 2026-10-10 documentation pass, not fixed

| Item | Where |
|---|---|
| The terminal's `identity:` line cites the wrong tracker section, and it appears in the demo outputs | `render/terminal.py` |
| JSON field descriptions that are false: `vendored_events` ("never subtracted from the numerator"), `high_risk_count` (it counts first-party impact), and `model/ir.py`'s claim that USRs include the file path (they include the file name) | `render/document.py`, `model/ir.py` |
| The demo summary's "semantic events (numerator)" row prints `events_reported`, which includes vendored events | `.github/workflows/demo.yml` |
| Four cause rules are marked as missing from ARCH-001 §6.3, which now lists them; a test enforces the stale state | `classify/rules.py`, `tests/test_classify.py` |
| ARCH-001 §13 marks `model/` "schema ✓", but no JSON schema is generated | `docs/ARCH-001-architecture.md` |
| A parallel-parsing test failed once under CPU load and passed on rerun: `test_parallel.py::TestNothingVanishes::test_a_worker_that_dies_becomes_a_failure_and_the_other_tus_survive` | `tests/test_parallel.py` |

### 3.4 · Open questions

| Question | Source |
|---|---|
| **Where does the reviewer actually read this?** The CLI is right for v0 and probably wrong as the product. Measured compression is 0.4:1 to 53:1, and the one repository with real implementation files decompresses. | T §7; PLAN-003 F |
| **Generated sources.** ARCH-001 §3.1 states the problem, not the policy. `clang-scan-deps` failed on 9.2 % of opencv's TUs against an unbuilt tree. | T §7 |
| **Should the model fingerprint be visible** in the CLI or JSON, so two runs can be compared without rerunning? | T §7 |
| **Should exposure use `Node.tu`** to sharpen the internal/public boundary where it is set? | T §7 |

### 3.5 · Planned, not started

| Item | Source |
|---|---|
| **Bitcoin Core as a scale-and-shape probe:** [PLAN-002](docs/plans/PLAN-002-bitcoin-probe.md). Needs a `--cmake-arg` passthrough first, because `cppsd diff` cannot pass CMake options. | PLAN-002; PLAN-003 E |
| **`enrich`:** evidence adapters for external analysis tools. Deferred past v0 by design. | T §1; ARCH-001 §8, §13 |

---

## 4 · Decisions in force, awaiting reaffirmation

From the v0 tracker's §8, with its reasons. The one superseded decision, that vendored code is
partitioned out of the ranking, is not repeated here: ARCH-001 §13's prerequisite replaced it on
2026-10-10.

| Decision | Why |
|---|---|
| Python, standalone repo | CodeAtlas C++ is TypeScript only because it began as a VS Code extension. Ideas carry over from it, not modules. |
| Linux only during design and implementation | Windows support is dead weight while the shape is still moving. Portability rules stay ([`CONTRIBUTING.md`](CONTRIBUTING.md)): cross-platform *correctness* is cheap; cross-platform *support* is not. |
| A system libclang is a hard requirement | The wheel's bundled library truncates ASTs silently. Convenience is not worth a failure mode that produces two agreeing wrong models. |
| Base is `merge-base(main, HEAD)` | Against main's tip, every change other people merged while the branch was open gets attributed to this branch. There is a test for this; keep it. |
| Compile database required at **both** revisions | Hard precondition, not a degradation path. Configure is seconds, so it is affordable. |
| Base that does not build ⇒ hard failure | The base is main. A main that does not build is a different problem, not this tool's. |
| JSON is the product, terminal is a consumer | Keeps delivery surfaces out of the analysis core — the mistake that made CodeAtlas TypeScript. `render`'s signature admits no `Model`. |
| v0 ships **tier-1 identity only** | Renames look bad on purpose. Measure the damage before building tiers 2–4; measured in the v0 tracker §3. **Partly reopened 2026-10-10** by issue 1: a function whose parameters changed is to be paired with its old version. |
| Identity tiers are one-to-one | Splits and merges surface as unmatched residue rather than being forced into a match. Enforced in the type, not just the matcher: a double-pairing does not crash, it produces a delta that reads perfectly and describes a change nobody made. |
| Cascade collapse at render, never at diff | JSON always carries the full event set; the view hides consequences under causes. When cause attribution is uncertain, show rather than hide. |
| `EdgeRetargeted` is refused, not deferred | An edge's id *is* `src\|kind\|dst`, so "retargeted from B to C" claims B and C are the same entity — a node-identity judgement the one-to-one matcher already declined. |
| The cache unit is the TU, not the commit | Per-commit caching means the first review against any new base pays full price, and the base is what every review shares. |
| Declining to cache beats a weaker key | A missing or version-mismatched `clang-scan-deps` disables the cache whole. Declining to cache is slow; a stale hit is wrong. |
| The cache key covers the extraction code, not just libclang | `extract.py` decides what becomes a node; change it and the same inputs yield a different partial. Hashed rather than a hand-bumped constant, because forgetting a constant fails toward serving stale entries. |
| Toolchain identity carries the target triple, never a host path | It is the one AST determinant the closure's content hashing cannot see. A path would break ARCH-001 §12.2, which makes the identity a value two hosts *compare* — equivalent machines would hard-fail. |
| Check what the whole key already covers before widening an identity | The stdlib and builtin headers turned out to be content-keyed via the include closure. Every redundant key component costs a mass invalidation to defend against a defended hazard. |
| Workers use `spawn`, not `fork` | A forked child inherits the parent's verified libclang, so the toolchain guarantee would hold *by accident of start method* and stop holding silently if that default changed. |
| Low confidence ranks **up** | Uncertainty is a reason for a human to look, not a reason to hide (ARCH-001 §7). Guarded by mutation-tested cases. Issue 6 questions what uncertainty means for an entity with no partner. |
