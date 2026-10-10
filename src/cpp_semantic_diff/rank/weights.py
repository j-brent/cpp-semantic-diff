"""Every number the rank stage tunes, in one file, each with the reason it is that number.

ARCH-001 §7 names four inputs and does not weight them, because the weighting is what has
to be calibrated against real repositories rather than argued from first principles. ARCH-001
§14 says the same thing about the tier-4 similarity threshold and draws the right conclusion
-- *design the knob in early* -- so the knobs are here, named, together, and the scoring
code reads them rather than spelling numbers inline. A ranking whose weights are scattered
through three modules cannot be tuned, and one that cannot be tuned will be tuned by
rewriting it.

Two properties are deliberate and worth preserving through any retune:

**The factor weights sum to 1 and every factor value is normalized to [0, 1].** So a total
is in [0, 1], comparable between runs and between repositories, and a threshold like
`HIGH_RISK` means the same thing everywhere. `summary.high_risk_count` (§10) prints on every
run; it cannot be a count against a scale that moves. `IMPACT_FACTORS` is a subset of the
weights rather than a second scale for the same reason -- it is rescaled by its own weight
sum so that one threshold serves both.

**Uncertainty is weighted lowest, and that is not a hedge.** ARCH-001 §7 is explicit that a
low-confidence event ranks *up* -- uncertainty is a reason for a human to look. It is not a
claim that uncertainty is the largest risk. Weighted at parity with blast radius it would
put every unmatched leaf above every widely-called change, because in v0 (tier 1 only,
§13) *every* add and remove is untiered. It belongs where it is: enough to break a tie
toward looking, not enough to reorder the page.
"""

from __future__ import annotations

from cpp_semantic_diff.model import EventKind, MatchTier
from cpp_semantic_diff.rank.exposure import Exposure

# --------------------------------------------------------------------------------------
# Factor weights. These four are the top-level tuning surface and sum to 1.0.
# --------------------------------------------------------------------------------------

#: Blast radius leads because RESEARCH-002 §3.6 puts it there: nothing in the landscape
#: says "this 3-line change has 400 transitive dependents; that 300-line change has 2", and
#: it is the one factor a text diff structurally cannot approximate.
W_BLAST_RADIUS = 0.35

#: Exposure is second because on a real repository it is doing filtering work rather than
#: ordering work -- most of fmt's model is test and vendored code (see `exposure.py`), and this
#: is the term that keeps it off the first page. Under-weight it and the ranking is correct
#: about everything except which repository it is ranking.
W_EXPOSURE = 0.25

#: Novelty at parity with exposure. It is the most locally *legible* signal -- a reviewer
#: agrees immediately that a removal outranks a body edit -- which makes it the term most
#: likely to be over-trusted. A removal in vendored googletest is still not the thing to
#: read first.
W_NOVELTY = 0.25

#: See the module docstring. Lowest on purpose, non-zero on purpose.
W_UNCERTAINTY = 0.15

FACTOR_WEIGHTS: dict[str, float] = {
    "blast_radius": W_BLAST_RADIUS,
    "exposure": W_EXPOSURE,
    "novelty": W_NOVELTY,
    "uncertainty": W_UNCERTAINTY,
}

#: The three factors that measure **the change**, as opposed to our knowledge of it. Every
#: factor orders the page; only these three decide whether an event is counted as high risk
#: (`RiskScore.is_high_risk`, which carries the argument).
#:
#: The reason is that a fixed threshold on a sum only means something if every term in the sum
#: can discriminate, and in v0 one of them cannot. Tier-1-only identity (§13) makes *every* add
#: and remove unmatched residue, so `UNCERTAINTY_BY_TIER[None]` applies to that whole
#: population at once: not a measurement, a constant offset -- and at 0.15 it is exactly the
#: offset that lifts a public addition with zero dependents from 0.4625 to 0.6125 and over the
#: line. Measured on fmt at `HEAD~100` on 2026-08-02: 133 of 335 events (40 %) "high risk"
#: (commit `ef2daff`). A number that says "40 % of this diff is dangerous" when it means "40 %
#: of this diff is new" is worse than no number, and §10 prints it on every run.
IMPACT_FACTORS: frozenset[str] = frozenset({"blast_radius", "exposure", "novelty"})

