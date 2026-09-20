# Status

Living tracker: what is built, what is next, and what was already decided. Updated at the end of
each working session. Started 2026-08-01 as a handover note; kept as a tracker because the
*reasons* behind the settled decisions are the part worth carrying forward.

Setup, requirements and the commands that work today are in [`README.md`](README.md). Design is
[ARCH-001](docs/architecture/overview.md); this file is state, not design.

Active plans: [PLAN-003](docs/plans/ship-v0.md) — what remains before v0 ships, measured rather
than estimated from this file; [PLAN-002](docs/plans/bitcoin-fixture.md) — Bitcoin Core as a
scale-and-shape probe. Each says in its own header what it is deleted into here, the way PLAN-001
was (§6). A plan is a plan; this file is the state it leaves behind.

---

## 1 · Pipeline

Eight stages (ARCH §3). **Seven exist; `enrich` is deferred post-v0 by design.**

- [x] **acquire** — two revs → two worktrees + compile databases
- [x] **produce** — worktree + compile db → `Model`
  - [x] toolchain: discover a system libclang and prove it before use
  - [x] cursor traversal and node extraction
  - [x] USR dedup, within and across translation units
  - [x] edges: `contains`, `calls`, `inherits`, `overrides`
  - [x] per-TU cache keyed on content + headers + flags + producer version
  - [x] parallel parsing — what makes the §9 budget reachable
  - [x] `Completeness` filled in from real parse failures
- [x] **match** — two `Model`s → `Correspondence` (tier 1 only, deliberately)
- [x] **diff** — → `Event` list
- [x] **classify** — cause and cascade links
- [ ] **enrich** — evidence adapters *(post-v0; blast radius shipped inside `rank`)*
- [x] **rank** — risk scores and decomposition
- [x] **render** — JSON document, terminal as a consumer

`uv run cppsd diff <repo> --base <ref>` runs the whole thing.

---

## 2 · The measurement this project exists to take

```
fmt    HEAD~12    179 lines changed  →  16 semantic events, 2 high-risk
                  7 top-level, 9 nested under a cause · 5 components
fmt    HEAD~100   3,178 lines changed → 335 semantic events, 17 high-risk
                  56 top-level, 279 nested · 30 components
spdlog HEAD~30    1,708 lines changed → 32 semantic events, 5 high-risk
                  24 top-level, 8 nested · 18 components
spdlog HEAD~5     35 lines changed   →  0 semantic events

leveldb HEAD~9..~1 45 lines changed  →   6 semantic events, 3 high-risk   (7.5 lines/event)
                  + 117 in bundled dependencies from a CMAKE_CXX_STANDARD bump

leveldb HEAD~10   52 lines changed   → 134 semantic events, 5 high-risk
                  3,660 more in bundled dependencies (96 %), ranked last
                  11 top-level · 142 components · first vendored event at rank 112
```

**The leveldb row is what a third fixture was for.** The commit bumps `third_party/googletest` and
`third_party/benchmark`, and on the first run the top 12 events and 44 of the top 50 were bundled
googletest — the exact failure `rank/exposure.py` was written to prevent. Vendored code now sorts
after everything first-party as a block (§6, and `ranker.py` carries the arithmetic), so the top
event is `DBImpl::TEST_CompactMemTable` — which is what the commit says it does.

| | before | after |
|---|---|---|
| vendored in top 12 | 12 | **0** |
| vendored in top 50 | 44 | **0** |
| rank of first vendored event | 1 | **112** |

Neither original fixture could show it: they vendor googletest as a static copy that never differs
between two revisions of the host, so vendored event counts were always near zero. It takes a
dependency that moves as a unit.

**The ratio no longer straddles.** It was 0.0 lines per event, dividing a first-party denominator by
a whole-world numerator. The denominator was never wrong — git renders a submodule bump as the two
lines a reviewer actually reads, and §10 defines it as "the number a reviewer would otherwise have
read", so inflating it to the hundred thousand lines inside googletest would have been fiction that
flattered the ratio. The numerator came down instead: `compression_ratio` and `high_risk_count` are
over first-party events, `vendored_events` names what was set aside, and `events_reported` stays the
complete set. Schema 1.1 → 1.2, because two fields kept their names and changed their meaning.

**leveldb still reads 0.4 lines per event, and that is the honest answer.** 52 lines of text diff
produced 134 first-party semantic events — mostly test-code entities that moved because googletest
moved under them. The tool is not compressing here; it is reporting that a two-line dependency bump
had 134 consequences in this repository's own code, which is a real finding and the opposite of the
thesis on this commit. Worth keeping visible rather than averaging away.

Nothing else moved: fmt `HEAD~12` (11.2 lines/event) and spdlog `HEAD~30` (53.4) are unchanged to
the digit, which is the check that the numerator was aligned rather than shrunk.

**The verdict, without spin.** Compression is real but roughly an order of magnitude weaker than
ARCH §10's illustrative *2,431 → 9* (270:1). Measured: **0.4:1 to 53:1** on first-party events.

The bottom of that range is new and it is not a rounding error — leveldb `HEAD~10` *decompresses*,
turning 52 lines into 134 things to read. Both fixtures that were available when the "9.5:1 to 53:1"
claim was written are header-only libraries that vendor by copying; the first repository with real
implementation files and a moving dependency produced a ratio below 1. One commit is not a verdict
on the thesis, but the range no longer supports a claim of reliable compression, and quoting the old
one now would mean quoting the two repositories that agreed.

The number that actually reads as dramatic is **top-level events after cascade collapse** — 3,178
lines down to 56 things to read. If the compression line is to be the headline, its honest form is
two numbers. It currently prints the weaker one first on purpose: the numerator must never quietly
become the collapsed count, because that is the one number in this product nobody can audit from
the outside.

