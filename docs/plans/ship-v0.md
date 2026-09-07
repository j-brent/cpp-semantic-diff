<!-- metadata
id: PLAN-003
type: plan
status: in progress
author: Brent Runyan
created: 2026-09-07
dependencies: [ARCH-001, PLAN-002]
-->

# Shipping v0 — what remains, and what it costs

A status assessment taken 2026-09-07 after a five-week gap, against `dev` at `e346f29`. Every
number below was re-measured on this machine rather than read out of `STATUS.md`; where the two
disagree, that disagreement is the finding.

Delete this file when sections A through C are done and the remainder has moved into `STATUS.md`
§6 and §7 as ordinary tracked items.

---

## 1 · Verified state

**v0 is functionally complete.** Seven of eight stages exist; `enrich` is deferred by design
(ARCH §13). Everything checked is green, and the pipeline reproduces the tracker's headline
numbers exactly rather than approximately.

| Check | Result |
|---|---|
| `uv run pytest -q` | **490 passed** in 145 s |
| `uv run ruff format --check .` · `ruff check .` | clean, 62 files |
| `uv run cppsd doctor` | libclang 18.1.3 · `x86_64-pc-linux-gnu` · builtin headers parsed clean |
| `uv run cppsd diff ~/fixtures/fmt --base HEAD~12` | 179 lines → **16 events, 2 high-risk** · 11.2 lines/event · 7 top-level · 5 components · 17.0 s warm |
| identity residue, same run | 10,571 matched · 3 base / 3 head unmatched · `internal_linkage_residue` 3 |

That last row is the one worth noticing: it matches `STATUS.md` §3 to the digit, five weeks and one
machine-state later. The JSON document is at schema 1.2 and carries the completeness and residue
blocks ARCH §11.2 requires. 9,515 lines of source, ~7,700 of tests, 463 test functions.

The engineering claim "the hard part is done" holds.

---

## 2 · Nothing since 2026-08-04 has left this machine

This is the largest single risk in the repository right now, and it is not a code defect.

- **16 unpushed commits on `dev`** — everything from `feat(acquire): resolve renames from git`
  through `docs: Close out PLAN-001`.
- **`main` is 41 commits behind `dev`**, sitting at the state where only `acquire` and `model`
  exist. The published default branch does not contain the product.
- **CI last ran 2026-08-02.** It has never seen `produce`, `match`, `diff`, `classify`, `rank` or
  `render`. The `ci` workflow's "verify toolchain discovery on a foreign host" step exists
  precisely because library discovery is the code most likely to be tuned to one machine — and
  five weeks of work has not been through it.
- **`.github/workflows/demo.yml` publishes a false story on every push.** It runs `plan` and
  `acquire` only, and writes into the run summary: *"Two of eight pipeline stages exist …
  `produce`, `match`, `diff`, `classify`, `enrich`, `rank` and `render` are unimplemented."*
  That is the confidently-wrong artifact this project exists to argue against, aimed at itself.
  It was honest when written and became false without anyone editing it, which is the failure
  mode worth naming.

---

## 3 · Tracker drift — three §6 items are already closed

The last two commits fixed defects without updating the tracker, so the open list overstates the
remaining work. Each of these was verified against the code, not inferred from a commit subject.

- **`merge_edge` merges edge attrs with `setdefault`** — fixed in `4cb9290`. The merge now takes
  the best-informed observation under a total order (`_edge_attr_rank`). `STATUS.md` §6 still
  lists it open.
- **Superseded cache identities are never reclaimed** — `cppsd cache gc --older-than/--dry-run`
  shipped in `a645cee`. §6 still says it "needs a `cppsd cache gc`". The local cache is 120 MB
  with 828 entries unused 30 d+, so the sweep has something to do on this machine.
- **Self-edges appear in real models — "producer-side"** — **misdiagnosed, and it is not the
  producer.** `merge_edge` has dropped `src == dst` since `8080f19`. What was actually observed is
  the terminal printing
  `fmt::v12::detail::is_named_arg -> fmt::v12::detail::is_named_arg`, and the JSON for that same
  event shows two *different* USRs — `…@SP>1#T@is_named_arg>#1t0.0` and `…@ST>1#T@is_named_arg`.
  A partial specialization inheriting from its primary template. The edge is real and correct; the
  rendering collapses two entities onto one display name. This moves from `produce` to `render`
  and gets much smaller.

