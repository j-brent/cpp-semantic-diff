<!-- metadata
id: PLAN-001
type: plan
status: in progress
author: Brent Runyan
created: 2026-08-02
dependencies: [ARCH-001]
-->

# Renames and the defining TU

Closing the two identity defects that make the tool report changes nobody made.

Scope is deliberately narrow: the design and the measurement protocol for one piece of work. What
is *built* is in [`STATUS.md`](../../STATUS.md) §1, what is *broken* is §6, and what was *decided*
is §8 — none of it is repeated here. This document exists for the part that does not fit a tracker
bullet: why the two defects cannot be split, what the risky one can break, and how to tell whether
it worked.

Delete it when the last commit lands and its measurements are in `STATUS.md` §3.

---

## 1 · The two defects are one piece of work

**`match_models(renames=...)` is never fed.** ARCH-001 §5 requires file renames resolved from git
*before* matching, because clang writes the source file name into the USR of every internal-linkage
entity — one renamed file breaks tier-1 identity for all of them at once. The parameter has existed
and been tested since the matcher was written.

**`Node.tu` is declared and never populated.** So `RenameMap.by_path`, the half of the rename
machinery that handles *directory moves*, is unreachable. Feeding `renames` alone fixes basename
renames only.

They are listed as two bullets in `STATUS.md` §6 and read as independent. They are not: the first is
half a fix without the second. But they are not symmetric either, and that asymmetry sets the order.

**Feeding `renames` can only help.** `DeclaredTier.key` rewrites base-side keys; a rewrite that
finds no head-side counterpart leaves both nodes in the residue where they already were.

**Populating `Node.tu` can hurt.** It makes tier 1 *stricter* for every file-scoped USR. Where the
producer's choice of TU is arbitrary — a `static` declared in a *header*, seen by N TUs and deduped
to one node — the arbitrary winner can flip between revisions and destroy matches that work today.
`match/declared.py` already anticipates this and calls it "the safe direction to fail", but safe
there means *over-reporting*, which is the number `STATUS.md` §3 publishes.

---

## 2 · What is settled

Established by reading the code and probing the fixtures, not assumed:

1. **Git already does the detection.** `diff.renames` defaults on since git 2.9. `--name-only` was
   discarding an answer git had already computed; `--name-status` recovers it. A query change, not
   new machinery.
2. **The compression denominator does not move.** `diff_line_count` uses `--numstat`, which already
   scores a pure rename `0 0`. Leave it alone.
3. **The extractor already anticipates `Node.tu`.** `_observation_rank` counts `tu` in its `known`
   component *and* carries it in the content tie-break, so `merge_node` stays order-independent once
   `tu` is populated. No new merge rule is needed — add a sentence to the docstring saying the `tu`
   tie-break is what decides the ambiguous case, and no code.
4. **Populating `tu` invalidates every cached model**, because `_producer_code_digest` hashes the
   source of `ir.py` and `extract.py` by design. That is the key working, not a problem — but it
   orphans a whole generation of identity directories, which is why GC rides along.
5. **fmt's own history has the test cases.** `f924d20d` (`test/core-test.cc` → `test/base-test.cc`,
   R100) and `79f1506f` (`include/fmt/core.h` → `include/fmt/base.h`, R100, 11 files).

---

## 3 · Step design

### 3.1 · `acquire` resolves renames — **done** (`267660b`)

`file_changes()` runs `git diff --name-status --find-renames=<n>% -z` and returns `FileChange`
records; `.renames` is the base→head map, `.paths` is what `--name-only` printed. `changed_files`
became a view over it, so one parser knows the shape of a change record.

Three things that shipped and are worth not undoing:

- **Its own runner.** `run_git` decodes with `errors="replace"`, which maps two different
  undecodable paths onto one string — a mangled label in `changed_files`, a *fabricated identity* in
  a rename map. `run_git_z` uses `surrogateescape`. Tested with real `a\xe9.cpp` / `a\xff.cpp`
  paths, because this failure is invisible on any ASCII fixture.