The spdlog `HEAD~5` row is the one that validates the rest. Thirty-five lines of CMake, comments and
a Windows-only header produce zero events, *correctly* — and the run prints the §11 limit that says
why, rather than implying the change was empty.

---

## 3 · The v0 identity measurement — a negative result

v0 ships tier-1 identity only so the damage could be measured before tiers 2–4 were built
(§8, ARCH §5). That measurement is a v0 deliverable and it is now in:

| Comparison | unmatched base | unmatched head | matched | pairable bound |
|---|---|---|---|---|
| fmt `HEAD~12` | 3 | 3 | 10,571 | 3 |
| fmt `HEAD~100` | 5 | 97 | — | 5 |
| spdlog `HEAD~30` | 2 | 1 | — | 1 |
| leveldb `HEAD~9..~1` | **0** | 49 | 7,695 | **0** |

Head-side residue is not pairable where it is genuinely new: fmt `HEAD~100`'s 97 are real additions,
and leveldb's 49 are an artifact of the `CMAKE_CXX_STANDARD 11 → 17` bump in that range exposing
declarations C++11 did not (all 49 in vendored googletest — see §5).

**The upper bound on events that tiers 2–4 could remove is 3, 5, 1 and 0** — and the zero is on the
only repository with real implementation files rather than a header-only library. Tier-1-only costs
almost nothing anywhere it has been measured. This argues against the next planned investment, which
is exactly what ARCH §10 asks the design to surface in week one rather than month six.

**Verdict: do not build tiers 2–4.** §7 held this open pending a third repository; it now has one,
of a deliberately different shape, and the answer got *stronger* rather than weaker. What would
reopen it is a repository mid-refactor — none of these four comparisons renames anything
substantial, and `internal_linkage_residue` remains the number to watch.

Caveat worth keeping: fmt and spdlog are stable libraries. A repository mid-refactor would look
different, and the number to watch is `internal_linkage_residue` — statics and anonymous-namespace
members whose USR embeds the file name, where a file rename breaks tier 1 wholesale.

### The rename hazard, measured

fmt `da256982` (`src/posix.cc` → `src/os.cc`), before and after `acquire.file_changes` was wired
into `match_models`:

| | before | after |
|---|---|---|
| `internal_linkage_residue` | 2 | **0** |
| unmatched base / head | 1 / 1 | 0 / 0 |
| events reported | 8 | **0** |
| high-risk | 6 | **0** |

All eight were **one unchanged function** — `convert_rwcount` in the renamed file's anonymous
namespace — reported as removed and added, with its two call edges and its `contains` edge each
counted twice. Zero is the correct answer: the same commit moved declarations from `posix.h` to
`os.h`, and external-linkage USRs do not encode the file, so that part is correctly no event.

**How rare, and how bad.** fmt has **154 file-scoped USRs out of 9,752 nodes** (1.6 %), 144 of them
in vendored `gmock-gtest-all.cc`. Most renames touch none and cost nothing — `include/fmt/core.h` →
`base.h` measured 0 before *and* after. So the defect is rare but severe where it lands, which is
the argument for fixing it rather than counting how often it fires.

### Both original fixtures were the wrong shape — leveldb added

fmt and spdlog are both effectively header-only, and **spdlog vendors fmt**
(`include/spdlog/fmt/bundled/`, `src/bundled_fmtlib_format.cpp`), so they were closer to one and a
half samples than two. That matters specifically here: a header-only library cannot use
namespace-scope statics, because that would mean one copy per translation unit. The fixtures
structurally excluded the phenomenon `Node.tu` exists for.

| | nodes | TUs | file-scoped USRs | of those, **library code** |
|---|---|---|---|---|
| fmt `HEAD~12` | 10,574 | 50 | 153 (1.5 %) | **2** |
| leveldb `HEAD~10` | 7,977 | 93 | 825 (10.3 %) | **428** |

Model composition by exposure class, which is the number that explains it:

| | vendored | test | internal | public |
|---|---|---|---|---|
| fmt | 31.7 % | 49.0 % | **0.0 %** (3 nodes) | 19.4 % |
| leveldb | 52.8 % | 28.5 % | **6.2 %** (491) | 12.5 % |

fmt has *three* internal nodes in the whole model — everything lives in `include/`, so everything
is public. Every `Node.tu` conclusion drawn from fmt alone rested on a sample of two.

---

## 4 · Measured performance

### The model fingerprint — and a correction to what this section claimed

fmt `HEAD~12` — base rev `bcaa44d05579`, 10,574 nodes, 20,958 edges — fingerprints
**`1a6339b03c13b1d0f513220b9ad37938`** at `HEAD`. Written out in full, with the fixture, the range
and the counts beside it, so that the next person can *check* it instead of trusting it.

**Both values this section carried before were never reproducible.** It recorded `5ba8fd29041c`,
then `5ca3843b6c1a` superseding it when `Model.fingerprint()` began hashing `Node.tu`. Measured in
a worktree at `07ed88b` — the exact commit whose STATUS diff introduced `5ca3843b6c1a` — the same
comparison against the same clone, same `base_sha bcaa44d05579`, same 10,574 nodes and 20,958
edges, reads `9ab7fd9a298171b3d5c4a5690d0958ba`. The code at the moment the number was written
produced a different number, so the error is in the writing and not in the code. Three things
confirm that reading. `fingerprint()` has been `blake2b(digest_size=16)` since the scaffolding
commit `b7394b9`, so it returns **32** hex characters and both recorded values are 12 — and every
`[:12]` in this codebase truncates a *git revision* (`cli.py`, `render/terminal.py`,
`acquire/git.py`), never a fingerprint. The fmt clone has no `FETCH_HEAD` at all — never fetched
since it was cloned on 2026-08-01 — so the commit range did not move under the measurement. And
the two measured values agree with each other: `826209c` is the only commit between `07ed88b` and
`HEAD` that changes what the producer puts in a node, and a member function template becoming a
`method` is exactly the kind of change that must move a fingerprint, since kind and attrs are
hashed.