#: What `IMPACT_FACTORS` sums to, so the subtotal can be rescaled into [0, 1] and compared
#: against the same `HIGH_RISK` as a total. Derived rather than written down: a retune that
#: moves `W_UNCERTAINTY` must not silently move what "high risk" means.
IMPACT_WEIGHT: float = sum(FACTOR_WEIGHTS[name] for name in IMPACT_FACTORS)

#: Where `summary.high_risk_count` (ARCH-001 §10) draws its line -- on `RiskScore.impact`,
#: not on `total`. Unchanged from the value the totals were calibrated against, because the
#: defect was never this number: `impact` restores the property the number needs, which is
#: that every term feeding it varies with the event.
HIGH_RISK = 0.6

# --------------------------------------------------------------------------------------
# Blast radius
# --------------------------------------------------------------------------------------

#: ARCH-001 §7: bounded depth. Three hops is far enough to cross the two indirections C++
#: actually adds -- a caller's caller, and a base class's derived class's callers -- and
#: short enough that the reverse cone of a common utility does not become the repository.
#: Past three the counts stop discriminating: everything popular reaches everything.
BLAST_MAX_DEPTH = 3

#: Where the count stops adding risk. Logarithmic, because the step from 0 to 5 dependents
#: is a different kind of fact than the step from 200 to 400 -- the first says "this is not
#: a leaf", the second says nothing a reviewer can act on differently.
BLAST_SATURATION = 100.0

#: Hard stop on one walk. §9's budget is per review, and a real model has entities whose
#: reverse cone is most of the graph; paying for it per event buys nothing, since anything past
#: `BLAST_SATURATION` scores the same. Reported through `BlastResult.saturated` rather than
#: silently truncated (§11).
BLAST_VISIT_CAP = 2000

# --------------------------------------------------------------------------------------
# Exposure
# --------------------------------------------------------------------------------------

#: Vendored code is not zero. A change *inside* bundled googletest is still a change, and
#: zeroing the term would make it unrankable relative to other vendored changes rather than
#: merely last. The gap between VENDORED/TEST and INTERNAL/PUBLIC is the load-bearing part.
EXPOSURE_WEIGHT: dict[Exposure, float] = {
    Exposure.VENDORED: 0.05,
    Exposure.TEST: 0.20,
    Exposure.INTERNAL: 0.60,
    Exposure.PUBLIC: 1.00,
}

# --------------------------------------------------------------------------------------
# Novelty
# --------------------------------------------------------------------------------------

#: ARCH-001 §7: "added and removed outrank modified". Every `EventKind` appears, following
#: the convention `REQUIRED_PAYLOAD` sets in `events.py` -- adding a kind without deciding
#: its weight should be a deliberate act, not an omission that silently scores zero.
#:
#: Removal above addition: an added entity is a new promise, a removed one is a broken
#: promise, and only the second can break code that is not in the diff.
#:
#: Edge events sit low because in practice most of them are consequences of a body change
#: that is already in the list -- classify (§6.3) will link many of them, and until it does,
#: weighting them like node events would let one edited function fill the page with its own
#: call churn.
#:
#: The derived kinds (§6.1) are mostly weighted high and none of them can fire in v0 (§13). They
#: are computed conclusions rather than observations: an event that says "this became
#: unreachable" has already done the reasoning a reviewer would otherwise have to do.
NOVELTY_BY_KIND: dict[EventKind, float] = {
    EventKind.NODE_REMOVED: 1.00,
    EventKind.NODE_ADDED: 0.85,
    EventKind.NODE_MOVED: 0.60,
    EventKind.NODE_RENAMED: 0.55,
    # Also the fallback for an attribute `NOVELTY_BY_ATTR` does not name -- see below.
    EventKind.NODE_ATTR_CHANGED: 0.65,
    EventKind.NODE_BODY_CHANGED: 0.30,
    EventKind.EDGE_REMOVED: 0.40,
    EventKind.EDGE_ADDED: 0.35,
    EventKind.EDGE_RETARGETED: 0.50,
    EventKind.EDGE_KIND_CHANGED: 0.50,
    # Above add/remove of an edge and level with `EdgeKindChanged`, for the reason
    # `diff/edges.py` gives: between two entities whose identity is already settled, a
    # relation that changed *state* is a specific semantic claim, where an edge that merely
    # appeared is usually the shadow of a body change already in the list. Deliberately not
    # refined by `NOVELTY_BY_ATTR`: that table is calibrated for entity attributes, and the
    # edge attribute vocabulary is not settled yet -- reusing it would put `access` on an
    # `inherits` edge above a removed public function on the strength of a shared key name.
    EventKind.EDGE_ATTR_CHANGED: 0.50,
    EventKind.BECAME_UNREACHABLE: 0.80,
    EventKind.BECAME_REACHABLE: 0.55,
    EventKind.CYCLE_INTRODUCED: 0.90,
    EventKind.FAN_IN_CHANGED: 0.40,
    EventKind.LAYER_VIOLATION_INTRODUCED: 0.90,
    EventKind.SIGNATURE_INCOMPATIBLE: 1.00,
}

