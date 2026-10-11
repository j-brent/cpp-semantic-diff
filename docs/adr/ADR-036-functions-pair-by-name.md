<!-- metadata
id: ADR-036
type: adr
kind: frozen
status: Pending Brent Runyan's review — decided by agent review during v0 orchestration, 2026-10-11. Becomes Accepted when the v0 branch merges to main. Amended by ADR-044 (name-tier key).
decided: 2026-10-11
author: agent review during v0 orchestration
-->

# ADR-036 · A function whose USR changed pairs by its name, where the name leaves no choice

*Frozen document: a decision record. Only the status line may change, when a later ADR supersedes it.*

**Status:** Pending Brent Runyan's review — decided by agent review during v0 orchestration, 2026-10-11. Becomes Accepted when the v0 branch merges to main. Amended by ADR-044 (name-tier key).

## Context

[ADR-026](ADR-026-changed-parameters-same-function.md) decided that a function whose parameters
changed is the same function, and left open how to pair it: overloads, scope and templates. Its
USR encodes its parameter types, and a method's encodes its `const`, so tier 1 cannot pair it.
Issue [#8](https://github.com/j-brent/cpp-semantic-diff/issues/8) is the parameter-type half of
[#7](https://github.com/j-brent/cpp-semantic-diff/issues/7), which proposes making identity the
kind and the qualified name.

## Decision

- **Identity stays the USR, and tier 1 is unchanged.** A second tier, `name`
  (`MatchTier.NAME`, `match/name.py`), runs over what tier 1 left, and only over functions and
  methods.
- **Key:** the kind, the qualified name, the file and TU for an internal-linkage function
  (through the git rename map, as tier 1 does), and the head id of the parent, None at global
  scope.
- **The parent must already be paired.** Otherwise nothing pairs. So this tier never reports a
  `NodeMoved`, and an inline namespace bump (`v1` to `v2`) pairs nothing: the namespaces are
  different entities to tier 1, and pairing their members would be a claim about the namespaces
  first. The inline-namespace half of #7 is not decided here.
- **One to one, or nothing.** A key with exactly one leftover on each side pairs. A key with
  leftovers on both sides that are not one to one pairs none of them, and each is reported as an
  ambiguous pairing (`Unpaired.AMBIGUOUS`, [ADR-034](ADR-034-uncertainty-is-doubt-about-the-pairing.md)).
  A key with leftovers on one side only is a genuine addition or removal. There is no tie-break.
- **Accepted:** with `f(int)` and `f(double)` before, and `f(int)` and `f(long)` after, tier 1
  pairs `f(int)`, and this tier pairs `f(double)` with `f(long)`, the only two left.
- **Accepted:** a template and a non-template of the same name pair. The change shows as
  `NodeAttrChanged(template_parameters)`. That attribute is a prerequisite of this tier: a
  signature spells a template parameter by position, so two templates that differ only in their
  parameter lists share a signature, and without it the change would be invisible.
- **Scope** (ADR-026's question): parameters, return type and `const` change the signature and
  are reported as `NodeSignatureChanged`
  ([ADR-035](ADR-035-signature-and-body-apply-to-functions.md)). `noexcept` stays the
  `exception_spec` attribute. Template parameters are the `template_parameters` attribute.
- **Uncertainty 0.25** for a name pairing. Not zero: the claim rests on the name alone, and a
  function replaced by an unrelated one of the same name pairs as readily as one that changed.
  Low, because the tier refuses every name that is not one to one. Below the structural tier's
  0.30, whose matched body says more than a matched name.
- **Call sites.** Edges map through the correspondence, so a paired function's calls produce no
  edge change events and are not reported twice. ADR-026 asks for them to be "filed under" the
  change event. For now that means a list: the output names the head-side callers beside each
  `NodeSignatureChanged` as evidence (ARCH-001 §6.2), and nothing checks whether each still
  compiles. [#16](https://github.com/j-brent/cpp-semantic-diff/issues/16) will replace the list
  with `CallerOfChangedSignature` change events.

## Consequences

- A function whose signature changed reads as one change event, not a removal and an addition
  with every call to it twice. `parameter-added` in the change catalog passes.
- The tier protocol gains the pairs made so far, which the parent guard needs.
- ADR-034's trade is now reachable in v0: an edge between a function paired by name and a plain
  addition carries the addition's uncertainty, 0.0, not the name tier's 0.25.
- `call-moved-to-another-overload` no longer fires where the old overload was removed and the new
  one is the only other of its name. The two pair as one function whose signature changed.
- The JSON gains `"name"` as a match tier, in `tiers_run` and `confidence.tier`, under schema 2.0.
- A rename is still a removal plus an addition: this tier keys on the qualified name. The
  measurement that tiers 2–4 wait on is unaffected.

## Alternatives rejected

- **Make identity the kind and the qualified name** (#7's full direction). It changes every node
  id, edge, change event, cache entry and the JSON contract, and overloads would need telling
  apart inside the id.
- **Break an N:M tie by signature similarity.** A guess presented as a fact
  ([ADR-003](ADR-003-identity-pairing-is-one-to-one.md)).
- **Pair under an unpaired parent.** A claim about the parents first, and it would report moves.
- **Refuse the `f(double)` to `f(long)` case.** Once tier 1 has taken the unchanged overload,
  the choice is unique, and refusing it would leave a one-to-one case unpaired for a reason the
  residue does not show.
- **Record name pairings as `declared`.** They could not be counted apart from USR matches, and
  v0 measures them.