Marked wrong rather than quietly overwritten, because a superseded number's *story* is what this
file carries forward — and the story is the reusable part. A truncated hash with no fixture, no
range and no counts beside it is unfalsifiable; there was nothing to check it against, so nobody
checked it, through two sessions and one supersession that copied the shape of the mistake forward.

**What this section asserts is a property of the check, not of a literal.** Two runs of one build
must agree — 1 worker against 8, a cold cache against a warm one, in either order. A value that
moves when the model gains content is expected and correct: `Node.tu` moved it, and so did the
member function template fix (§6). A value that moves between two runs of the same build is the
defect, and it is the only thing this number is for. Phrasing it as "fmt reads X" inverted that —
it made the literal the claim, which is how a wrong literal survived unnoticed while the property
it stood for was holding the whole time. The current value was verified identical across cold/warm
and 1/8 workers by the commit that produced it, and reproduced exactly in a separate run.

The fingerprint reaches nobody. It appears in no CLI output and in no JSON field; outside
`differ.py`'s docstring its only callers are tests and ad-hoc scripts. That is a large part of why
a wrong value went unchallenged — contradicting it required someone to decide to compute it by
hand. Whether it *should* surface is §7's question, not answered here.

### Timing — stale, and named rather than deleted or guessed

fmt, 50 TUs, WSL, 8 cores. All 22 measured builds — 1 vs 4 vs 8 workers, cold/warm, cache on/off —
agreed with one another, which is the check above passing twenty-two times; what they were recorded
as agreeing *on* is the part that did not survive re-measurement.

**The numbers below were taken on 2026-08-02 (`7dad880`) and have not been re-taken.** Six commits
have touched `extract.py` or `ir.py` since — `93b0d04`, `3c71f8a`, `4cb9290`, `9118830`, `07ed88b`,
`826209c` — and every one of them gives every TU a fresh `Cache.identity`, including `3c71f8a`,
which changed two comments. That is §8's hash-the-source decision working as designed, and it means
the store these warm rows were served from is superseded whole: "fully warm" is a state this
machine would have to re-earn before the row could be reproduced. The extraction half has also
gained per-cursor work in that range that nothing has timed. The *shape* is structural and is not
in doubt — parallelism pays on invalidated TUs and not at all on the warm floor — but the absolute
seconds describe a producer that no longer exists.

| Scenario | invalidated TUs | 1 worker | 8 workers |
|---|---|---|---|
| cold, cache off | 50 | 105.7 s | **31.0 s** |
| cold, cache on | 35 + 15 in-run hits | 86.1 s | **25.6 s** |
| fully warm | 0 | 6.3 s | 6.4 s *(no pool started)* |
| warm, 10 sources touched | 13 | 30.7 s | **14.2 s** |
| warm, 20 sources touched | 24 | 55.0 s | **19.4 s** |

**ARCH §9's budget — ≤ 20 TUs under 30 s warm — was met when this was taken**, and at 24
invalidated TUs rather than 20, so the stated case had more headroom than the number shows. At one
worker the same review took 55 s. Re-taking the 24-TU row is what would turn this back from a
past-tense measurement into a current claim; inventing a correction for it would be the mistake the
first half of this section is about.

The residual ~6.5 s warm floor was not parallelized: ~1.3 s `clang-scan-deps` + toolchain discovery,
~4.2 s key hashing and cache deserialization, ~0.6 s assembly.

---

## 5 · Findings worth keeping

Every one of these came from running the code against real repositories. None was visible in
synthetic fixtures.

- **Silent AST truncation.** The `libclang` PyPI wheel ships the native library without builtin
  headers. Every TU came back with exactly one error — `'stddef.h' file not found` — and no
  exception. The first 50-TU measurement was wrong by 21 s. This is the worst possible failure for
  a two-point tool: both models omit the same declarations and agree.
- **The extractor leaked the host path and commit SHA into identity.** clang spells an anonymous
  record as `(anonymous union at <abs path>:<line>:<col>)`, and that reached `name`,
  `qualified_name`, and — less obviously — canonical *type* spellings, so it also reached
  `attrs["type"]`, `attrs["return_type"]` and `signature`. Worktrees are named by SHA, so 32 fmt
  nodes and 22 spdlog nodes differed on **every comparison of any two revisions**. They were the
  only spurious differences in an otherwise clean two-point run.
- **Lazily-evaluated exception specifications are a tier-1 hazard.** clang leaves an implicit
  special member's spec `unevaluated` in a TU that never odr-uses it, so `~ostream` is
  `void () noexcept` from one TU and `void ()` from another. `signature` is tier-1 identity input:
  add one test file and an unchanged destructor reports delete + add.
- **Never parse a version out of a whole path.** `x86_64-linux-gnu` contains `86`, which outranked
  every real LLVM version and ranked LLVM 16 above LLVM 20.
- **Ask the system before guessing.** Ubuntu ships `llvm-config-18` and no bare `llvm-config`, so
  the good discovery path silently never fired. `ldconfig -p` reports a directory and suffix no glob
  had. The same trap recurred with `clang-scan-deps`, which is also not on PATH.
- **The producer is the parser *and* the extraction code.** `Toolchain.id` alone leaves a cache-key
  hole exactly the size of this project's own source: the extractor fix above changes every partial
  while leaving all cache inputs byte-identical. `ir.py` and `extract.py` are hashed into the key.
- **fmt's compile database has 50 entries but only 35 distinct compilations** — three test sources
  compiled once per target, differing only in `-o`. Because the cache keys on flags as
  `parse_arguments` produces them, 15 of 50 parses in a *cold* run are served from entries written
  earlier in the same run.
