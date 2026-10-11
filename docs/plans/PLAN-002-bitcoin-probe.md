<!-- metadata
id: PLAN-002
type: plan
status: not started
author: Brent Runyan
created: 2026-08-04
dependencies: [ARCH-001]
kind: living
-->

# Bitcoin Core as a scale-and-shape probe

*Living document: kept current until it is done, then deleted.*

Deferred. Starts when the issues blocking [#23](https://github.com/j-brent/cpp-semantic-diff/issues/23) are closed.

Adding a fourth verification repository — and the reasons it must not be wired in like the first
three.

The [v0 tracker](../../reports/2026-08-01--v0-build/STATUS.md) §3 records why fmt and spdlog were one and a half samples and what leveldb fixed. This
plan is the next question: those three are all **libraries**, all small, all dependency-free. Every
number the project publishes was taken on that one class of repository.

Delete this file when the measurements are in a frozen report and #23 is closed.

**Since written (2026-10-10).** The VENDORED exposure class is gone: third-party code is
submodules plus paths passed as `--third-party`, is left out of the model, and a comparison where
it changed is refused ([ARCH-001 §4.5](../ARCH-001-architecture.md#45-the-boundary-of-the-model)).
§2's second point and §6.3 measure the class that was removed; reconcile them with §4.5 before
running. This plan was written against `STATUS.md`, which is now the frozen v0 tracker: where it
says to record a result there, record it in a new frozen report instead. `cppsd` still has no option to pass CMake arguments (§5).

---

## 1 · The instruction that comes before all the others

**Bitcoin Core is not a `fixtures`-marked fixture. Do not add it to the pytest suite.**

The `fixtures` marker means "a test that needs a real clone and runs in the normal suite." fmt is
50 TUs, leveldb 93. Bitcoin Core is roughly an order of magnitude larger, and a cold model is two
of those — base and head. Wiring it into `conftest.require_fixture` alongside the others makes
`uv run pytest -q` unusable, which is a worse outcome than not having the fixture.

It is a **probe**: run by hand, measured once per question, recorded in a frozen report. If it later
earns a place in CI it will be as a separate marked-slow job with its own budget, and that is a
decision to make *after* the numbers exist, not before.

---

## 2 · What it adds that the other three cannot

Four shapes, in descending order of how much they matter. The first is the reason to do this at
all; the last is a bonus.

**It is an application, not a library — so `rank/exposure.py`'s model has nothing to grip.**
Exposure classifies by path convention: `include/` is PUBLIC, everything else first-party is
INTERNAL. Bitcoin Core has no `include/`. Everything is under `src/`. So the prediction is that
**every first-party node classifies INTERNAL** and the four-class scale collapses to two. This is
the failure mode `exposure.py`'s own docstring names — *"A library that publishes from `src/` and
keeps private headers under `include/internal` is read backwards"* — in its extreme form: nothing
is public at all. leveldb *validates* the convention (it has `include/leveldb`); this is the first
fixture that can break it. v0 tracker §3's exposure-composition table is the artifact to extend.

**Five vendored subtrees with no conventional directory name.** `src/secp256k1`, `src/crc32c`,
`src/leveldb`, `src/minisketch`, `src/univalue` are third-party code sitting in first-party paths.
`exposure.py` names this too — *"A dependency vendored without a conventional directory name is not
detected at all and is scored as first-party."* Here there are five instances at once, and one of
them is a fixture this project already clones separately. Expect the VENDORED share to be
under-counted; the number to record is by how much.

**A real third-party template dependency, which `Completeness` has never met.** fmt, spdlog and
leveldb are dependency-free. Bitcoin Core uses Boost — headers only, see §3 — and `extract.py`
prunes every cursor outside the repository root, so Boost contributes **zero nodes** while calls
into it resolve to nothing and land in `Completeness.edges_dropped`. That counter has never been
exercised against a genuine external library. Its value here is the first real reading.

**Genuine architectural layering.** `consensus/`, `policy/`, `validation`, `net_processing`,
`node/`, `wallet/`, `kernel/` are a layered system with rules people argue about in review.
`BECAME_UNREACHABLE`, `LAYER_VIOLATION_INTRODUCED`, component decomposition and blast radius were
all designed against this shape and have only ever run against libraries.

---

## 3 · What is settled — verified, not assumed

Checked against master on 2026-08-04. Re-verify before acting; this repository moves.

1. **Boost is headers-only, and it is two libraries.** `depends/packages/boost.mk` sets
   `-DBOOST_INCLUDE_LIBRARIES="multi_index;test"` and `-DBOOST_TEST_HEADERS_ONLY=ON`. `multi_index`
   is the mempool (`txmempool.h`), `test` is the unit suite. `boost::signals2` is gone. On Debian,
   `libboost-dev` — the headers metapackage — is sufficient; the `libboost-system-dev` /
   `-filesystem-dev` / `-thread-dev` lists in older build guides are pre-v22 and wrong.
2. **libevent is no longer a dependency**, required or optional. It is absent from
   `doc/dependencies.md` entirely.
3. **The minimal configure needs four packages.** `build-essential cmake python3 libboost-dev`.
   GUI, ZMQ, Cap'n Proto/IPC and USDT are all off by default; `-DENABLE_WALLET=OFF` drops SQLite.
4. **Nothing needs a build step to parse.** `acquire.configure` runs CMake and never builds, so any
   repository whose headers are generated at *build* time parses truncated. Bitcoin Core's
   generated headers come from `configure_file` at *configure* time, and the two codegen paths that
   would break this — Cap'n Proto for IPC and Qt's moc for the GUI — are both off by default. Do
   not turn them on.
5. **`src/secp256k1` is a CMake subproject** and configures as part of the parent. It needs no
   separate handling.

Sources: [`doc/build-unix.md`](https://github.com/bitcoin/bitcoin/blob/master/doc/build-unix.md),
[`doc/dependencies.md`](https://github.com/bitcoin/bitcoin/blob/master/doc/dependencies.md),
[`depends/packages/boost.mk`](https://github.com/bitcoin/bitcoin/blob/master/depends/packages/boost.mk).

---

## 4 · The constraint that will otherwise waste a session

**Bitcoin Core migrated from autotools to CMake in the v29 cycle.** `acquire.configure` runs CMake
on *both* worktrees, so a base ref below that boundary fails on the base side while head succeeds —
and the failure is in the fixture, not in this tool. An agent that picks `--base HEAD~2000` without
checking will spend a long time diagnosing its own code for a problem it does not have.

Find the boundary once, before choosing any revision pair:

```sh
git -C ~/fixtures/bitcoin log --oneline --diff-filter=A -- CMakeLists.txt
```

Both `--base` and `--head` must be above it. Record the boundary commit in v0 tracker §3 so the
next agent does not rediscover it.

---

## 5 · Setup

```sh
sudo apt install build-essential cmake python3 libboost-dev
cd ~/fixtures && git clone https://github.com/bitcoin/bitcoin
```

Full history, no `--depth` — a shallow clone has no second point and the tool rejects it by design.
No submodules: the third-party code is subtrees, already in the tree.

`acquire` passes extra CMake arguments through, and the configure this fixture needs is
`-DENABLE_WALLET=OFF`. Confirm how to supply it from the CLI before assuming it can be; if it
cannot, that is a finding about `acquire`'s interface and belongs in v0 tracker §6, not a reason
to hand-configure a worktree and work around it.

---

## 6 · Measurement protocol

Do these in order. **Stop and record after each.** The point is not to reach the end; it is that
each step produces a number nobody has, and any one of them can be the finding that ends the plan.

**6.1 · Size it before building anything.** `cppsd plan` exists to answer "what would be analyzed"
without analyzing it. Run it first and record TU count and changed-file count. If the TU count is
far above the order of magnitude assumed here, revise the rest of this plan before spending a cold
build on it.

**6.2 · One cold model, one side, timed.** Not a diff — a single `acquire` + model build, wall
clock and peak RSS. `produce/build.py:assemble` consumes partials as a stream, but the merged node
set is held whole, and fmt reaches 10,574 entities from 134,366 raw nodes. The open question is
whether the merge survives a repository twenty times larger, and the honest possible answer is that
it does not. **An OOM or a wildly-blown budget here is a successful outcome of this plan** — it is
the §9 budget meeting a real repository, which is exactly what v0 tracker §2 says this project
must surface early. Record it in §4's performance table and stop.

**6.3 · Exposure composition.** Extend v0 tracker §3's four-class table with a bitcoin row. The
prediction in §2 is that INTERNAL is near 100 % of first-party and the five subtrees are
misclassified as first-party rather than VENDORED. Record what actually happens. If the prediction
holds, `exposure.py`'s vocabulary list is the calibration surface — but **do not edit it in this
plan.** Changing the heuristic and measuring it are two pieces of work, and doing both at once
means the second cannot judge the first.

**6.4 · `Completeness.edges_dropped`, with and without Boost in the picture.** Compare a
mempool-touching revision pair against one that touches none. The difference is the first real
measurement of what an external template dependency costs the model's completeness.

**6.5 · One real review.** A revision pair from actual project history where the change is
semantic and the line diff is large. This is the only step that tests the product claim rather than
the machinery. Report events, top-level-after-collapse, and the line-diff denominator, in
v0 tracker §2's format.

---

## 7 · Commit split

| | Commit | Note |
|---|---|---|
| 1 | docs: README clone block and the §2 rationale | the durable part; mirrors how leveldb landed |
| 2 | docs: v0 tracker §3 and §4 rows from §6's measurements | one commit per question answered is fine |
| 3 | docs: fold in and delete this file | |

No source changes are planned. **If this plan produces a code commit, something has gone wrong with
its scope** — findings go to v0 tracker §6 as defects, and fixing them is separate work with its
own plan. The exception is `acquire`'s CMake-argument interface (§5), if it turns out not to have
one.

---

## 8 · What this does not close

- **CI.** The probe is manual by design. Whether Bitcoin Core ever belongs in an automated job is a
  question the §6 numbers answer, not this plan.
- **The exposure heuristic.** §6.3 measures it and refuses to fix it. A repository-level
  configuration for public-path conventions is the obvious shape of the fix and it is a design
  question for ARCH, not a fixture question.
- **Whether the `V0_KINDS` layering events actually fire.** §2 argues Bitcoin Core is the first
  fixture that *could* produce them. Whether it *does* depends on the revision pair chosen, and one
  pair proving nothing is the likeliest outcome of §6.5.
