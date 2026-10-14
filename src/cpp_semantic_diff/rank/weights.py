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
put every doubtful leaf above every widely-called change, and in v0 (no rename detection,
§13) every rename is a doubtful leaf twice over. It belongs where it is: enough to break a
tie toward looking, not enough to reorder the page.
"""

from __future__ import annotations

from cpp_semantic_diff.model import ChangeEventKind, MatchTier, Unpaired
from cpp_semantic_diff.rank.exposure import Exposure

# --------------------------------------------------------------------------------------
# Factor weights. These four are the top-level tuning surface and sum to 1.0.
# --------------------------------------------------------------------------------------

#: Blast radius leads because RESEARCH-002 §3.6 puts it there: nothing in the landscape
#: says "this 3-line change has 400 transitive dependents; that 300-line change has 2", and
#: it is the one factor a text diff structurally cannot approximate.
W_BLAST_RADIUS = 0.35

#: Exposure is second because on a real repository it is doing filtering work rather than
#: ordering work -- most of fmt's model is test code (see `exposure.py`), and this is the term
#: that keeps it off the first page. Under-weight it and the ranking is correct
#: about everything except which repository it is ranking.
W_EXPOSURE = 0.25

#: Novelty at parity with exposure. It is the most locally *legible* signal -- a reviewer
#: agrees immediately that a removal outranks a body edit -- which makes it the term most
#: likely to be over-trusted. A removal in a test helper is still not the thing to read
#: first.
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
#: The reason is that risk is a claim about consequences, and our doubt about a match is not
#: one of them. It was found the hard way: until ADR-034 every unmatched entity scored the
#: maximum uncertainty, so on tier-1-only identity (§13) every add and remove carried the same
#: constant offset -- at 0.15 exactly the offset that lifted a public addition with zero
#: dependents from 0.4625 to 0.6125 and over the line. Measured on fmt at `HEAD~100` on
#: 2026-08-02: 133 of 335 events (40 %) "high risk" (commit `ef2daff`). A number that says
#: "40 % of this diff is dangerous" when it means "40 % of this diff is new" is worse than no
#: number, and §10 prints it on every run. ADR-034 removed the offset for plain additions;
#: the argument above still holds for the ones that remain doubtful.
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

#: Attributes that record how something is written, not what it is: a function's written
#: signature where its canonical type is unchanged (ADR-038), whether an enumerator's value is
#: written (ADR-038), a static assertion's message (ADR-039). A change to one alone is listed
#: and ordered like any other, and never counted as high risk (ADR-046): it can clear
#: `HIGH_RISK` on blast radius and exposure alone, and nothing outside the diff can break.
INFORMATIONAL_ATTRS: frozenset[str] = frozenset({"sugared_signature", "explicit_value", "message"})

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

#: Test code is not zero: a deleted assertion is still a change worth ranking. The gap between
#: TEST and INTERNAL/PUBLIC is the load-bearing part.
EXPOSURE_WEIGHT: dict[Exposure, float] = {
    Exposure.TEST: 0.20,
    Exposure.INTERNAL: 0.60,
    Exposure.PUBLIC: 1.00,
}

# --------------------------------------------------------------------------------------
# Novelty
# --------------------------------------------------------------------------------------

#: ARCH-001 §7: "added and removed outrank modified". Every `ChangeEventKind` appears, following
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
#: The derived kinds (§6.1) are mostly weighted high, and only `CallerOfChangedSignature` can
#: fire in v0 (§13, ADR-040). The others are computed conclusions rather than observations: an
#: event that says "this became unreachable" has already done the reasoning a reviewer would
#: otherwise have to do.
NOVELTY_BY_KIND: dict[ChangeEventKind, float] = {
    ChangeEventKind.NODE_REMOVED: 1.00,
    ChangeEventKind.NODE_ADDED: 0.85,
    ChangeEventKind.NODE_MOVED: 0.60,
    ChangeEventKind.NODE_RENAMED: 0.55,
    # Also the fallback for an attribute `NOVELTY_BY_ATTR` does not name -- see below.
    ChangeEventKind.NODE_ATTR_CHANGED: 0.65,
    ChangeEventKind.NODE_BODY_CHANGED: 0.30,
    # Level with a removal. Every caller compiled against the old signature, so a changed one
    # can break code outside the diff exactly as a removal can -- it is §7's "signature"
    # outranking body changes, at the top of the scale like `return_type` below.
    ChangeEventKind.NODE_SIGNATURE_CHANGED: 1.00,
    ChangeEventKind.EDGE_REMOVED: 0.40,
    ChangeEventKind.EDGE_ADDED: 0.35,
    ChangeEventKind.EDGE_RETARGETED: 0.50,
    ChangeEventKind.EDGE_KIND_CHANGED: 0.50,
    # Above add/remove of an edge and level with `EdgeKindChanged`, for the reason
    # `diff/edges.py` gives: between two entities whose identity is already settled, a
    # relation that changed *state* is a specific semantic claim, where an edge that merely
    # appeared is usually the shadow of a body change already in the list. Deliberately not
    # refined by `NOVELTY_BY_ATTR`: that table is calibrated for entity attributes, and the
    # edge attribute vocabulary is not settled yet -- reusing it would put `access` on an
    # `inherits` edge above a removed public function on the strength of a shared key name.
    ChangeEventKind.EDGE_ATTR_CHANGED: 0.50,
    ChangeEventKind.BECAME_UNREACHABLE: 0.80,
    ChangeEventKind.BECAME_REACHABLE: 0.55,
    ChangeEventKind.CYCLE_INTRODUCED: 0.90,
    ChangeEventKind.FAN_IN_CHANGED: 0.40,
    ChangeEventKind.LAYER_VIOLATION_INTRODUCED: 0.90,
    # Below the signature change that causes it, and above a body change. It is an observation,
    # not a verdict: the call site was written against the old signature, and it may still
    # compile and behave the same. Each caller is a place the reviewer has to look, more so
    # than one edited body, but the cause is the change; `_bound_by_cause` caps it there
    # anyway (ADR-040).
    ChangeEventKind.CALLER_OF_CHANGED_SIGNATURE: 0.70,
}

#: ARCH-001 §7: "attribute changes to `virtual`, `const`, access, and signature outrank body
#: changes". Those are the doc's paraphrase; these are the keys `produce/extract.py`
#: `_attributes()` actually writes, and the two do not line up in one place that matters.
#:
#: **There is no `signature` attribute, and there cannot be one.** `signature` is a field on
#: `Node` (§4.1), not an entry in `attrs`, so the mechanical attribute diff in
#: `diff/nodes.py` can never emit `NodeAttrChanged(signature)`. A changed signature is
#: `NodeSignatureChanged` (ADR-035), whether tier 1 paired the two versions or the name tier
#: did because the USR changed (ADR-036) -- scored by `NOVELTY_BY_KIND`, not here.
#:
#: `return_type` is the part of the doc's intent that survives as a real attribute. The
#: signature spells it too, so a changed return type arrives as `NodeSignatureChanged` and
#: the diff stage does not repeat it here (ADR-035); the weight covers the attribute where
#: it does appear, and is kept level with `NodeSignatureChanged`. `type` is the same fact
#: for a field. Both are weighted at the top accordingly.
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
    # Breaks every call site that names the template arguments, as a parameter change
    # breaks every call site; the signature cannot show it (`extract._template_parameters`).
    "template_parameters": 0.90,
    # A namespace that became inline, or stopped being. It changes which declaration an
    # unqualified-through-the-parent name finds -- `lib::f` resolving into `lib::v1` or into
    # `lib::v2` -- so code outside the diff can bind differently without a line changing.
    # Below `access`: the names still exist, and the usual cause is a deliberate ABI version
    # step rather than an accident.
    "inline": 0.80,
    # Changes what every call that omits the argument passes, silently: those callers still
    # compile. Below a signature change, which can break them.
    "default_arguments": 0.85,
    # A function or variable that stopped being constexpr breaks every use in a constant
    # expression.
    "constexpr": 0.80,
    # Changes the state every constructor that does not name the field starts it in.
    "default_member_init": 0.60,
    # Gaining one makes every caller that discards the result warn; losing one only loses
    # the warning. Nothing stops compiling unless warnings are errors.
    "nodiscard": 0.55,
    # A warning at every use, and notice that the entity is going away.
    "deprecated": 0.55,
    # Breaks every class that derives from it, and devirtualizes calls on it.
    "final": 0.70,
    # Base order is layout and construction order: every base's offset, and which base is
    # constructed first.
    "base_order": 0.90,
    # The entities that carry values (ADR-038).
    "underlying_type": 0.95,  # an enum's size and signedness: layout and ABI of every use
    "scoped": 0.90,  # every unqualified use of an enumerator, and every implicit conversion
    # The value is what every use of an enumerator compiles to, and a changed one can break
    # code outside the diff silently: a serialized or persisted value now means something else.
    "value": 0.95,
    # Low on its own: a value that becomes written without changing is a style edit, and one
    # that changes reports the change through `value` as well.
    "explicit_value": 0.30,
    # What every use of an alias compiles to, so the signature of every function written
    # with it: level with `type`.
    "target": 0.95,
    # The written spelling of a function type whose canonical form did not change -- an
    # alias swapped for its target, or one alias for another. Where the canonical signature
    # changed too, `NodeSignatureChanged` reports it and this is not repeated. No caller is
    # affected, so it sits with a body change.
    "sugared_signature": 0.30,
    # A variable's `type`, `value` and `constexpr` are the keys above: a field's type, an
    # enumerator's value and a function's constexpr are the same facts.
    "linkage": 0.85,  # whether other translation units can name it at all
    "thread_local": 0.80,  # one object per thread, or one shared by all
    "storage": 0.75,  # `static`, `extern`: mostly restates linkage, and is reported beside it
    # An initializer clang could not evaluate changed: a call, a constructor, an address. Real,
    # but it says only that the text changed, as a body change does, with a little more weight
    # because it runs before `main`.
    "initializer_hash": 0.50,
    # A macro's replacement changed: every use of it now compiles to something else, and no
    # declaration in the model shows it (ADR-039).
    "replacement_hash": 0.80,
    "function_like": 0.70,  # every use must now be written with, or without, an argument list
    # A static assertion's message: what a failing build prints, and nothing else (ADR-039).
    "message": 0.30,
}

# --------------------------------------------------------------------------------------
# Confidence -- inverted. ARCH-001 §7: "a low-confidence event ranks *up*, not down."
# --------------------------------------------------------------------------------------

#: Keyed by match tier, valued as **uncertainty**, not confidence. §5's tier table, read
#: downward: tier 1 is a USR match and carries no doubt worth ranking on; each further tier
#: is a weaker claim that these two entities are the same entity, and an event resting on a
#: weaker claim is more worth a human's eye, not less.
#:
#: Uncertainty means *the tool may have this pairing wrong* -- not *this entity has no
#: partner*. An unmatched entity has no tier and is valued by `UNCERTAINTY_BY_UNPAIRED`.
UNCERTAINTY_BY_TIER: dict[MatchTier, float] = {
    MatchTier.SIMILARITY: 0.75,
    MatchTier.POSITIONAL: 0.50,
    MatchTier.STRUCTURAL: 0.30,
    # A function paired by its name because its USR changed (ADR-036). The pairing is
    # unique by construction -- the tier refuses any name that is not one-to-one -- so the
    # doubt is low. It is not zero: the claim rests on the name alone, and a function
    # replaced by an unrelated one of the same name pairs as readily as one that changed.
    # Below `STRUCTURAL`, whose matched body says more than a matched name.
    MatchTier.NAME: 0.25,
    # An entity paired across an inline namespace bump (`lib::v1::f` with `lib::v2::f`,
    # ADR-044): its USR is the old one with the version tag swapped, and the swap stands only
    # where one inline namespace was replaced by one other under the same parent and something
    # inside them paired through it. Nearly a USR match, so close to zero; not zero, because
    # the version tag was part of the identity and the tool chose to look past it.
    MatchTier.INLINE_NAMESPACE: 0.10,
    MatchTier.DECLARED: 0.00,
}

#: Keyed by why an entity is unmatched (ADR-034), on the same scale as the tiers. Every
#: genuine addition and removal has no partner by definition, so "unmatched" alone is not
#: doubt; the evidence that a partner was missed is.
#:
#: - `NONE` -- nothing on the other side could be its partner. A plain addition or removal
#:   is as certain as a tier-1 match: nothing about it is in question.
#: - `CANDIDATE` -- the other side's leftovers hold an entity of the same kind under the same
#:   parent (§5's tier-4 candidate set, counted, not scored). Possibly a rename tier 1
#:   cannot see. Midway: evidence that a partner may exist, which is less than a tier that
#:   chose wrongly and more than nothing.
#: - `AMBIGUOUS` -- a tier found partners and refused to choose between them. The pairing
#:   is the thing in question, so it is the maximum.
UNCERTAINTY_BY_UNPAIRED: dict[Unpaired, float] = {
    Unpaired.NONE: 0.00,
    Unpaired.CANDIDATE: 0.50,
    Unpaired.AMBIGUOUS: 1.00,
}

#: Added when either model is incomplete (§4.4, §11). A global fact today -- `Confidence`
#: carries one flag for both models -- so it shifts every event equally and changes no
#: ordering; it is applied rather than skipped so that the score says out loud what the
#: output is resting on, and so that it starts discriminating for free if per-event
#: completeness ever exists.
UNCERTAINTY_INCOMPLETE_MODELS = 0.25