- **fmt's model is 80.6 % test code**, 31.7 % of it vendored gtest. Extraction is right to include
  it; ranking had to be taught it exists, or every review opens with vendored churn.
- **Threads do not help.** ctypes drops the GIL around `index.parse`, but the extraction half is
  Python walking cursors: four TUs go 10.38 s → 8.93 s on four threads. 1.16×.
- **Aliasing merges as well as resolves.** fmt's 26,071 distinct edges drop 5,113 unresolvable
  endpoints but yield 20,958 — not what subtraction gives. 2,399 are two calls to two instantiations
  of one template becoming one call to the template, which is what ARCH §5 asks for.
- **`EdgeAttrChanged` is correct and almost never fires** — zero occurrences across 300 commits of
  each fixture. Worth knowing before anything is built on top of it: a rule that depends on it will
  look correct in review and do nothing in practice.
- **The `-std` hazard is real, and it was found by a one-line CMake diff.** §6 predicted that a
  `CMAKE_CXX_STANDARD` bump "rewrites 6 % of the model"; leveldb `HEAD~9..~1` does exactly that —
  `11 → 17`, 45 lines of diff, and **117 events in vendored googletest** (49 `NodeAdded`, 67
  `EdgeAdded`) from declarations C++17 exposes and C++11 did not. Two things worth keeping. The
  first-party events on that range are **6, all real** and all traceable to the commits in it, so
  the artifact did not contaminate what the reviewer reads — the vendored partition contained it,
  which is that decision earning its place a second time. And the artifact is invisible in the
  residue as anything but "additions": a language-mode change looks exactly like new code, which is
  why `Completeness` should carry the language mode (§6) rather than leaving it to be inferred.
- **`git worktree add` does not populate submodules**, and nothing noticed until the fixture set
  widened past two repositories that vendor by copying. A submodule is a gitlink — mode 160000, a
  commit id — and the worktree gets an empty directory. leveldb failed CMake configure, which
  surfaced as ARCH §3.1's "the base is expected to build": alarming and wrong, since the base built
  and the worktree was incomplete. The quieter case is worse — where a missing submodule does *not*
  break configure, both models lose the same entities and agree with each other, which is the §5
  silent-truncation failure by another road. Fixed in `acquire`, and it **requires network**: a
  worktree clones submodules into its own module store (`.git/worktrees/<sha>/modules/…`), so the
  parent having them checked out does not help and `submodule.alternateLocation=superproject` does
  not rescue it. Both measured with the network blocked.
- **The exposure heuristic generalized to a layout it was not fitted to.** leveldb puts
  `db/db_test.cc` beside `db/db_impl.cc` with no `test/` directory anywhere, so only the
  filename-stem half of the rule applies — and it classifies correctly. One over-promotion worth
  calibrating: `port/port_stdcxx.h` reads PUBLIC because it is a header outside a private segment,
  though leveldb treats `port/` as internal.
- **`git diff --name-only` was discarding an answer git had already computed.** Rename detection
  defaults on since git 2.9, so the base-side path of every rename was available all along and the
  flag choice threw it away. Before building machinery to recover something, check whether the tool
  already knows it and is being asked the wrong question.
- **A defect report written from rendered output can name the wrong stage.** "Self-edges appear
  in real models — producer-side" sat in §6 for weeks. The producer has refused `src == dst` since
  the day it was written; the JSON for the event behind that report carries two *different* USRs,
  a partial specialization and its primary template, and the `inherits` edge between them is
  exactly right. What was broken was one stage downstream and an order of magnitude smaller: the
  display name collapsed two identities into one string. The rendering is the surface almost every
  observation is made through, and it is lossy on purpose — it trades identities for names a human
  can read. So the first move on anything seen in the terminal is to read the same event in the
  JSON, which is what §8 means by the JSON being the product rather than a debug convenience.
- **The same surface fails in the other direction too: it deletes.** The finding above is the view
  naming the wrong stage; this is the view removing the evidence. `_subject` interpolated entity
  labels into rich markup unescaped and `[` opens a markup tag, so an overload disambiguation that
  was correct in the document never once reached a screen, and the identical hole in `_completeness`
  was shortening the parse-failure detail ARCH §11.2 requires to be stated. **Absence in the
  terminal is not evidence of absence in the product** — which is the failure ARCH §11 opens by
  naming, arriving one stage past where §11 looks for it, in the view rather than in the analysis.
  It is also the harder half of the pair: a wrong name provokes a defect report, a silent deletion
  provokes nothing, and both defects sat unreported from `f6cf2a6` through every fixture run
  since. Hence the rule the fix took: escape positionally, at every point a document string
  enters the markup, never at the sites judged to need it — you cannot tell by looking at a
  string whether it will lose something.
- **`errors="replace"` is not a safe default when the string is an identity.** Two different
  undecodable paths decode to the same string, so a rename map built through `run_git` would collapse
  two renames into one entry and assert that two different files are the same file. Paths that are
  identity are decoded with `surrogateescape`, the way `os` decodes filenames.

---

## 6 · Known defects and gaps

Found by running the pipeline on real code. Ranked by how badly each corrupts the product's claim.

Closed this session, kept because the *reason* each was invisible is the reusable part: three
ranking defects that were one attribution bug (an edge event was treated as belonging to both its
endpoints, so a new call inherited its callee's fan-in — inversions 174→0, high-risk 40 %→5 %,
largest component 78 %→18 %); `Completeness` unable to state incompleteness numerically; the
missing `EdgeAttrChanged`; and a toolchain identity that named the parser but not the parse. Every
one was invisible on synthetic fixtures and obvious on the first real run.

