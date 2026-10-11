"""The delta: what a two-point analysis is allowed to say.

ARCH-001 §6. The event set is **closed**. A new signal becomes a new `ChangeEventKind`, never a
new string in a free-form field -- because every downstream stage (classify's cause rules
§6.3, rank's novelty weights §7, render's collapse §6.3) switches on the kind, and a
stringly-typed kind turns each of those into a place where an unknown value is silently
ignored. There is deliberately no `Other`, no `detail: str`, and no escape hatch here.

The shape is §6.2 exactly: id, kind, subject, payload, caused_by, provenance, confidence.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from enum import StrEnum
from typing import NewType

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cpp_semantic_diff.model.ir import (
    AnyId,
    AttrValue,
    Completeness,
    Location,
    MatchTier,
    NodeKind,
)

ChangeEventId = NewType("ChangeEventId", str)


class ChangeEventKind(StrEnum):
    """The closed set of ARCH-001 §6.1.

    Node and edge events are read off the two models plus the correspondence. Derived
    events are *computed* -- they are the founding document's point that the most
    important consequences of a change are never in the change.
    """

    # Node
    NODE_ADDED = "NodeAdded"
    NODE_REMOVED = "NodeRemoved"
    NODE_MOVED = "NodeMoved"  # parent changed
    NODE_RENAMED = "NodeRenamed"
    NODE_ATTR_CHANGED = "NodeAttrChanged"
    NODE_BODY_CHANGED = "NodeBodyChanged"
    #: A function's or method's signature differs between two paired versions of it.
    #: Its own kind rather than `NodeAttrChanged(signature)`: `signature` is a field of the
    #: node (ARCH-001 §4.1), not an attribute, and a reviewer's question about it -- did
    #: every caller follow? -- is not the question any attribute change raises (ADR-035).
    NODE_SIGNATURE_CHANGED = "NodeSignatureChanged"

    # Edge
    EDGE_ADDED = "EdgeAdded"
    EDGE_REMOVED = "EdgeRemoved"
    EDGE_RETARGETED = "EdgeRetargeted"
    EDGE_KIND_CHANGED = "EdgeKindChanged"
    #: Added after v0 shipped, under §6.1's own rule that "new signals become new
    #: event kinds, never free-text". §4.2 gives edges `attrs` and the producer fills them:
    #: `access` on `inherits`, `virtual` on `calls`. A base class going public -> private
    #: between two otherwise unchanged classes is a breaking change in C++, and without this
    #: kind the delta is silent about it -- the edge matches on both sides, so add/remove
    #: cannot express it either. The asymmetry argument §6.1 makes for `EdgeKindChanged`
    #: covers this exactly: the endpoints are identity and belong to the matcher, the attrs
    #: are diffable state. §6.1 now lists it.
    EDGE_ATTR_CHANGED = "EdgeAttrChanged"

    # Derived -- computed from both models plus graph algorithms, not read off either.
    BECAME_UNREACHABLE = "BecameUnreachable"
    BECAME_REACHABLE = "BecameReachable"
    CYCLE_INTRODUCED = "CycleIntroduced"
    FAN_IN_CHANGED = "FanInChanged"
    LAYER_VIOLATION_INTRODUCED = "LayerViolationIntroduced"
    #: A call from G to F, present on both sides, where F's signature changed (ADR-040). The
    #: subject is the head call edge, so its home is the caller. It states an observation --
    #: this call site was written against the old signature -- and claims nothing about
    #: whether it still compiles or behaves the same. It replaces ARCH-001's never-emitted
    #: `SignatureIncompatible`, a name that claimed a verdict no stage makes.
    #:
    #: A **lower bound**: only calls the model sees. Calls that depend on a template
    #: parameter, calls from initializers and default arguments (issue #13), and
    #: calls through a function pointer are not call edges into F, so their callers get no
    #: event.
    CALLER_OF_CHANGED_SIGNATURE = "CallerOfChangedSignature"


NODE_KINDS: frozenset[ChangeEventKind] = frozenset(
    {
        ChangeEventKind.NODE_ADDED,
        ChangeEventKind.NODE_REMOVED,
        ChangeEventKind.NODE_MOVED,
        ChangeEventKind.NODE_RENAMED,
        ChangeEventKind.NODE_ATTR_CHANGED,
        ChangeEventKind.NODE_BODY_CHANGED,
        ChangeEventKind.NODE_SIGNATURE_CHANGED,
    }
)

EDGE_KINDS: frozenset[ChangeEventKind] = frozenset(
    {
        ChangeEventKind.EDGE_ADDED,
        ChangeEventKind.EDGE_REMOVED,
        ChangeEventKind.EDGE_RETARGETED,
        ChangeEventKind.EDGE_KIND_CHANGED,
        ChangeEventKind.EDGE_ATTR_CHANGED,
    }
)

DERIVED_KINDS: frozenset[ChangeEventKind] = frozenset(ChangeEventKind) - NODE_KINDS - EDGE_KINDS

#: What v0 actually emits: ARCH-001 §13's "node add/remove/attr/body/signature and edge
#: add/remove/kind/attr", plus `CallerOfChangedSignature`, the one derived kind in v0, which the
#: derive stage emits (ADR-040). `EdgeKindChanged` and `EdgeAttrChanged` are reachable on tier-1
#: input for one argument applied twice (§6.1, `diff/edges.py`) -- an edge's endpoints are
#: identity and belong to the matcher, while its kind and its attrs are diffable state, so
#: between two entities tier 1 has already paired, a relation that changed kind or changed
#: `access` needs no new identity claim from this stage.
#:
#: What stays out is what v0 genuinely cannot reach: `NodeRenamed` and `NodeMoved` need
#: rename detection (both v0 tiers pair only entities that agree on name and scope: the USR
#: encodes both, and the name tier keys on the qualified name under a paired parent),
#: `EdgeRetargeted` is refused outright (§6.1), and the other derived kinds are deferred by §13.
#: The rest of the set is still declared in `ChangeEventKind` because the schema is the
#: contract the whole pipeline is written against; declaring it late would mean rewriting
#: consumers rather than extending them.
V0_KINDS: frozenset[ChangeEventKind] = frozenset(
    {
        ChangeEventKind.NODE_ADDED,
        ChangeEventKind.NODE_REMOVED,
        ChangeEventKind.NODE_ATTR_CHANGED,
        ChangeEventKind.NODE_BODY_CHANGED,
        ChangeEventKind.NODE_SIGNATURE_CHANGED,
        ChangeEventKind.EDGE_ADDED,
        ChangeEventKind.EDGE_REMOVED,
        ChangeEventKind.EDGE_KIND_CHANGED,
        ChangeEventKind.EDGE_ATTR_CHANGED,
        ChangeEventKind.CALLER_OF_CHANGED_SIGNATURE,
    }
)

#: The other half of what `V0_KINDS` says, stated positively so a test can assert it: kinds no
#: amount of real v0 input can produce. A delta containing one of these means a later tier
#: landed, or that some stage started inferring identity on its own.
UNREACHABLE_IN_V0: frozenset[ChangeEventKind] = frozenset(ChangeEventKind) - V0_KINDS

#: The node kinds a node change event can be about, for the kinds that cannot be about every
#: node (ARCH-001 §6.1's applicability table; ADR-035). Only a function or a method has a
#: signature or a code body: the producer leaves `signature` and `body_hash` empty for every
#: other kind, and "methods" includes constructors, destructors and conversion operators,
#: which the model classifies as methods. A kind absent here applies to every node kind.
#: The diff stage reads this rather than trusting the producer to keep the fields empty.
APPLIES_TO: dict[ChangeEventKind, frozenset[NodeKind]] = {
    ChangeEventKind.NODE_SIGNATURE_CHANGED: frozenset({NodeKind.FUNCTION, NodeKind.METHOD}),
    ChangeEventKind.NODE_BODY_CHANGED: frozenset({NodeKind.FUNCTION, NodeKind.METHOD}),
}

#: Payload keys a kind must carry, for the kinds ARCH-001 §6.1 names or directly implies.
#: This is a floor, not a ceiling: a kind may carry more, but a consumer switching on the
#: kind can rely on these being present. The derived kinds other than
#: `CallerOfChangedSignature` carry no requirement because their payloads are not designed yet
#: (§13 puts them out of v0) -- an empty entry here means "undesigned", and adding a kind
#: without adding an entry is a deliberate choice rather than an oversight, which is why every
#: kind appears.
REQUIRED_PAYLOAD: dict[ChangeEventKind, frozenset[str]] = {
    ChangeEventKind.NODE_ADDED: frozenset(),
    ChangeEventKind.NODE_REMOVED: frozenset(),
    ChangeEventKind.NODE_MOVED: frozenset({"old_parent", "new_parent"}),
    ChangeEventKind.NODE_RENAMED: frozenset({"old_name", "new_name"}),
    ChangeEventKind.NODE_ATTR_CHANGED: frozenset({"attr", "old", "new"}),
    ChangeEventKind.NODE_BODY_CHANGED: frozenset(),
    ChangeEventKind.NODE_SIGNATURE_CHANGED: frozenset({"old_signature", "new_signature"}),
    ChangeEventKind.EDGE_ADDED: frozenset(),
    ChangeEventKind.EDGE_REMOVED: frozenset(),
    ChangeEventKind.EDGE_RETARGETED: frozenset({"old_dst", "new_dst"}),
    ChangeEventKind.EDGE_KIND_CHANGED: frozenset({"old_kind", "new_kind"}),
    # The same three keys as `NodeAttrChanged`, deliberately: it is the same mechanical
    # attribute diff (§4.3) over `Edge.attrs` instead of `Node.attrs`, and a consumer that
    # can read one should not have to learn a second shape to read the other.
    ChangeEventKind.EDGE_ATTR_CHANGED: frozenset({"attr", "old", "new"}),
    ChangeEventKind.BECAME_UNREACHABLE: frozenset(),
    ChangeEventKind.BECAME_REACHABLE: frozenset(),
    ChangeEventKind.CYCLE_INTRODUCED: frozenset(),
    ChangeEventKind.FAN_IN_CHANGED: frozenset(),
    ChangeEventKind.LAYER_VIOLATION_INTRODUCED: frozenset(),
    # Both ends by id, though the subject edge names them, so a consumer never parses an
    # edge id. Both signatures, so the call can be read against the change without looking
    # the cause up. `caller_edited`: the caller has its own `NodeBodyChanged` or
    # `NodeSignatureChanged` in this diff. An unedited caller is the one a text diff never
    # shows the reviewer (ADR-040).
    ChangeEventKind.CALLER_OF_CHANGED_SIGNATURE: frozenset(
        {"caller", "callee", "old_signature", "new_signature", "caller_edited"}
    ),
}


class Side(StrEnum):
    """Which of the two points a hunk belongs to. A two-point analysis has exactly two."""

    BASE = "base"
    HEAD = "head"


class Unpaired(StrEnum):
    """Why an entity has no partner on the other side -- the match stage's doubt about it.

    An entity no tier paired is not, by that fact alone, a doubtful pairing: a genuine
    addition has no partner by definition, and nothing about it is uncertain. What makes an
    unpaired entity doubtful is evidence that a partner may have been missed, so that is what
    this records (ADR-034). Ordered weakest doubt first; `diff.context.UNPAIRED_ORDER` relies
    on the order and `rank.weights.UNCERTAINTY_BY_UNPAIRED` values it.
    """

    #: Nothing on the other side's residue could be this entity's partner: no unpaired entity
    #: of the same kind under the same parent. A plain addition or removal.
    NONE = "none"
    #: The other side's residue holds at least one entity of the same kind under the same
    #: parent -- ARCH-001 §5's tier-4 candidate set, counted rather than scored. A rename
    #: that tier 1 cannot see lands here.
    CANDIDATE = "candidate"
    #: A tier found partners but could not choose between them, so it paired none (e.g.
    #: `match.declared._index` on a colliding key). The pairing itself is in doubt.
    AMBIGUOUS = "ambiguous"


class Hunk(BaseModel):
    """A source span an event came from: (side, file, line span). ARCH-001 §6.2.

    Composed from `Location` rather than restating file and line, so the separator
    normalization that makes provenance comparable across hosts stays in one place.

    Provenance must be **bidirectional**: the reviewer navigates from an event to the lines
    *and* from a line in the ordinary diff to the events it caused. The reverse index is
    derived from these hunks (see `events_at`) rather than stored, because two copies of
    the same relation drift.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    side: Side
    location: Location

    def contains(self, line: int) -> bool:
        end = self.location.end_line or self.location.line
        return self.location.first_line <= line <= end