#: ARCH-001 §7: "attribute changes to `virtual`, `const`, access, and signature outrank body
#: changes". Those are the doc's paraphrase; these are the keys `produce/extract.py`
#: `_attributes()` actually writes, and the two do not line up in one place that matters.
#:
#: **There is no `signature` attribute, and there cannot be one.** `signature` is a field on
#: `Node` (§4.1), not an entry in `attrs`, so the mechanical attribute diff in
#: `diff/nodes.py` can never emit `NodeAttrChanged(signature)`. Worse, in v0 a changed
#: signature changes the USR, which is tier-1 identity (§5), so the pair is never matched
#: and the change surfaces as remove + add -- scored by `NOVELTY_BY_KIND`, not here.
#:
#: `return_type` is the part of the doc's intent that survives as a real attribute, and it
#: is the interesting part: C++ cannot overload on return type, so the USR does *not*
#: encode it, so a changed return type is a signature change that tier 1 matches straight
#: through and this table is the only place that can weight it. `type` is the same fact for
#: a field. Both are weighted at the top accordingly.
#:
#: The remaining keys are listed whether or not ARCH names them, so that the table is a
#: statement about the producer's output rather than about the doc's prose. An attribute
#: absent here falls back to `NOVELTY_BY_KIND[NODE_ATTR_CHANGED]`, which sits above a body
#: change on purpose: an attribute this table has no rule for is still a specific, named,
#: semantic change, and §7's rule about uncertainty applies to our own ignorance too.
NOVELTY_BY_ATTR: dict[str, float] = {
    "return_type": 1.00,  # §7's "signature", as far as the attribute diff can express it
    "type": 1.00,  # a field's type is the signature of the data
    "access": 0.95,  # §7, named
    "virtual": 0.95,  # §7, named
    "pure_virtual": 0.95,  # abstractness is a contract on every derived class
    "const": 0.90,  # §7, named. RESEARCH-002 §3.8 puts this in the densest defect class
    "deleted": 0.90,  # deleting a special member removes an operation clients may use
    "exception_spec": 0.85,  # part of the type since C++17, and an ABI fact
    "static": 0.85,  # changes how every call site must spell the call
    "abstract": 0.80,  # a record that became instantiable, or stopped being
    "bitfield_width": 0.80,  # layout, and therefore serialization -- RESEARCH-002 §3.12
    "defaulted": 0.70,
    "explicit": 0.70,  # a conversion that was opt-in became implicit, or the reverse
    "mutable": 0.60,
    "template": 0.60,  # an entity that became a template: real, but it changes identity too
}

# --------------------------------------------------------------------------------------
# Confidence -- inverted. ARCH-001 §7: "a low-confidence event ranks *up*, not down."
# --------------------------------------------------------------------------------------

#: Keyed by match tier, valued as **uncertainty**, not confidence. §5's tier table, read
#: downward: tier 1 is a USR match and carries no doubt worth ranking on; each further tier
#: is a weaker claim that these two entities are the same entity, and an event resting on a
#: weaker claim is more worth a human's eye, not less.
#:
#: `None` -- unmatched residue -- is the maximum. It is also, in v0, every add, every remove
#: and every rename (§13), which is precisely the population the tool is least sure about and
#: the one v0 exists to measure rather than hide.
UNCERTAINTY_BY_TIER: dict[MatchTier | None, float] = {
    None: 1.00,
    MatchTier.SIMILARITY: 0.75,
    MatchTier.POSITIONAL: 0.50,
    MatchTier.STRUCTURAL: 0.30,
    MatchTier.DECLARED: 0.00,
}

#: Added when either model is incomplete (§4.4, §11). A global fact today -- `Confidence`
#: carries one flag for both models -- so it shifts every event equally and changes no
#: ordering; it is applied rather than skipped so that the score says out loud what the
#: output is resting on, and so that it starts discriminating for free if per-event
#: completeness ever exists.
UNCERTAINTY_INCOMPLETE_MODELS = 0.25