Also closed: **`match_models(renames=...)` was never fed** — `acquire.file_changes` now resolves
renames from git and `cli.py` passes them (§3 has the before/after). The lesson worth keeping is
about the *measurement*, not the fix: the commit chosen to demonstrate the defect showed 0 before
and 0 after, because the prediction that fmt's anonymous records carry file-scoped USRs was wrong —
clang spells them `…@Ua`. `internal_linkage_residue` is 0 on most real renames, so it cannot serve
as a smoke test; a fixture commit has to be chosen by first checking the renamed file for a
namespace-scope `static` or an anonymous namespace.

The first two are what the leveldb row in §2 is made of, and they are the most serious open items.

Three more are closed below that were listed open until now: the `merge_edge` attribute merge and
the cache sweep were fixed by commits that did not touch this file, and the self-edge item was never
a defect in the stage it named. A tracker drifts by omission, not by error — the fix landed, the
line stayed — so an item here is only as current as the last commit that thought to edit it.

Five more are struck this pass — the render label, member function templates, `--workers`,
`cache.notes()` and `CPPSD_RESOURCE_DIR` — which takes PLAN-003's §B tail down to its last row, the
exposure-endpoint decision below.

- [ ] **`internal_linkage_residue` is still computed over the whole model**, vendored included —
      the last metric that does not distinguish what `rank/exposure.py` classifies. leveldb is
      52.8 % vendored by node count. `compression_ratio` and `high_risk_count` were fixed; this one
      lives on `Correspondence`, which is upstream of `rank` and has no exposure map, so it needs a
      decision about where the classification belongs rather than a one-line change.

The renames-and-defining-TU work is complete; PLAN-001 was folded into this file and deleted, as
its own header instructed. Its measurements are in §2 and §3, and the one claim it got wrong is
below.

- [x] ~~**`Node.tu` is declared and never populated.**~~ Set where the USR is file-scoped **and**
      the declaration is in the TU's own primary source. The second condition is what makes it
      safe: a `static` in a *header* is one entity per includer collapsed to one node, so its TU
      would be whichever observation ranked highest and would flip when an unrelated source joined
      the build. Under the narrow rule exactly one TU can see each entity that gets a TU, so there
      is nothing to flip. `Model.fingerprint()` hashes it under the same condition
      `DeclaredTier.key` uses, so fmt `HEAD~12`'s fingerprint moved, as it had to — though the
      literal written here for it was never reproducible, and §4 now carries the measured value in
      place of it. Re-measured on all three fixtures: **every event count, high-risk count and
      residue identical to before**, which is what "near-zero risk by construction" predicted.
- [ ] **Two same-basename statics still merge into one node — a standing decision, not a backlog
      item.** `Node.tu` does *not* fix this, which the plan for that work claimed it would.
      `merge_node` keys the model on `Node.id`, which **is** the USR, so the collision happens
      before any TU is recorded; half of them stay invisible and a body change in either produces
      no event. What `tu` fixed is the *matching* half: a base node from `a/` no longer pairs with
      a head node from `b/`. Separating them in the model needs the node dictionary keyed on
      `(usr, tu)`, which reaches every edge endpoint, every event, every cache entry and the JSON
      contract — days of work through the contracts §9 holds read-only for exactly that reason.
      **Deliberately left named rather than half-done**, because there is no instance in any
      fixture and `tests/test_tu_identity.py` is the only place it exists: the cost is certain and
      the occurrence rate is zero on everything measured. The trigger to revisit is an observed
      instance, not a spare afternoon — and it is cheap to watch for, since `merge_node` already
      sees both observations and two partials offering different non-`None` `tu` for one USR *is*
      the collision. Counting that costs nothing and would turn this from a prediction into a
      number.
- [x] ~~**`EdgeAdded`/`EdgeRemoved` between two surviving nodes is not collapsible**~~ — the shape
      that actually caused it is: a dependency adds an overload and every call site moves to it,
      leaving the old overload declared but uncalled. `call-moved-to-another-overload` links those
      to the `EdgeAdded`, which chains to the new overload's `NodeAdded`. leveldb `HEAD~10`:
      **39 top-level → 11**, and fmt and spdlog unchanged to the digit, which is the check that it
      did not over-link. A call genuinely dropped between two survivors still has no cause and is
      still shown — that is the design (`test_classify.py`), not a gap.
- [ ] **Exposure takes the max over both edge endpoints** while blast radius and decomposition now
      take the source. Contained by the cause bound today, but an `EdgeAdded` from test code into
      public API with *no* cause link is still scored as public. Decide it deliberately.
- [ ] **No cause rule links `NodeAttrChanged(virtual)` to the `EdgeAttrChanged` on every call edge
      into it.** A method gaining `virtual` with N callers is N+1 top-level events.
- [x] ~~**`merge_edge` merges edge attrs with `setdefault`**~~ — the merge now takes the
      *best-informed* observation under a total order (`_edge_attr_rank`) rather than the first to
      arrive. Truthy outranks falsy because every falsy value this producer emits is also what it
      emits when it does not know — `virtual` is `False` both for "not virtual" and for a callee
      this TU could not resolve as a method, `access` falls back to `"private"` — so the rule never
      discards a fact for a default; `repr` beyond that is a stable tie-break and says so. The
      reason worth carrying: first-to-arrive made the value a function of compile-database order
      and pool-completion order, so the same two revisions could diff clean or dirty depending on
      which TU finished first, and worse under the parallel producer, where that is not stable
      between two runs of the *same* build. `merge_node` has been ordered against exactly this
      since it was written and edges simply never were. `max` over a total order is commutative,
      which is the property the tests assert rather than the values.
