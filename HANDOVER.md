# Handover — session of 2026-08-01/02

Durable state lives in [`STATUS.md`](STATUS.md); design lives in
[ARCH-001](docs/architecture/overview.md). This file is deliberately narrow: what happened in one
session, **what was tried and rejected**, and where to start. It is not a second tracker — an
earlier handover note was retired in favour of `STATUS.md` for exactly that reason (`0e8fa30`), and
nothing here should be duplicated there.

Delete this file once its contents are stale. It is a snapshot, and snapshots rot.

---

## 1 · What changed

The pipeline went from two and a half stages to seven of eight — `enrich` is the deferred one.
`uv run cppsd diff <repo> --base <ref>` runs end to end. 426 tests, ~100 s.

Fourteen commits, `f64e12f` through `9cdd2e3`. Read them in order if you need the reasoning; each
message says why the change mattered, not what it did.

The one-line summary of the result is in `STATUS.md` §2 and it is honest rather than flattering:
compression is 9.5:1 to 53:1, not the 270:1 the design was argued from.

---

## 2 · Things that were tried and did not work

This is the part that is expensive to rediscover.

**Hub suppression and vendored-connection refusal, for decomposition.** The giant component on fmt
`HEAD~100` held 78 % of events, and the obvious causes were high-degree utility nodes and the
`test/` ↔ vendored-gtest bridge. Both fixes were built and measured at every hub cap from 3 to 20.
**Both bought exactly nothing** once edge events were homed on their source entity — they were
fixes for a symptom of the attribution bug. The measurement is recorded in `rank/components.py`'s
docstring so nobody proposes them again.

**Threads for parallel parsing.** ctypes drops the GIL around `index.parse`, so threading looks
plausible. Measured: four fmt TUs go 10.38 s → 8.93 s on four threads, **1.16×**. The extraction
half is Python walking cursors. Processes were the answer.

**`llvm-config --host-target` for the target triple.** Rejected on three grounds: it needs a second
binary version-matched against the parser to learn something the parser already knows;
`CPPSD_LIBCLANG` can point at a library with no relationship to the `llvm-config` on PATH; and
`--host-target` is the host of the LLVM *build*, not the library's default target. The triple is
read from the verified library's own probe TU instead.

**A type-layout proxy for the same problem** (`sizeof` of `void*`, `long`, `long double`).
Real evidence, but it would have needed a hand-maintained list of what to measure — the same
enumeration trap that produced the exception-specification bug in the extractor. Rejected.

**Adding stdlib and resource-dir components to the cache identity.** The doc's §12.1 asked for it.
Measurement showed the include closure already content-hashes 68 libstdc++ headers and 14 clang
builtin headers, so both were already keyed. Adding them would have cost a mass invalidation to
defend against a defended hazard. **Check what the whole key covers before widening an identity.**

---

## 3 · Where to start

**The `acquire` rename API.** It is the only open item that produces *wrong* output rather than
noisy or slow output. `match_models(renames=...)` exists, is tested, and is never fed;
`acquire` exposes only `changed_files` (`git diff --name-only`), which cannot carry renames. ARCH
§5 is explicit that renames must be resolved from git *before* matching — inferring them in the
matcher would derive identity from the content whose identity is in question. Until it is wired,
any review that renames a file over-reports every internal-linkage entity in it, because clang
writes the file name into those USRs.

After that, `STATUS.md` §6 is ordered by how badly each item corrupts the product's claim, which is
the order I would keep.

**One caution on the compression metric.** `summary.compression` is the product's own success
number and the flattering framing is always available — quote top-level-after-collapse (3,178
lines → 56) instead of `events_reported` and it looks close to the original claim. That number is
real and worth reporting, but it must not become the numerator. It is the one figure here nobody
can audit from outside, and quietly redefining it is how a success metric stops measuring
anything.

---

## 4 · How this session was run, and what to watch

Roughly a dozen subagents, mostly two or three concurrent, each scoped to a disjoint file set with
the shared contracts (`model/ir.py`, `model/events.py`) reserved and agents told to stop and report
rather than edit them. That worked: there were no merge conflicts, and the reserved-contract rule
caught four separate requests to change `Completeness` and `EventKind`, which were then done once
as a coherent pass instead of four times.

Three things to watch if you run it the same way:

- **File scoping constrains well; it does not stop an agent that decides something adjacent needs
  doing.** One rewrote 116 lines of `docs/research/cpp-analysis-tool-landscape.md` — accurate-looking
  content, but unasked-for and under the repository author's byline. Reverted; preserved as a patch
  outside the repo. Another edited `tests/test_render.py` outside its boundary, disclosed it, and
  was right to.
- **Agents measure what they are told to measure and report it honestly, including against their
  own work.** The budget verdict came back "missed at 20 TUs, 57.8 s" rather than rounded to a
  pass. Ask for the verdict explicitly and it arrives.
- **Cross-agent dependencies appear mid-flight.** The `diff` stage emitted a new event kind while
  `rank` was concurrently adding its weight; each noticed the other. Both landing was fine, but a
  commit of only one would have `KeyError`d. Commit coupled work together.

---

## 5 · Loose ends that are decisions, not tasks

- `scratchpad/unauthorized-research-doc-edit.patch` — the reverted RESEARCH-001 addition, awaiting a
  read before it goes anywhere near the repo.
- **Exposure asymmetry** (`STATUS.md` §6): blast radius and decomposition now measure an edge event
  at its source; exposure still takes the max over both endpoints. Contained today by the cause
  bound. It wants a deliberate answer, not a default.
- **`EdgeAttrChanged` fires but is rare** — zero occurrences across 300 commits of each fixture.
  Correct, low-yield, and worth knowing before anything depends on it.
- **The suite is ~100 s, and `tests/test_toolchain.py` is 140 s of it** through repeated
  `llvm-config`/`ldconfig` shelling. A `Toolchain.adopt()` fixes that *and* the 0.6 s-per-worker
  startup cost. If the edit loop gets annoying before then, marking the libclang-dependent tests
  gives ~250 pure tests in about a second.
