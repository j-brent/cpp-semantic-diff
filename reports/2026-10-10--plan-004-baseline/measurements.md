<!-- metadata
type: report
kind: frozen
frozen: 2026-10-10
author: Brent Runyan
created: 2026-10-10
dependencies: [PLAN-004]
-->

# PLAN-004 baseline and step 1 measurements

*Frozen document: measured 2026-10-10 at
[`42ccba3`](https://github.com/j-brent/cpp-semantic-diff/commit/42ccba3), before any PLAN-004
change. Not maintained.*

Two things are recorded here:

- **The baseline** that [PLAN-004 §5](../../docs/plans/PLAN-004-third-party-boundary.md#5--what-done-looks-like) compares against: "first-party change events identical to a
  run before this work".
- **Step 1** of [PLAN-004 §4](../../docs/plans/PLAN-004-third-party-boundary.md#4--steps): how many nodes each label would get, and what the unresolved
  in-repository callees are.

Fixture clones are the v0 tracker's, unfetched since 2026-08-01: `HEAD` is fmt `e2243a6a`,
spdlog `989d28dd`, leveldb `7ee830d0`. libclang 18.1.3, x86_64-pc-linux-gnu.

---

## 1 · Baseline

In [`baseline/`](baseline/). Each range has the JSON document (schema 1.2) and the terminal
view, made with:

```
NO_COLOR=1 COLUMNS=160 uv run cppsd diff ~/fixtures/<name> --base <base> [--head <head>] \
  --no-cache --limit 0 --out baseline/<name>-<range>.json > baseline/<name>-<range>.terminal.txt
```

| Range | Revisions | Change events | High-risk | `vendored_events` |
|---|---|---|---|---|
| fmt `HEAD~12` | `bcaa44d0` → `e2243a6a` | 16 | 2 | 0 |
| spdlog `HEAD~30` | `566b2d14` → `989d28dd` | 32 | 5 | 0 |
| leveldb `HEAD~9..~1` | `23e35d79` → `9e273a6a` | 6 | 3 | 117 |

The change-event counts equal the [v0 tracker](../2026-08-01--v0-build/STATUS.md) §2. The JSON's timings differ from run to run, the rest does not.

**Expected difference after PLAN-004, spdlog `HEAD~30` with `--third-party include/spdlog/fmt/bundled`:**
3 fewer change events. The three `EdgeRemoved` nested under `NodeRemoved spdlog::details::to_string_view`
lead into bundled fmt (`basic_string_view::basic_string_view`, `buffer::size`, `buffer::data`, all in
`include/spdlog/fmt/bundled/base.h`) and become crossings. Every other first-party change event stays.

leveldb `HEAD~10` is not kept. §5 expects it to be refused, so there is nothing to compare, and
its JSON is 7.9 MB (134 change events, 3,660 more in bundled code).

---

## 2 · Nodes per label

The rule as §3.1 proposes it, applied to every node's file: **third-party** if under a path in
the third-party set, else **untracked** if git does not track the file at that revision, else
**first-party**. The third-party sets are the ones §5 uses: `test/gtest` (fmt, declared),
`include/spdlog/fmt/bundled` (spdlog, declared), and leveldb's submodules
`third_party/benchmark` and `third_party/googletest`.

| Range | Side | Third-party | Untracked | First-party | Total |
|---|---|---|---|---|---|
| fmt `HEAD~12` | base | 3,347 | 0 | 7,227 | 10,574 |
| | head | 3,347 | 0 | 7,227 | 10,574 |
| spdlog `HEAD~30` | base | 1,595 | 0 | 884 | 2,479 |
| | head | 1,595 | 0 | 883 | 2,478 |
| leveldb `HEAD~9..~1` | base | 3,931 | 0 | 3,764 | 7,695 |
| | head | 3,980 | 0 | 3,764 | 7,744 |

- **Untracked is zero everywhere.** The build directory is inside each worktree, but no parsed
  translation unit draws a declaration from it. leveldb does include one generated header,
  `build/include/port/port_config.h`, and it holds only macros, which the model does not
  record. None of the three fixtures uses FetchContent.
- **Third-party is a third to over half of each model:** 32 % of fmt, 64 % of spdlog, 51 % of
  leveldb.
- **leveldb's head has 49 more third-party nodes than its base**, and the same first-party
  count. These are the 49 head-side unmatched entities the v0 tracker §3 found, all in googletest.

---

## 3 · Unresolved in-repository callees

An edge whose target is declared in a file inside the repository, yet resolves to no node.
§3.3's working claim is that all of these are compiler-generated members. Counted as distinct
targets, grouped by what the declaration is:

| Range | Side | Label | Implicit special member | Lambda member | Local class member | Template instantiation | Builtin | Other | Total |
|---|---|---|---|---|---|---|---|---|---|
| fmt `HEAD~12` | both | first-party | 231 | 119 | 122 | 741 | 3 | 17 | 1,233 |
| | | third-party | 30 | 2 | 0 | 25 | 0 | 1 | 58 |
| spdlog `HEAD~30` | base | first-party | 15 | 11 | 8 | 0 | 0 | 0 | 34 |
| | | third-party | 34 | 4 | 8 | 45 | 2 | 6 | 99 |
| leveldb `HEAD~9..~1` | base | first-party | 46 | 0 | 6 | 2 | 1 | 0 | 55 |
| | | third-party | 50 | 19 | 0 | 7 | 0 | 1 | 77 |

spdlog's and leveldb's head sides differ from their bases by at most three targets per row.
Both sides of every range are in [`measure.json`](measure.json), with examples of each group.
The probe also had a group for instantiations whose template *is* a node. It never occurs on any
fixture, because such an edge resolves to the template.

**The claim does not hold.** Implicit special members are 19 % of fmt's first-party targets.
The rest are:

- **template instantiations whose alias chain reaches no node**, mostly member templates of
  class templates, e.g. `buffer::append`, `dynamic_format_arg_store::push_back`. This is the
  largest group on fmt;
- **members of lambdas**: `operator()` and the conversion to a function pointer;
- **members of classes declared inside a function**, e.g. leveldb's `LogReporter` in
  `db/db_impl.cc`, and the `FMT_COMPILE_STRING` struct that fmt's macro declares;
- **compiler builtins** declared where they are first used, e.g. `__builtin_va_copy`;
- **other**: inherited constructors (`format_error::runtime_error`), friend operators defined in
  a class (`detail::operator~`), and alias templates libclang reports as call targets
  (`conditional_t`).

Override edges are not in these counts: the probe sees calls and bases only. 6 (fmt) and 10
(leveldb) override edges into the repository are dropped too.

---

## How it was measured

[`measure.py`](measure.py) builds each model as `cppsd diff` does, serially and without the
cache, and records every edge target's declaration as the extractor meets it. "Implicit special
member" means a constructor, destructor or assignment declared at its class's own location,
which is where clang places members it generates. "Tracked" means listed by
`git ls-tree -r` at that revision.

```
uv run python reports/2026-10-10--plan-004-baseline/measure.py leveldb spdlog fmt \
  | jq -s add > reports/2026-10-10--plan-004-baseline/measure.json
```