- [x] ~~Self-edges appear in real models (`is_named_arg -> is_named_arg`). Producer-side.~~
      **Misdiagnosed, and the misdiagnosis is the part worth keeping (§5).** `merge_edge` has
      refused `src == dst` since the first extraction commit, so no self-edge can reach an
      assembled model. What was observed is the *terminal* printing
      `fmt::v12::detail::is_named_arg -> fmt::v12::detail::is_named_arg`; the JSON for that same
      event carries two different USRs — `…@SP>1#T@is_named_arg>#1t0.0` and
      `…@ST>1#T@is_named_arg` — a partial specialization inheriting from its primary template.
      The edge is real, correct, and `inherits`. The defect is the display name and it moves to
      render, below, where it gets much smaller.
- [x] ~~**A rendered name that is not an identity.**~~ `entity_names` now disambiguates with a
      ladder — signature, template role, location, USR — taking the first discriminator that
      separates *every* member of an ambiguous group, so a group is annotated uniformly or not by
      that rule at all. No rung reads iteration order or a list index, which is the property that
      matters: a label that varied between two runs of one comparison would be a worse defect than
      the one being fixed. The template role is read off the USR because `kind` cannot see it — the
      extractor maps `CLASS_TEMPLATE` and `CLASS_TEMPLATE_PARTIAL_SPECIALIZATION` both to the tag
      kind — and the pattern is anchored at the end of the string, because a template argument can
      itself be a USR and an unanchored read would report the argument's kind as the entity's.
      **The item grew a second defect on the way out, and the second is why the first was never
      visible.** `_subject` interpolated the label into rich markup unescaped, and `[` opens a
      markup tag, so `f [void (int)]` has printed as `f` since the bracketing was written — **the
      overload disambiguation had never once reached a terminal**, on any review, in any fixture.
      The same hole in `_completeness` was deleting part of the reason a model is incomplete
      (`[[nodiscard]]` rendering as `[]`), which is what ARCH §11.2 requires to be shown.
      `git log -S` puts both defects in `f6cf2a6`, the commit that introduced the bracketing:
      neither is a regression, they shipped broken together. The escape is positional and the
      whole file was
      swept rather than the two known sites — no string read off the document reaches the markup
      parser — because you cannot tell by looking at a string whether it will lose something. §5
      carries the general form.
- [x] ~~**Superseded cache identities are never reclaimed.**~~ `cppsd cache gc [--older-than N]
      [--dry-run]`, with `cppsd cache status` as the command that makes the number checkable
      before it is acted on. The hole was structural rather than a leak: `_evict` keeps the newest
      generations *within one TU directory* and only runs on a write, while `Cache.identity`
      hashes the producer code digest — so every change to `extract.py` or `ir.py` gives every TU
      a brand-new identity directory, and a directory nothing writes to again is immortal.
      Measured when it shipped: **1,156 entries / 229 MB, of which 576 entries / 147 MB were
      unreachable by any code path.** On this machine today the store reads 834 entries / 120 MB,
      757 entries / 100 MB of it unused 30 d+. Worktrees (63 MB) are reported and explicitly *not*
      collected: removing one is `git worktree remove` against the repository that owns it, and
      this command does not know which that is. Safe by construction — a collected entry is
      reparsed by the next run that wants it, which is §8's settled argument applied to an entry
      that was deleted rather than never written. It works only because a cache *hit* now touches
      its entry; without that the sweep would run backwards, since the TU that never changes is
      the one the cache serves most and writes least.
  - [ ] It keys on **age, not on identity**. The precise rule — delete every identity that is not
        the current producer's — needs a toolchain and a checkout to know what the current identity
        *is*, which would make cleaning the cache require a repository to clean it against. Age
        needs neither and collects the same directories one generation later, so a superseded
        generation survives until nothing has touched it for `--older-than` days.
- [x] ~~`CPPSD_RESOURCE_DIR` is keyed by path, not content.~~ **Resolved narrower than the item
      claimed, and no key was widened.** `clang-scan-deps` does run the compile-database command
      without our `-resource-dir` — but on an ordinary host the directory it resolves for itself is
      the one the override names: here `/usr/lib/clang/18` and `/usr/lib/llvm-18/lib/clang/18` are
      one directory through a symlink, the same inodes, and `_relative` resolves both spellings to
      the same file. Where that holds, every builtin header the parse reads is already
      content-hashed as a closure member, and a key component covering the directory a second time
      would defend an already-defended hazard at the price of a mass invalidation. That is §8's
      "check what the whole key already covers before widening an identity" holding literally, on
      the second hazard in a row. What escapes is a resource directory from a *different*
      installation than the scanner's — and there the cache declines to store rather than key
      builtin headers by path, which is §8 again from the other side. Checked rather than assumed:
      `_uncovered_resource_dir` asks whether any closure names a file under the override, which is
      exactly the difference between the two cases, and it returns immediately when the variable is
      unset.
- [ ] Worker startup is ~0.6 s each, of which the probe is 24 ms — the rest is
      `candidate_libraries()` re-running `llvm-config`/`ldconfig` that the parent already ran.
      Wants a `Toolchain.adopt(library, expected_id)`. **The second half of this item was a
      wrong number and is withdrawn**: it claimed the same cost made `tests/test_toolchain.py`
      "take 140 s of the suite's ~100 s wall", which does not parse — the suite is serial, there
      is no `xdist` in `pyproject.toml` — and measured today that file runs **22 tests in 6.3 s**
      against a full suite of 509 in 145 s. So this is worth doing for the producer and not for
      the suite, and the "one fix covers both" argument for prioritising it is gone. Third wrong
      literal found in this file in one pass (§4 has the other two); the pattern is a number
      written from an impression rather than from a run.
- [x] ~~`cli.py` has no `--workers` flag, though `build_model(workers=...)` and `$CPPSD_WORKERS`
      are live.~~ `--workers` / `-j` on `diff`, the only command that builds a model. Passed through
      as `None` when the flag is absent rather than as a count resolved in the CLI, so
      `produce.parallel.worker_count` stays the single place that orders flag > `$CPPSD_WORKERS` >
      cores — a default filled in at the CLI layer would shadow the environment variable on every
      run.
