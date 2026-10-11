<!-- metadata
id: ADR-044
type: adr
kind: frozen
status: Pending Brent Runyan's review — decided by agent review during v0 orchestration, 2026-10-11. Becomes Accepted when the v0 branch merges to main.
decided: 2026-10-11
author: agent review during v0 orchestration
-->

# ADR-044 · An inline namespace bump pairs what it renamed

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Pending Brent Runyan's review — decided by agent review during v0 orchestration, 2026-10-11. Becomes Accepted when the v0 branch merges to main.

## Context

A library that versions its ABI with an inline namespace, as fmt does with `fmt::v11` becoming
`fmt::v12`, changes the USR of everything inside it. It also changes the USR of every function
elsewhere whose signature or template arguments name one of its types. Callers see no change:
`fmt::format` still resolves, because the namespace is inline. Tier 1 saw every one of those
entities removed and an unrelated one added. Issue
[#7](https://github.com/j-brent/cpp-semantic-diff/issues/7) names this the larger case on fmt.
[ADR-036](ADR-036-functions-pair-by-name.md) left it open: its name tier pairs nothing under an
unpaired parent.

## Decision

- **Identity stays the USR.** A new tier, `inline-namespace` (`MatchTier.INLINE_NAMESPACE`,
  `match/inline.py`), runs after tier 1 and before the name tier.
- **Namespaces carry `inline`.** The producer records it on every namespace, true or false,
  from `clang_Cursor_isInlineNamespace`. A namespace that becomes inline, or stops being, is a
  `NodeAttrChanged(inline)`.
- **Candidates.** Under each paired parent, or the global scope, take the inline namespaces tier
  1 left on each side. Exactly one on each side maps the base namespace to the head one. More
  than one on either side, with any on the other, is a refused choice, reported as an ambiguous
  pairing ([ADR-034](ADR-034-uncertainty-is-doubt-about-the-pairing.md)). A plain namespace is
  never a candidate: renaming one breaks every caller that names it. Outer namespaces are
  mapped first, so a mapped namespace is a paired parent for a nested bump.
- **Anchored rewrite.** Each base USR has the old namespace's path replaced by the new one,
  only where a USR starts: after `c:`, after a file-scoped USR's file name, after `$` (a type
  in a parameter list) or after `>` (the template a specialization argument names). The path
  must end there: next comes `@`, `#`, `>` or the end. So `other::lib::v1` and `lib::v10` are
  left alone. The rewritten USR is keyed exactly as tier 1 keys it. Only a key unique on both
  sides pairs; a key two nodes share, with a partner on the other side, is a refused choice.
- **Evidence.** A mapping stands only if a descendant of the old namespace that is not itself a
  namespace pairs through the rewrite. An inline namespace removed beside an unrelated one
  added (fmt has `inline namespace literals`) is one for one and must not pair.
- **No type-spelling normalization.** libclang 18 already drops inline namespaces from
  canonical type spellings, so `void f(S)` keeps its signature across the bump. The exception
  is where dropping it would be ambiguous (both `lib::v1::X` and `lib::X` declared): there the
  spelling keeps the tag and a bump shows as `NodeSignatureChanged`. Accepted.
- **The name tier keys on the name, not the qualified name.** It already requires the paired
  parent, which says where the function is. The qualified name restated it in base
  coordinates, which is wrong across a bump. This amends ADR-036's key.
- **Uncertainty 0.10.** Close to a USR match, since the USR is the old one with one tag
  swapped. Not zero, since the tag was part of identity and the tool chose to look past it.
- The namespace pair reads as `NodeRenamed(v11 -> v12)`. Nothing inside it is a `NodeMoved`,
  because the parents are paired.

## Measurement

fmt upstream, v11 to v12 bump `ddd20d57` (2025-09-17), on 2026-10-11. "Before" is the tiers
before this decision. "As agreed" anchored only after `c:`, a file name and `$`; `>` was added
after this run showed what it missed.

| Range | | Before | As agreed | With `>` (decided) |
|---|---|---|---|---|
| `ddd20d57^..ddd20d57` | change events | 14,790 | 691 | 1 (`NodeRenamed`) |
| | unpaired base / head | 2,272 / 2,272 | 96 / 96 (61 ambiguous) | 0 / 0 |
| | high-risk count | 4 | 228 | 0 |
| `ddd20d57~12..ddd20d57` | change events | 14,910 | 888 | 198 |
| | unpaired base / head | 2,297 / 2,273 | 131 / 107 | 35 / 11 |
| | high-risk count | 10 | 267 | 39 |

"Before" has few high-risk events because every entity sat under one removed namespace and was
capped by it as a cascade (§6.3). Without `>`, the template functions and methods that missed
the rewrite stood as separate causes. The name tier paired 132 of them at 0.25 and refused the
overload sets.

**Ranking effect of 0.10**, measured by ranking the same events again with the tier at 0.00. On
the 12-commit range, the 6 body changes resting on this tier each move up 10 places of 198. The
top 25 and the high-risk count are unchanged: high risk is judged without uncertainty. So 0.10
is safe.

## Consequences

- A bumped inline namespace reads as one rename, not a removal and an addition of the library.
  `inline-namespace-bumped` in the change catalog passes. It was the last `step 4` marker.
- **Gap, accepted:** if every member of a bumped namespace also changed its signature, nothing
  pairs through the rewrite, the evidence guard withdraws the mapping, and the bump stays
  unpaired. A real ABI bump keeps most members unchanged.
- `NodeRenamed` is reachable in v0, for inline namespaces only. No other rename is paired, and
  the measurement that tiers 2–4 wait on is unaffected.
- The JSON gains `"inline-namespace"` as a match tier, under schema 2.0.
- Changing `produce/extract.py` changes the producer digest, so every cached partial misses once
  ([ADR-014](ADR-014-cache-key-covers-extraction-code.md)).
- The rewrite reads the USR's format, which `model.ir.usr_file` alone did before. It is kept to
  the one module, anchored, and tested on the USR shapes fmt produces.

## Alternatives rejected

- **Drop inline namespaces from identity** (#7's direction). It changes every node id, edge,
  change event, cache entry and the JSON contract.
- **Unanchored substring replacement.** Pairs `other::lib::v1` with `other::lib::v2`.
- **Anchor only after `c:`, a file name and `$`.** Measured above: it misses every template
  argument naming the namespace.
- **Pair on one-for-one alone, without evidence.** Pairs `v12` with an unrelated `literals`.
- **Pair plain namespaces the same way.** A plain namespace rename is a change callers feel.
- **Normalize type spellings.** libclang already does it, and normalizing again could hide a
  real change.
