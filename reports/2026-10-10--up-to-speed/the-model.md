<!-- metadata
type: report
kind: frozen
frozen: 2026-10-10
author: Brent Runyan
created: 2026-10-10
-->

# The model

*Frozen document: the 2026-10-10 review. Not maintained; the work it found is in [`ROADMAP.md`](../../ROADMAP.md).*

**Created:** 2026-10-10
**Last modified:** 2026-10-10 16:26 CEST

Where the model was defined, and how far it can be trusted to be sufficient. Part of the
2026-10-10 project review: see [`project-review.md`](project-review.md) and
[`known-issues.md`](../../ROADMAP.md#2--confirmed-work) (since moved into the roadmap).

## Sources

Every source below was read at repository commit
[`4d8e367`](https://github.com/j-brent/cpp-semantic-diff/commit/4d8e3671cb464690ce3059773d5b4390001d550b)
(`4d8e3671cb464690ce3059773d5b4390001d550b`, the head of `dev` and `main` on 2026-10-10). The
table also gives the commit that last changed each file.

| Source | In this repository | On GitHub, at `4d8e367` | Last changed in |
|---|---|---|---|
| RESEARCH-002, founding research note | [`docs/research/semantic-diff-for-review.md`](../../docs/research/RESEARCH-002-semantic-diff-for-review.md) | [link](https://github.com/j-brent/cpp-semantic-diff/blob/4d8e3671cb464690ce3059773d5b4390001d550b/docs/research/semantic-diff-for-review.md) | [`e4cafac`](https://github.com/j-brent/cpp-semantic-diff/commit/e4cafac61486beb62f0f56698d166762bbb4e3cd), 2026-08-01 |
| ARCH-001, architecture | [`docs/architecture/overview.md`](../../docs/ARCH-001-architecture.md) | [link](https://github.com/j-brent/cpp-semantic-diff/blob/4d8e3671cb464690ce3059773d5b4390001d550b/docs/architecture/overview.md) | [`08db50e`](https://github.com/j-brent/cpp-semantic-diff/commit/08db50ebf924b833910d72b29f49060b47e69c39), 2026-09-20 |
| Model types | [`src/cpp_semantic_diff/model/ir.py`](../../src/cpp_semantic_diff/model/ir.py) | [link](https://github.com/j-brent/cpp-semantic-diff/blob/4d8e3671cb464690ce3059773d5b4390001d550b/src/cpp_semantic_diff/model/ir.py) | [`07ed88b`](https://github.com/j-brent/cpp-semantic-diff/commit/07ed88b1cc8d3d63754789a7577ae61147f85860), 2026-08-04 |
| Extractor | [`src/cpp_semantic_diff/produce/extract.py`](../../src/cpp_semantic_diff/produce/extract.py) | [link](https://github.com/j-brent/cpp-semantic-diff/blob/4d8e3671cb464690ce3059773d5b4390001d550b/src/cpp_semantic_diff/produce/extract.py) | [`826209c`](https://github.com/j-brent/cpp-semantic-diff/commit/826209c54da91e883ab120eaf020cf36e1f1ba8b), 2026-09-20 |
| Extractor tests | [`tests/test_extract.py`](../../tests/test_extract.py) | [link](https://github.com/j-brent/cpp-semantic-diff/blob/4d8e3671cb464690ce3059773d5b4390001d550b/tests/test_extract.py) | [`826209c`](https://github.com/j-brent/cpp-semantic-diff/commit/826209c54da91e883ab120eaf020cf36e1f1ba8b), 2026-09-20 |
| Parser setup check | [`src/cpp_semantic_diff/produce/toolchain.py`](../../src/cpp_semantic_diff/produce/toolchain.py) | [link](https://github.com/j-brent/cpp-semantic-diff/blob/4d8e3671cb464690ce3059773d5b4390001d550b/src/cpp_semantic_diff/produce/toolchain.py) | [`dc8738e`](https://github.com/j-brent/cpp-semantic-diff/commit/dc8738e80608551babf96f1423f82ab416aa7187), 2026-09-20 |

---

The short answers: the model was specified in two design documents and then cut down deliberately for v0. And no, nobody has shown that it's sufficient.

## Where the model was defined

It was defined in four layers, each narrower than the one before:

1. **[`docs/research/semantic-diff-for-review.md`](../../docs/research/RESEARCH-002-semantic-diff-for-review.md) (RESEARCH-002), the founding research note.**
   - [§3](https://github.com/j-brent/cpp-semantic-diff/blob/4d8e3671cb464690ce3059773d5b4390001d550b/docs/research/semantic-diff-for-review.md?plain=1#L84) lists **twelve questions a reviewer is trying to answer**: did the API contract change, is it noise, what is the blast radius, was the change complete, and so on.
   - [§5](https://github.com/j-brent/cpp-semantic-diff/blob/4d8e3671cb464690ce3059773d5b4390001d550b/docs/research/semantic-diff-for-review.md?plain=1#L300) claims they can be answered as "a graph diff over a semantic model of the codebase".
   - It says what a model needs in general terms only. It doesn't list entity or relationship types.

2. **[`docs/architecture/overview.md`](../../docs/ARCH-001-architecture.md) (ARCH-001) [§4](https://github.com/j-brent/cpp-semantic-diff/blob/4d8e3671cb464690ce3059773d5b4390001d550b/docs/architecture/overview.md?plain=1#L140), the full design.**
   - It defines **14 entity kinds and 14 relationship kinds** ([§4.1](https://github.com/j-brent/cpp-semantic-diff/blob/4d8e3671cb464690ce3059773d5b4390001d550b/docs/architecture/overview.md?plain=1#L146), [§4.2](https://github.com/j-brent/cpp-semantic-diff/blob/4d8e3671cb464690ce3059773d5b4390001d550b/docs/architecture/overview.md?plain=1#L171)). Beyond what you've seen, the entities include enum, enumerator, alias, variable, template, file and build target. The relationships include instantiates, has-a, holds-a, includes and needs-symbol.
   - The relationship kinds come from an earlier project's list of 61 C++ relations (the [CodeAtlas C++](https://github.com/j-brent/codeatlas-cpp) ontology, which lives in another repository, not this one). The only reason given for which ones were picked is "a subset chosen for diff-relevance".
   - Its one firm rule is that every field must be identity, comparable state, or source location.

3. **ARCH-001 [§13](https://github.com/j-brent/cpp-semantic-diff/blob/4d8e3671cb464690ce3059773d5b4390001d550b/docs/architecture/overview.md?plain=1#L587), the v0 cut.**
   - It keeps namespace, class, function, method and field, plus contains, calls, inherits and overrides.
   - The stated reason is "the smallest slice that tests the thesis end to end". So the slice was chosen to test whether the idea works at all, not to be complete.

4. **The code.**
   - [`src/cpp_semantic_diff/model/ir.py`](../../src/cpp_semantic_diff/model/ir.py) declares the full 14 + 14 vocabulary ([`NodeKind`](https://github.com/j-brent/cpp-semantic-diff/blob/4d8e3671cb464690ce3059773d5b4390001d550b/src/cpp_semantic_diff/model/ir.py#L56), [`EdgeKind`](https://github.com/j-brent/cpp-semantic-diff/blob/4d8e3671cb464690ce3059773d5b4390001d550b/src/cpp_semantic_diff/model/ir.py#L73)).
   - [`src/cpp_semantic_diff/produce/extract.py`](../../src/cpp_semantic_diff/produce/extract.py) ([`MODELLED_KINDS`](https://github.com/j-brent/cpp-semantic-diff/blob/4d8e3671cb464690ce3059773d5b4390001d550b/src/cpp_semantic_diff/produce/extract.py#L65)) decides what is actually extracted. That is the v0 set plus **struct** and **union**.
   - So the step 1 diagram is slightly wrong: it lists six entity kinds and leaves out union.

## How confident can we be that it's sufficient?

Not very. There are two separate questions, and neither has been settled.

**1. Does the model faithfully capture what's in the code?**
- The project has checks for part of this:
  - a hard check that the parser is set up correctly, so it can't silently drop declarations ([`toolchain.py`](../../src/cpp_semantic_diff/produce/toolchain.py));
  - per-file parse-failure reporting;
  - unit tests for specific C++ constructs in [`tests/test_extract.py`](../../tests/test_extract.py).
- Nothing checks the extracted model against an independent source to show it's complete.

**2. Are these entity and relationship types enough to answer the reviewer's questions?**
- No document traces each of the twelve questions to the model fields that would answer it.
- My own reading of v0 against the twelve:

| Reviewer question | Can v0 answer it? |
|---|---|
| Did the contract change? | Partly. It sees signatures, return types, virtual/const/access, and field types. It doesn't see **enum values, global variables or type aliases**, because none of these are modelled. It also doesn't see struct size or layout, which needs a compiled build. |
| Is most of this noise? | Yes. Body fingerprints ignore formatting and comments. |
| Is it correct? | Only *where* behavior could differ, through changes to calls. |
| Did the structure degrade? | No. Includes and build-target dependencies aren't modelled. |
| Was the change complete? | Partly. It can show which callers of a changed function changed. It can't check whether a `switch` covers a new enum value, because there are no enums. |
| What is the blast radius? | Yes, as a lower bound from calls and containment. |
| Did the resource profile change? | No. That needs a compiled build. |
| Did ownership or lifetime change? | Mostly no. The has-a and holds-a relationships aren't extracted. Only field types are visible. |
| What evidence backs it? | No. The evidence stage isn't built. |
| Does it hold across build variants? | No. A model covers one build configuration. |
| Does it match the stated intent? | Partly, through clusters of connected changes. |
| Is it reversible? | No. |

The most concrete gap is that **enums, global variables and type aliases are not in the model at all**. For example, a reviewer would never see a changed enum value or a changed global constant. These are common, reviewable changes, and the full design already includes all three kinds.

---

- What entities (and relationships?) does the clang parser 'define'? What about the C++ standard? Or the Virtual Machine (is that what it's called? the C++ machine model)?