class Confidence(BaseModel):
    """The two inputs ARCH-001 §11 names, carried verbatim -- not a score.

    The match half is `tier` when the subject was matched and `unpaired` when it was not:
    an unmatched subject still has a match-stage verdict, namely how likely it is that a
    partner exists and was missed (ADR-034).

    §11: "Every event carries confidence derived from its match tier and the completeness of
    both models." The derivation is deliberately *not* performed here. Rank (§7) owns the
    weighting, and it has the inverting rule that makes a premature scalar dangerous: a
    low-confidence event ranks **up**, not down. Collapsing tier and completeness into one
    number here would force that rule to be re-derived from a lossy input.

    `tier` and `models_complete` are required. A default would let a caller omit
    completeness and thereby claim, silently, that nothing was missing -- the exact
    substitution of absence of evidence for evidence of absence that §11 exists to prevent.
    `unpaired` defaults to None only because the validator below refuses that default
    wherever it would be a claim.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tier: MatchTier | None = Field(
        description="The tier that matched the subject, or None when it was unmatched "
        "residue -- for an edge event, when either endpoint was. v0 has no rename detection "
        "(ARCH-001 §13), so None also covers every rename -- which is what makes the rename "
        "damage measurable rather than invisible.",
    )
    unpaired: Unpaired | None = Field(
        default=None,
        description="Why the subject is unmatched, set exactly when `tier` is None: "
        "unmatched means *no partner*, not *doubtful pairing*, and this says how much "
        "doubt there is (ADR-034). For an edge event, the most doubtful of its unmatched "
        "endpoints.",
    )
    models_complete: bool = Field(
        description="Both models parsed every TU they attempted -- **source coverage, and "
        "only that**. What was missing lives in the per-side `Completeness` records "
        "(ARCH-001 §4.4), including the count of edges that lead outside the repository, "
        "which this flag deliberately does not reflect; this is only the flag that tells a "
        "consumer it has to say so.",
    )

    @model_validator(mode="after")
    def _unpaired_iff_unmatched(self) -> Confidence:
        # Defaulted, but never silently: an unmatched subject without a reason would leave
        # rank to guess how doubtful it is, and a matched one with a reason claims both.
        if self.tier is None and self.unpaired is None:
            raise ValueError("an unmatched subject (tier None) must say why: unpaired is required")
        if self.tier is not None and self.unpaired is not None:
            raise ValueError(f"a subject matched at {self.tier.value!r} has no unpaired reason")
        return self

    @classmethod
    def derive(
        cls,
        tier: MatchTier | None,
        base: Completeness,
        head: Completeness,
        unpaired: Unpaired | None = None,
    ) -> Confidence:
        """Coverage on both sides, because an event rests on both models (ARCH-001 §11).

        `Completeness` measures two unrelated kinds of incompleteness and only one of them
        belongs here. A TU that failed to parse can make an entity that exists read as
        absent, so it taints every claim on that side -- including claims about entities in
        the TUs that *did* parse, since what is missing may be what would have contradicted
        them. Dropped edges are the other kind: with full coverage, an endpoint that
        resolves to no declared node is outside the repository by construction (`std::`,
        libc, implicit special members), which is the model's domain boundary rather than a
        hole in it. Folding that in would set this flag False on every event of every real
        run, and rank (§7) uses it to promote the events it is least sure about -- a flag
        that is always on promotes nothing. The residue is reported by its own numbers on
        `Completeness` and by `render.CompletenessReport.boundary`.
        """
        return cls(
            tier=tier, unpaired=unpaired, models_complete=base.is_complete and head.is_complete
        )


def _derive_id(kind: ChangeEventKind, subject: str, payload: dict[str, AttrValue]) -> ChangeEventId:
    """Content-derived, so that re-running the pipeline produces the same JSON.

    A counter would make ids depend on iteration order, which breaks the §3 promise that
    each stage is a pure function of the previous one and makes `caused_by` links unstable
    between runs. Provenance, confidence and `caused_by` are excluded on purpose: the same
    change is the same event regardless of where it was found or what caused it (the same
    reason `Location` is not identity, ARCH-001 §4.1). Concretely, classify assigning a
    cause must not renumber an event that other events already point at.
    """
    h = hashlib.blake2b(digest_size=8)
    h.update(kind.value.encode())
    h.update(subject.encode())
    for key in sorted(payload):
        h.update(f"\x00{key}={payload[key]!r}".encode())
    return ChangeEventId(f"{kind.value}:{h.hexdigest()}")


class ChangeEvent(BaseModel):
    """ARCH-001 §6.2. The fields are exactly the seven named there.

    **Frozen**, like `Match`, `Hunk` and `Confidence`. ARCH-001 §3 specifies every stage as
    a pure function of the previous one, and a mutable event makes that a convention rather
    than a fact: any stage that returns its input list -- which is the obvious
    implementation of a stage that changes nothing -- hands two owners the same objects, and
    a later write edits an earlier stage's output retroactively. `classify` already defends
    itself by copying every event; freezing means the next stage does not have to remember
    to, and `render.DiffDocument.by_id` no longer indexes something that can change under it.

    The way to produce a modified event is `model_copy(update=...)`, which is what classify
    does to set `caused_by`. That is *not* a workaround: it also skips re-validation, which
    is exactly right here, because re-running the validator below would leave the derived
    `id` alone (it is already set) but would otherwise be a second chance to renumber an
    event other events already point at.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: ChangeEventId = Field(
        default=ChangeEventId(""),
        description="Derived from kind + subject + payload when not supplied, so it is "
        "stable across runs and survives a JSON round trip.",
    )
    kind: ChangeEventKind
    subject: AnyId

    payload: dict[str, AttrValue] = Field(
        default_factory=dict,
        description="Kind-specific, and scalar for the same reason node attributes are "
        "(ARCH-001 §4.3): the diff that produces it must stay mechanical. A nested payload "
        "would need bespoke construction and bespoke rendering per kind.",
    )

    caused_by: ChangeEventId | None = Field(
        default=None,
        description="The event this one is a consequence of (ARCH-001 §6.3). A link, never "
        "a removal: collapse happens at render, so the JSON always carries the full set. "
        "Events with no matching cause rule keep None and are shown.",
    )

    provenance: list[Hunk] = Field(
        default_factory=list,
        description="Source spans on either side. Empty is legitimate for a derived event, "
        "which may have no lines of its own.",
    )

    confidence: Confidence

    @field_validator("payload", mode="before")
    @classmethod
    def _payload_must_be_scalar(cls, v: object) -> object:
        # `before`, so the diagnostic names the key and the rule instead of surfacing
        # pydantic's generic union mismatch. Mirrors Node.attrs (ARCH-001 §4.3).
        if isinstance(v, dict):
            for key, value in v.items():
                if not isinstance(value, str | int | float | bool | type(None)):
                    raise ValueError(
                        f"payload[{key!r}] is {type(value).__name__}; event payloads must be "
                        f"scalar so that the diff stays mechanical (ARCH-001 §4.3)"
                    )
        return v

    @model_validator(mode="after")
    def _complete_the_event(self) -> ChangeEvent:
        missing = REQUIRED_PAYLOAD[self.kind] - self.payload.keys()
        if missing:
            raise ValueError(
                f"{self.kind.value} requires payload keys {sorted(missing)} (ARCH-001 §6.1)"
            )
        if not self.id:
            # `object.__setattr__` because the model is frozen and ordinary assignment
            # raises. Writing a *derived* field during construction is the one write a
            # frozen model must still allow: the alternative is to make callers compute the
            # id themselves, which puts the identity rule (`_derive_id`) in every producer
            # of an event instead of in the type that defines it. Nothing after
            # construction writes here -- every later change goes through `model_copy`.
            object.__setattr__(self, "id", _derive_id(self.kind, self.subject, self.payload))
        return self

    @property
    def is_derived(self) -> bool:
        """Derived events (§6.1) are computed, not read off a model. They are unsound in the
        way §11 requires stating -- a reachability delta is a lower bound, not a fact."""
        return self.kind in DERIVED_KINDS


def events_at(events: Iterable[ChangeEvent], side: Side, file: str, line: int) -> list[ChangeEvent]:
    """The reverse half of ARCH-001 §6.2: from a source line to the events it caused.

    Without this direction the tool is a second artifact to read *alongside* the diff, which
    is a net increase in the reviewer's work rather than a reduction. Linear here because
    render builds its index once; the point is that the schema makes the direction possible.
    """
    path = file.replace("\\", "/")  # same normalization Location applies
    return [
        e
        for e in events
        if any(
            h.side is side and h.location.file == path and h.contains(line) for h in e.provenance
        )
    ]
