<!-- metadata
type: report
kind: frozen
frozen: 2026-10-11
author: Brent Runyan
created: 2026-10-11
dependencies: [PLAN-004]
-->

# PLAN-004 done: the §5 checks, measured

*Frozen document: measured 2026-10-11 at
[`62dc064`](https://github.com/j-brent/cpp-semantic-diff/commit/62dc064), after every PLAN-004
step was merged into `dev`. Not maintained.*

[PLAN-004](https://github.com/j-brent/cpp-semantic-diff/blob/62dc064/docs/plans/PLAN-004-third-party-boundary.md)
(linked at its last version; it is deleted now that it is built) put third-party code outside
the model. Its §5 lists what "done" looks like. This report records each check, run on current
`dev`, against the baseline in the [step 1 report](../2026-10-10--plan-004-baseline/measurements.md).

Fixture clones are the same as the baseline's, unfetched since 2026-08-01: `HEAD` is fmt
`e2243a6a`, spdlog `989d28dd`, leveldb `7ee830d0`. libclang 18.1.3, x86_64-pc-linux-gnu.

**Pending at the time of measuring:**
[#67](https://github.com/j-brent/cpp-semantic-diff/pull/67), which makes the extractor descend
into `extern "C"` blocks, whose declarations were never modelled (leveldb's C API in `db/c.cc`
and `include/leveldb/c.h`, and three blocks in spdlog). Every number below is from before #67.
Its own measurements report the same change events on all three ranges, and more nodes on
leveldb and fmt, so the node counts in §3 will not hold after it.

---

## 1 · The checks

| Check | Expected | Measured | Holds |
|---|---|---|---|
| fmt `HEAD~12`, `--third-party test/gtest` | runs; no node under `test/gtest`; crossings labelled `test/gtest`; first-party change events identical to the baseline | exit 0. 0 nodes under `test/gtest` on either side; 5,371 crossings per side labelled `test/gtest`; 1 of 50 TUs third-party and not parsed. 17 change events against the baseline's 16: the 16 are identical, and the 17th is `NodeAttrChanged(replacement_hash)` on the macro `FMT_VERSION`, which the model did not hold at baseline (§2) | yes |
| spdlog `HEAD~30`, `--third-party include/spdlog/fmt/bundled` | runs; bundled fmt no longer modelled; first-party change events unchanged | exit 0. 0 nodes under the path; 170 crossings into it at base, 167 at head. The three `EdgeRemoved` into bundled fmt that the baseline predicted are gone; the other 29 baseline change events are unchanged. 52 more change events come from model growth merged since the baseline (§2) | yes, with §2 |
| spdlog `HEAD~30`, no option | runs, and notes that `include/spdlog/fmt/bundled` looks like third-party code | exit 0, with the note. 84 change events: the 81 above plus the three edges into bundled fmt, and nothing else | yes |
| leveldb `HEAD~10` | refused, naming `third_party/googletest` and `third_party/benchmark` | exit 3, before any checkout: "Third-party code changed between the two revisions: third_party/benchmark, third_party/googletest." | yes |
| leveldb `HEAD~9..~1` | runs; the 49 head-side unmatched entities, all in googletest, are gone | exit 0. Unmatched 0 base / 0 head (baseline: 0 / 49). 24 of 93 TUs third-party and not parsed; 1,999 crossings per side labelled `third_party/googletest`. 6 change events, identical to the baseline's 6 first-party ones; the baseline's 117 vendored change events are gone | yes |
| `scripts/ci.sh` (formatting, lint, types, tests) | clean | passes on the commit that adds this report | yes |

The outputs are in [`runs/`](runs/): the JSON document (schema `2.0.dev`) and the terminal view
of each range, and the refusal for leveldb `HEAD~10`. The node and crossing counts come from
[`probe.py`](probe.py), whose output is [`probe.json`](probe.json): the JSON document lists only
the entities change events reference, so it cannot show that no node lies under a path.

**What isolates PLAN-004's own effect.** On current `dev`, the same range run with and without
the option differs by exactly what the boundary should remove: spdlog by the three edges into
bundled fmt (84 against 81), and fmt by nothing (17 against 17, the run without the option in
[`runs/fmt-head12-noopt.json`](runs/fmt-head12-noopt.json), made with the cache on), since googletest does not change
in that range.

---

## 2 · What else changed the change events

Step 6 measured spdlog at 33 change events, 7 high-risk, against the baseline's 32 and 5;
current `dev` gives 81 and 13. To find which merge did what, spdlog `HEAD~30`
with `--third-party include/spdlog/fmt/bundled` was run at every feature merge into `dev` since
the baseline:

| `dev` at | Merged | Change events | High-risk | What changed |
|---|---|---|---|---|
| `42ccba3` | baseline | 32 | 5 | |
| `638434e` … `cf42cbe` | #43 name tier, #51 inline-namespace tier, #46 what the model cannot see, #50 callers of a changed signature | 32 | 5 | nothing |
| `1d72569` | #48 PLAN-004 step 4: extraction boundary | 29 | 5 | − the three `EdgeRemoved` from `spdlog::details::to_string_view` into bundled fmt, now crossings, as the baseline predicted |
| `8585b96` | #53 | 29 | 5 | nothing |
| `fa27d8b` | #52 compare more on existing entities | 33 | 7 | + 2 `NodeAttrChanged(default_arguments)` on the two `pattern_formatter` constructors, both high-risk; + 2 `NodeBodyChanged` on the `base_sink` and `ansicolor_sink` constructors, because a constructor's initializer list became part of its body hash |
| `a122612` | #55 PLAN-004 step 5: no vendored ranking | 33 | 7 | nothing |
| `9e21c1d` | #54 enums, aliases, variables, macros, static assertions | 81 | 13 | + 3 `NodeAdded` macros (`SPDLOG_NAMESPACE`, `_BEGIN`, `_END`); + 16 `NodeAttrChanged(replacement_hash)` on macros (`SPDLOG_LEVEL_NAME_*`, `SPDLOG_LOGGER_*` and the like); + 29 `NodeAttrChanged(sugared_signature)`, 6 of them high-risk |
| `8a9b6fd`, `67d24cc`, `62dc064` | #60 step 6, #65, #66 | 81 | 13 | nothing |

Step 6's 33 and 7 is `fa27d8b`'s result: the step 6 branch was measured before #54 reached it.

**None of the 52 additions is a defect of the boundary.** Each is a real edit in spdlog's
source, reported the way the ADR that added the comparison says. In this range spdlog wrapped
its headers in `SPDLOG_NAMESPACE_BEGIN` / `SPDLOG_NAMESPACE_END` and dropped the `spdlog::`
qualifier they no longer need. For example, `base_sink-inl.h` went from

```cpp
SPDLOG_INLINE spdlog::sinks::base_sink<Mutex>::base_sink(
    std::unique_ptr<spdlog::formatter> formatter)
```

to

```cpp
SPDLOG_INLINE sinks::base_sink<Mutex>::base_sink(
    std::unique_ptr<formatter> formatter)
```

- `sugared_signature` is the signature as written. ADR-038 says an event for it alone "means the
  spelling changed and the type did not", which is every one of these 29.
- `default_arguments` is each default as clang prints it (ADR-043), so
  `spdlog::details::os::default_eol` → `details::os::default_eol` is a change.
- Bodies are compared as tokens, so a dropped qualifier in an initializer list is a change.
- The 16 macros' replacement lists changed. Those checked, such as `SPDLOG_LEVEL_NAME_TRACE`
  and `SPDLOG_INFO`, now write `SPDLOG_NAMESPACE::` where they wrote `spdlog::`, a real change:
  the namespace became configurable.

**A finding for ranking, not for this plan.** The 29 signature changes and the 2 default-argument
changes name the same types and values as before. Yet the two `default_arguments` events are the
top two of the 81, and 8 of the 13 high-risk change events are among these 31. Whether a change
of spelling alone should rank that high is a question for the weights in `rank/weights.py`.

---

## 3 · The models

From [`probe.json`](probe.json). Crossings are counted per distinct (caller, callee, kind).

| Range | Side | Nodes, before #67 | Under the third-party set | Crossings into it | Other crossings |
|---|---|---|---|---|---|
| fmt `HEAD~12` | base | 8,618 | 0 | 5,371 `test/gtest` | 2,252 |
| | head | 8,618 | 0 | 5,371 | 2,252 |
| spdlog `HEAD~30` | base | 1,034 | 0 | 170 `include/spdlog/fmt/bundled` | 772 |
| | head | 1,036 | 0 | 167 | 776 |
| leveldb `HEAD~9..~1` | base | 4,179 | 0 | 1,999 `third_party/googletest` | 1,763 |
| | head | 4,179 | 0 | 1,999 | 1,763 |

The other crossings are labelled `C++ standard library`, `C library`, `compiler builtins`,
`implicit`, `function-local`, `template instantiation` and `unresolved`; `probe.json` has the
count for each. None is labelled `untracked`, as the step 1 report predicted.

**Exposure.** With googletest left out, fmt's model at `HEAD~12` is 69 % test code: 5,918 of
8,618 nodes at base are TEST, 2,694 PUBLIC and 6 INTERNAL.

**The JSON's crossings.** Measured by step 6 on fmt `HEAD~12` with `--third-party test/gtest`:
every crossing would be 7,635 per side with 22,623 sites, about 3.4 MB of JSON per side against a
40 KB document. The document lists only the crossings out of entities that change events
reference: 30, about 15 KB. Step 6's 7,635 and the 7,623 in the table above (5,371 + 2,252) were
taken at different commits; the code between them changed what is extracted.

---

## How it was measured

Each range, from the repository root at `62dc064`:

```
NO_COLOR=1 COLUMNS=160 uv run cppsd diff ~/fixtures/<name> --base <base> [--head <head>] \
  [--third-party <path>] --no-cache --limit 0 --out runs/<name>-<range>.json \
  > runs/<name>-<range>.terminal.txt
```

The model counts, with the cache on:

```
uv run python reports/2026-10-11--third-party-boundary/probe.py fmt HEAD~12 HEAD test/gtest
uv run python reports/2026-10-11--third-party-boundary/probe.py spdlog HEAD~30 HEAD include/spdlog/fmt/bundled
uv run python reports/2026-10-11--third-party-boundary/probe.py leveldb HEAD~9 HEAD~1
```

The table in §2 ran the first command for spdlog in a `git archive` export of each merge, with a
fresh cache directory. Change events are compared by kind and subject, the identity a change
event keeps across runs.