- **`--find-renames` passed explicitly** though detection defaults on, so a reviewer with
  `diff.renames=false` in config cannot silently get a different review (ARCH-001 §12).
- **`--find-copies` deliberately absent.** A copy leaves the base file in place; claiming the
  original moved would be a node-identity judgement made from a file-level heuristic.

A malformed `$CPPSD_RENAME_SIMILARITY` is **fatal**, unlike `$CPPSD_WORKERS`, which is ignored on a
typo. The worker count changes the speed; this changes which files count as the same file.

### 3.2 · Wire it at the call site

`cli.py` — `changes = file_changes(repo, revs)`, `files = changes.paths` into the existing suffix
filters, `renames=changes.renames` into `match_models`. The nine-line comment stating the gap is
deleted rather than amended: it exists to make the gap visible, and the gap will be gone. Its ARCH §5
citation survives in the short note that replaces it.

`plan` switches too and gains a rename count — its whole purpose is answering "what would be
analyzed" without analyzing it, and renames are now part of that.

Do **not** filter the map to C++ suffixes here. `RenameMap.of` already drops the ambiguous case, and
filtering would put an identity policy in the orchestration layer.

`match/declared.py`'s docstring says "v0 has no caller passing one yet" and must name
`acquire.file_changes` instead.

### 3.3 · Populate `Node.tu` — the risky one, and it goes last

**The rule is narrower than "wherever the USR is file-scoped".** Two conditions:

1. `usr_file(node.id) is not None`, and
2. **the declaration is in the TU's own primary source file.**

Condition 2 is the load-bearing one and it is a judgement call, not a derivation. It declines to
invent a TU for header-declared internal-linkage entities, where there is no single true answer and
the arbitrary winner flips when an unrelated source file is added — breaking tier 1 for an entity the
review never touched. It trades some §5 coverage for not introducing a new source of
revision-flippable identity. **Settle it with the matrix in §5, not by argument.**

`tu` must be **repo-relative**. An absolute path carries the SHA-named worktree directory into every
partial, base and head never agree, and every internal-linkage match breaks on every comparison —
the same class of bug `STATUS.md` §5 records as the extractor leaking the host path into identity.

**`usr_file` must move to `model/ir.py` specifically** — not merely somewhere under `model/`.
`_producer_code_digest` hashes exactly two modules, `ir.py` and `extract.py`. A USR-format helper
that the producer's stored output depends on, living anywhere else, is a cache-key hole of precisely
the shape `STATUS.md` §5 already records: change it and every stored partial becomes wrong while
every key stays byte-identical. Re-export from `match/declared.py` so its callers are untouched.

**`Model.fingerprint()` must learn about `tu` too.** It hashes id, kind, signature, body_hash, attrs
and edge ids — not `tu` — while its docstring claims two models with the same fingerprint produce an
empty delta, which `diff/differ.py` relies on. After this change a pure directory move of a source
file containing a static changes identity but nothing the fingerprint hashes, so the claim becomes
false. Hash `tu` under the *same* file-scoped condition `DeclaredTier.key` uses, so there is one rule
about when the TU is identity. This changes fmt's recorded fingerprint; no test pins a literal, so
nothing breaks, but `STATUS.md` §4 must be re-measured.

Do **not** bump `ENTRY_FORMAT`. The payload shape is unchanged, and bumping orphans the whole store
directory instead of superseding per-identity directories. `_producer_code_digest` invalidates
everything on its own.

### 3.4 · Cache GC, riding with 3.3

`Cache.identity` hashes the producer code digest, and the layout is
`tu-models/v1/<shard>/<identity>/`. Changing `extract.py` gives every TU a brand-new identity
directory, and the old ones are never written to again — so `_evict`, which trims within a single TU
directory, can never reach them. **The sweep must be by age across the whole tree, not
per-directory.**

---

## 4 · Commit split

`Node.tu` goes last and alone because it is the only step that can make the numbers worse.