Related documentation drift: `rank/exposure.py`'s module docstring still states that "`Node.tu` is
declared in the IR and the current producer never populates it," which `07ed88b` changed.

---

## 4 · What remains

### A · Release hygiene — ~½ day

Push `dev`; watch CI on a foreign host and fix what it surfaces; rewrite `demo.yml` to run
`cppsd diff` and publish a real numerator; refresh `STATUS.md` §6 per section 3 above; then merge
`dev` → `main`. The merge goes last on purpose — merging first would publish the lying demo
workflow to the default branch.

### B · Small defect tail — ~1 day for all of it

| Item | Estimate |
|---|---|
| `cli.py` has no `--workers` flag, though `build_model(workers=…)` and `$CPPSD_WORKERS` are live | ~20 min |
| `cache.notes()` quotes a stale ~2.6 s per-TU cost (now ~1.5–2.0 s serial, ~0.5 s parallel) | ~10 min |
| Specialization rendering — section 3's third item | ~1 h |
| ARCH §6.3's cause table is missing the `call-moved-to-another-overload` row | ~30 min |
| `CPPSD_RESOURCE_DIR` is keyed by path, not content | ~1 h, or state it as a limit and move on |
| Member function templates classified `function`, not `method` — 414 on fmt | ~2–3 h |
| Exposure takes the max over both edge endpoints while blast radius takes the source | ~1–2 h, mostly the decision |

### C · Medium — ~2–3 days

- **`internal_linkage_residue` is computed over the whole model, vendored included.** leveldb is
  52.8 % vendored by node count. It lives on `Correspondence`, upstream of `rank` and its exposure
  map, so the placement decision comes before the code. ~½ day.
- **`Toolchain.adopt(library, expected_id)`** to kill the ~0.6 s per-worker rediscovery, which also
  cuts `test_toolchain.py`'s share of the 145 s suite. One fix, two payoffs. ~½ day.
- **`diff.renameLimit` can silently truncate the rename map.** `-l0` removes the cap and is
  potentially quadratic exactly where it matters; neither answer is obviously right and the
  fixtures are too small to force it. ~½ day, most of it judgement.
- **The `NodeAttrChanged(virtual)` → `EdgeAttrChanged` cause rule.** ~½ day, and **probably skip
  it**: §5 records `EdgeAttrChanged` firing zero times across 300 commits of each fixture. A rule
  built on it will look correct in review and do nothing.

### D · Named, not done — 3–5 days if taken

Two same-basename statics still merge into one node. Keying the node dictionary on `(usr, tu)`
reaches every edge endpoint, every event, every cache entry and the JSON contract. There is no
instance in any fixture; `tests/test_tu_identity.py` is the only place it exists. **Leaving it
named rather than half-done remains the right call** — but it should be a sentence in §6 that says
so, not an unchecked box that reads as a backlog item.

### E · PLAN-002, the Bitcoin Core probe — ~1–2 days

The highest-information work remaining. Its §5 prediction is confirmed: `acquire.configure()`
accepts `extra_args` and `cli.py` exposes no way to supply them, so `-DENABLE_WALLET=OFF` cannot be
passed from the command line. That is the finding PLAN-002 §7 anticipated as its one licensed code
change — a `--cmake-arg` passthrough, ~1 h, before the probe can start.

### F · Not an engineering item

Measured compression is **0.4:1 to 53:1**, and the one repository with real implementation files
*decompresses*. §7's "where does the reviewer actually read this" is still open. E is more likely
to reopen scope than to close it, which is what it is for.

---

## 5 · The estimate

- **~1.5–2 days** — A + B. A v0 that is pushed, CI-verified on a foreign host, merged to `main`,
  with a demo that tells the truth and a tracker that is accurate.
- **+2–3 days** — C.
- **+1–2 days** — E, which may change what comes after it.

"Almost finished" is true of the engineering. It is not yet true of the product question, and the
probe in E is the cheapest way to find out which.