- [x] ~~`cache.notes()` quotes a stale per-TU cost (~2.6 s).~~ It measures rather than restates:
      the cache is constructed once per model and `notes()` is read after the last partial, so
      `time.monotonic()` across that window is this build's own production time. Divided by the TUs
      actually parsed, not by the invalidated ones alone, which would report several times the real
      cost on a mostly-cold build. No fresher constant replaced the old one, because the next
      constant goes stale the same way — the same ~2.6 s figure was also sitting in the module
      docstring, which is the part worth keeping: a number asserted in prose has no owner and no
      expiry, so one measurement had drifted in two places at once.
- [x] ~~Member function templates are classified `function`, not `method`, losing `explicit` with
      no other representation — 414 on fmt.~~ Clang files a template under `FUNCTION_TEMPLATE`
      whether its semantic parent is a namespace or a class, so a mapping keyed on the cursor kind
      cannot tell `fmt::format` from `basic_memory_buffer::append`. Membership is a property of the
      scope and is now read off the scope — the **semantic** parent, never the
      lexical one, and a friend function template is the case that decides it: it is written inside
      a class body and is not a member of it, and clang reports the class as its lexical parent and
      the enclosing namespace as its semantic one. 414 fmt nodes and 163 spdlog nodes changed class.
      The finding is that `explicit` needed no work of its own: the `clang_CXXMethod_*` predicates
      unwrap a `FunctionTemplateDecl` to the function it templates, so they had always answered
      correctly on these cursors, and the value was being discarded solely because `_attributes`
      keys on the node kind and `function` carries nothing that could hold it. Fixing the kind was
      the whole fix — 23 fmt and 2 spdlog nodes now carry `explicit` that carried nothing before.
      Measured before and after on one clone in one session: every event id, the kind histogram, the
      ranking order and all of events / high-risk / top-level / components / matched / residue
      identical on both fixtures, the only change in either document being `entities[*].kind`.
      Nothing in the codebase treats the two kinds differently — `rank/graph.py`'s container set is
      the only NodeKind-keyed rule and `diff/` never compares node kind — so a kind change cannot
      produce an event.
- [x] ~~`signature` is `-std`-dependent and nothing says so.~~ `Completeness.language_modes` now
      carries the `-std=` values each side was parsed at, and the view says so in yellow when the
      two differ. The dependence is unchanged and unfixable — `noexcept` is part of the function
      type from C++17 and not before — so the answer is to state it, per ARCH §11. leveldb
      `HEAD~9..~1` reads `base c++11 → head c++11, c++17`, and the mixed head is real: 20 vendored
      `benchmark` TUs stay at c++11 while leveldb's own 73 move to c++17. fmt still states no `-std`
      at all, which now shows as an empty list rather than as silence.

---

## 7 · Still open

- **Generated sources.** ARCH §3.1 states the problem, not the policy. Configure-only is the v0
  design; `clang-scan-deps` failed on 119 of opencv's 1,292 TUs (9.2 %) against an unbuilt tree,
  every one on a header CMake emits during the build. `fmt` and `spdlog` will not reveal where the
  threshold is.
- ~~**Whether tiers 2–4 are worth building at all.**~~ **Answered: no.** The third repository is in
  and the pairable bound on leveldb is **0** (§3). Still unknown on a repository mid-refactor, which
  is the one condition that would reopen it.
- **Where the reviewer actually reads this.** CLI is right for v0 and probably wrong as the
  product. The JSON-first contract defers the decision without prejudging it.
- **Unnamed (not anonymous) records have an unstable USR** — file basename plus a byte offset that
  moves on any edit above the record. A tier-1 hazard. Neither fixture has one.
- ~~**ARCH §6.3's cause table is missing a row.**~~ **Answered, and the table is now closed.**
  §6.3 names `call-moved-to-another-overload` with where it came from stated in place, says that it
  is the whole table, that the rows are `CAUSE_RULES` in `classify/rules.py` and its Status column
  is `RuleStatus`, and that order is part of the table — the first rule to find a cause wins, which
  matters now that two rules share `EdgeRemoved`. The question underneath it, whether a design
  document should list every rule the implementation happens to have, was answered yes for reasons
  specific to this table: a cause link is what render hides an event under, so a rule living only in
  the code is a reviewer's change disappearing for a reason the design cannot be read to explain.
  Two rows were also drifting from the code and were corrected against it — `rename-retargets-edges`
  is `UNREACHABLE` rather than "dead", and the signature rule is spelled
  `signature-change-breaks-callers`.
- **Should the model fingerprint surface anywhere a reader can see it?** It is in no CLI output and
  no JSON field; outside `differ.py`'s docstring its only callers are tests and ad-hoc scripts,
  which is a large part of how §4 carried two unreproducible values across two sessions. In the JSON
  it would let two runs be compared without re-running the tool, which is ARCH §12's reproducibility
  claim made checkable from outside. Against: it is an internal content hash with no meaning to a
  reviewer, and publishing it in the document makes it a contract that every future change to what
  `fingerprint()` hashes then breaks. Left open deliberately — §4's correction is an argument for
  making the number checkable, not yet an argument for where.
- **Should `rank/exposure.py` key on `Node.tu`?** Its docstring said it could not, because the
  producer never populated the field; that stopped being true (§6) and the docstring has been
  corrected, which leaves the actual question unasked rather than answered. `tu` is set only where
  the USR is file-scoped *and* the declaration is in the TU's own primary source, so it is `None`
  for almost every node — file-scoped USRs are 1.5 % of fmt's nodes and 10.3 % of leveldb's, and
  `tu` covers a subset of those. It could not classify a model on its own. What it might do is
  sharpen the INTERNAL/PUBLIC boundary where it *is* set: an entity nobody outside one TU can name
  is internal by the language rather than by directory convention, which is the half of the rule
  §5 records `port/port_stdcxx.h` getting wrong. Whether that is worth a second mechanism beside
  path convention is undecided; no exposure logic was changed.