| | Commit | State |
|---|---|---|
| 1 | `acquire`: resolve renames from git | done, `267660b` |
| 2 | `cli`: feed the matcher — **where the defect closes** | done, measured in §5 |
| 3 | `render`: state the rename count in the document | |
| 4 | `model`/`produce`: populate `Node.tu`, plus cache GC | |
| 5 | docs: fold the measurements into `STATUS.md`, delete this file | |

**If commit 4 regresses the residue even under the narrow rule, stop and ship 1–3.** The rename fix
stands alone, and it is the part that produces wrong output rather than noisy output.

---

## 5 · How to tell whether it worked

Measurement, not green tests. `Correspondence.internal_linkage_residue` is already surfaced in both
the JSON document and the terminal view, so it reads straight off the tool:

```sh
uv run cppsd diff ~/fixtures/fmt --base 79f1506f~1 --head 79f1506f --json --out before.json
```

**Measured, commit 2.** `da256982` (`src/posix.cc` → `src/os.cc`, 2019):

| | before | after |
|---|---|---|
| `internal_linkage_residue` | 2 | **0** |
| unmatched base / head | 1 / 1 | 0 / 0 |
| events reported | 8 | **0** |
| high-risk | 6 | **0** |

All eight events were **one unchanged function** — `convert_rwcount`, in the anonymous namespace of
the renamed file: `NodeRemoved` + `NodeAdded`, its two call edges each removed and re-added, and its
`contains` edge. Six scored high-risk. Zero is the correct answer here: the commit also moved
declarations from `posix.h` into `os.h`, and external-linkage USRs do not encode the file, so a
declaration changing headers is correctly no event at all.

**The commit this plan originally named was the wrong one, and the reason is worth keeping.**
`79f1506f` (`include/fmt/core.h` → `base.h`) measured **0 before and 0 after**. The prediction that
its unnamed enums and unions carry file-scoped USRs was wrong: clang spells an *anonymous* record's
USR `…@Ua`, not `c:<file>@<offset>`, so those entities were never file-scoped. `f924d20d` is 0 for
the reason predicted — gtest `TEST(...)` macros expand to external-linkage classes.

**The bound that finding puts on the defect.** fmt has **154 file-scoped USRs out of 9,752 nodes**
(1.6 %), and 144 of them are in vendored `gmock-gtest-all.cc`. So the defect is *rare but severe*:
most renames touch nothing with internal linkage and cost nothing, and a rename that does hit one
turns a single unchanged function into eight events, most of them high-risk. Judging it by how often
it fires would have been the wrong call — a reviewer who meets it sees a screen of invented work.

Method note for the remaining commits: `internal_linkage_residue` is 0 on most real renames, so it
cannot be used as a smoke test. Pick the fixture commit by first checking that the renamed file
contains a `static` at namespace scope or an anonymous namespace.

**Separate the two defects — four cells, because they can mask each other:**

| | `tu` unpopulated | `tu` populated |
|---|---|---|
| **renames not fed** | today's baseline | **must not regress** |
| **renames fed** | expect the drop | expect the same drop |

The top-right cell decides the narrow-vs-broad `tu` rule. Run the broad rule once as a throwaway: if
broad regresses and narrow does not, that is a `STATUS.md` §5 finding of exactly the kind that is
invisible on synthetic fixtures.

Then re-run the §4 fingerprint check across worker counts and cache states. `Node.tu` changes the
fingerprint *by design*, so the assertion is that the new value is stable, not that it matches the
old one — and confirm the ~30 s warm budget survives the forced cold rebuild.

---

## 6 · Holes this work does not close

- **`diff.renameLimit`.** Git silently skips *inexact* rename detection on large diffs, warning on
  stderr — which `run_git` discards on success. The map would then be incomplete with no signal,
  which is an ARCH-001 §11 "state the limit" case. `-l0` makes it unlimited but potentially quadratic
  on exactly the large refactor where it matters. Neither answer is obviously right.
- **Two statics sharing a basename in different directories** still merge to one node, because
  `usr_file` returns a file *name*, not a path. The real fix is keying nodes on `(usr, tu)` rather
  than the USR, which reaches every edge endpoint, every event, every cache entry and the JSON
  contract. Far out of scope, and better named than half-done.