- **`diff.renameLimit` can silently truncate the rename map.** Git skips *inexact* rename detection
  on large diffs and says so on stderr, which `run_git` discards on success — so the map would be
  incomplete with no signal, exactly the ARCH §11 case that wants the limit stated. `-l0` removes the
  cap but is potentially quadratic on the large refactor where it matters. Neither answer is
  obviously right, and the fixtures are too small to force the choice.

---

## 8 · Decisions — with the reason, so you can tell if it still holds

| Decision | Why |
|---|---|
| Python, standalone repo | CodeAtlas C++ is TypeScript only because it began as a VS Code extension. Ideas carry over from it, not modules. |
| Linux only during design and implementation | Windows support is dead weight while the shape is still moving. Portability rules stay (§9) — cross-platform *correctness* is cheap; cross-platform *support* is not. |
| A system libclang is a hard requirement | The wheel's bundled library truncates ASTs silently. Convenience is not worth a failure mode that produces two agreeing wrong models. |
| Base is `merge-base(main, HEAD)` | Against main's tip, every change other people merged while the branch was open gets attributed to this branch. There is a test for this; keep it. |
| Compile database required at **both** revs | Hard precondition, not a degradation path. Configure is seconds, so it is affordable. |
| Base that does not build ⇒ hard failure | The base is main. A main that does not build is a different problem, not this tool's. |
| JSON is the product, terminal is a consumer | Keeps delivery surfaces out of the analysis core — the mistake that made CodeAtlas TypeScript. `render`'s signature admits no `Model`. |
| v0 ships **tier-1 identity only** | Renames look bad on purpose. Measure the damage before building tiers 2–4. Done — see §3. |
| Identity tiers are one-to-one | Splits and merges surface as unmatched residue rather than being forced into a match. Enforced in the type, not just the matcher: a double-pairing does not crash, it produces a delta that reads perfectly and describes a change nobody made. |
| Cascade collapse at render, never at diff | JSON always carries the full event set; the view hides consequences under causes. When cause attribution is uncertain, show rather than hide. |
| `EdgeRetargeted` is refused, not deferred | An edge's id *is* `src\|kind\|dst`, so "retargeted from B to C" claims B and C are the same entity — a node-identity judgement the one-to-one matcher already declined. |
| The cache unit is the TU, not the commit | Per-commit caching means the first review against any new base pays full price, and the base is what every review shares. |
| Declining to cache beats a weaker key | A missing or version-mismatched `clang-scan-deps` disables the cache whole. Declining to cache is slow; a stale hit is wrong. |
| The cache key covers the extraction code, not just libclang | `extract.py` decides what becomes a node; change it and the same inputs yield a different partial. Hashed rather than a hand-bumped constant, because forgetting a constant fails toward serving stale entries. |
| Toolchain identity carries the target triple, never a host path | It is the one AST determinant the closure's content hashing cannot see. A path would break §12.2, which makes the identity a value two hosts *compare* — equivalent machines would hard-fail. |
| Check what the whole key already covers before widening an identity | The stdlib and builtin headers turned out to be content-keyed via the include closure. Every redundant key component costs a mass invalidation to defend against a defended hazard. |
| Workers use `spawn`, not `fork` | A forked child inherits the parent's verified libclang, so the toolchain guarantee would hold *by accident of start method* and stop holding silently if that default changed. |
| Low confidence ranks **up** | Uncertainty is a reason for a human to look, not a reason to hide (ARCH §7). Guarded by mutation-tested cases. |
| Vendored code is **partitioned** out of the ranking, not merely down-weighted | The reviewer is reviewing their own code; a bumped dependency is context, not the subject. As a weighted term it lost — measured on leveldb, where blast radius plus novelty (0.60) beat all of exposure (0.25) and put googletest in the top 12. Raising the weight would fossilize a number against one repository and still lose on the next. The claim is "not what is being reviewed", which is an ordering, not a quantity. Only VENDORED — the project's own tests are its code and keep the weighted demotion. |

---

## 9 · Conventions

- **No hardcoded paths.** A tool that analyzes arbitrary repositories on arbitrary machines cannot
  carry assumptions about either. Ask the system, then fall back; document fallbacks as fallbacks.
- **Cite ARCH sections in code comments** where a non-obvious choice traces to a design decision
  (`# ARCH-001 §4.3`). It is how the reasoning survives.
- **Docs carry a metadata block** — `id`, `type`, `status`, `author`, `created`, `dependencies`.
  IDs are `RESEARCH-00x` and `ARCH-00x`, unique, never recycled.
- **Commit messages** say what changed and *why it matters*. Record findings discovered by running
  the code; every finding in §5 was a real bug in waiting.
- **Tests assert the design, not the implementation.** The merge-base test exists because getting it
  wrong is invisible in output; the fingerprint test exists because "a moved entity is the same
  entity" is a claim ARCH makes; the cache-invalidation tests exist because a stale hit is the worst
  failure this system can have. Prefer tests of that kind. Do not pin float scores — it fossilizes
  weights that are meant to be tuned.
- Run `uv run ruff format . && uv run ruff check . && uv run pytest -q` before committing.
- **Reserve the shared contracts when work is split across concurrent agents.** `model/ir.py` and
  `model/events.py` are what every stage agrees on; letting several workstreams edit them
  concurrently produces four partial versions of one decision. Held as read-only with a
  stop-and-report rule, it caught four separate requests to change `Completeness` and `EventKind`,
  which were then done once as a coherent pass. **Commit coupled work together** — a stage emitting
  a new event kind and the ranker learning its weight are one commit, because either alone is a
  `KeyError`.
