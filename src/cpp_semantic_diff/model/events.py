"""The delta: what a two-point analysis is allowed to say.

ARCH-001 §6. The event set is **closed**. A new signal becomes a new `EventKind`, never a
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
)

EventId = NewType("EventId", str)


class EventKind(StrEnum):
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

    # Edge
    EDGE_ADDED = "EdgeAdded"
    EDGE_REMOVED = "EdgeRemoved"
    EDGE_RETARGETED = "EdgeRetargeted"
    EDGE_KIND_CHANGED = "EdgeKindChanged"

    # Derived -- computed from both models plus graph algorithms, not read off either.
    BECAME_UNREACHABLE = "BecameUnreachable"
    BECAME_REACHABLE = "BecameReachable"
    CYCLE_INTRODUCED = "CycleIntroduced"
    FAN_IN_CHANGED = "FanInChanged"
    LAYER_VIOLATION_INTRODUCED = "LayerViolationIntroduced"
    SIGNATURE_INCOMPATIBLE = "SignatureIncompatible"


NODE_KINDS: frozenset[EventKind] = frozenset(
    {
        EventKind.NODE_ADDED,
        EventKind.NODE_REMOVED,
        EventKind.NODE_MOVED,
        EventKind.NODE_RENAMED,
        EventKind.NODE_ATTR_CHANGED,
        EventKind.NODE_BODY_CHANGED,
    }
)

EDGE_KINDS: frozenset[EventKind] = frozenset(
    {
        EventKind.EDGE_ADDED,
        EventKind.EDGE_REMOVED,
        EventKind.EDGE_RETARGETED,
        EventKind.EDGE_KIND_CHANGED,
    }
)

DERIVED_KINDS: frozenset[EventKind] = frozenset(EventKind) - NODE_KINDS - EDGE_KINDS

#: What v0 emits (ARCH-001 §12): node add/remove/attr/body and edge add/remove. The rest of
#: the set is declared here because the schema is the contract the whole pipeline is written
#: against; declaring it late would mean rewriting consumers rather than extending them.
V0_KINDS: frozenset[EventKind] = frozenset(
    {
        EventKind.NODE_ADDED,
        EventKind.NODE_REMOVED,
        EventKind.NODE_ATTR_CHANGED,
        EventKind.NODE_BODY_CHANGED,
        EventKind.EDGE_ADDED,
        EventKind.EDGE_REMOVED,
    }
)

#: Payload keys a kind must carry, for the kinds ARCH-001 §6.1 names or directly implies.
#: This is a floor, not a ceiling: a kind may carry more, but a consumer switching on the
#: kind can rely on these being present. The derived kinds carry no requirement because
#: their payloads are not designed yet (§12 puts them out of v0) -- an empty entry here
#: means "undesigned", and adding a kind without adding an entry is a deliberate choice
#: rather than an oversight, which is why every kind appears.
REQUIRED_PAYLOAD: dict[EventKind, frozenset[str]] = {
    EventKind.NODE_ADDED: frozenset(),
    EventKind.NODE_REMOVED: frozenset(),
    EventKind.NODE_MOVED: frozenset({"old_parent", "new_parent"}),
    EventKind.NODE_RENAMED: frozenset({"old_name", "new_name"}),
    EventKind.NODE_ATTR_CHANGED: frozenset({"attr", "old", "new"}),
    EventKind.NODE_BODY_CHANGED: frozenset(),
    EventKind.EDGE_ADDED: frozenset(),
    EventKind.EDGE_REMOVED: frozenset(),
    EventKind.EDGE_RETARGETED: frozenset({"old_dst", "new_dst"}),
    EventKind.EDGE_KIND_CHANGED: frozenset({"old_kind", "new_kind"}),
    EventKind.BECAME_UNREACHABLE: frozenset(),
    EventKind.BECAME_REACHABLE: frozenset(),
    EventKind.CYCLE_INTRODUCED: frozenset(),
    EventKind.FAN_IN_CHANGED: frozenset(),
    EventKind.LAYER_VIOLATION_INTRODUCED: frozenset(),
    EventKind.SIGNATURE_INCOMPATIBLE: frozenset(),
}


class Side(StrEnum):
    """Which of the two points a hunk belongs to. A two-point analysis has exactly two."""

    BASE = "base"
    HEAD = "head"


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
        return self.location.line <= line <= end


class Confidence(BaseModel):
    """The two inputs ARCH-001 §11 names, carried verbatim -- not a score.

    §11: "Every event carries confidence derived from its match tier and the completeness of
    both models." The derivation is deliberately *not* performed here. Rank (§7) owns the
    weighting, and it has the inverting rule that makes a premature scalar dangerous: a
    low-confidence event ranks **up**, not down. Collapsing tier and completeness into one
    number here would force that rule to be re-derived from a lossy input.

    Both fields are required. A default would let a caller omit completeness and thereby
    claim, silently, that nothing was missing -- the exact substitution of absence of
    evidence for evidence of absence that §11 exists to prevent.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tier: MatchTier | None = Field(
        description="The tier that matched the subject, or None when it was unmatched "
        "residue. In v0 only tier 1 runs (ARCH-001 §12), so None also covers every rename "
        "-- which is what makes the rename damage measurable rather than invisible.",
    )
    models_complete: bool = Field(
        description="Both models parsed every TU they attempted. What was missing lives in "
        "the per-side `Completeness` records (ARCH-001 §4.4); this is only the flag that "
        "tells a consumer it has to say so.",
    )

    @classmethod
    def derive(cls, tier: MatchTier | None, base: Completeness, head: Completeness) -> Confidence:
        return cls(tier=tier, models_complete=base.is_complete and head.is_complete)


def _derive_id(kind: EventKind, subject: str, payload: dict[str, AttrValue]) -> EventId:
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
    return EventId(f"{kind.value}:{h.hexdigest()}")


class Event(BaseModel):
    """ARCH-001 §6.2. The fields are exactly the seven named there."""

    model_config = ConfigDict(extra="forbid")

    id: EventId = Field(
        default=EventId(""),
        description="Derived from kind + subject + payload when not supplied, so it is "
        "stable across runs and survives a JSON round trip.",
    )
    kind: EventKind
    subject: AnyId

    payload: dict[str, AttrValue] = Field(
        default_factory=dict,
        description="Kind-specific, and scalar for the same reason node attributes are "
        "(ARCH-001 §4.3): the diff that produces it must stay mechanical. A nested payload "
        "would need bespoke construction and bespoke rendering per kind.",
    )

    caused_by: EventId | None = Field(
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
    def _complete_the_event(self) -> Event:
        missing = REQUIRED_PAYLOAD[self.kind] - self.payload.keys()
        if missing:
            raise ValueError(
                f"{self.kind.value} requires payload keys {sorted(missing)} (ARCH-001 §6.1)"
            )
        if not self.id:
            self.id = _derive_id(self.kind, self.subject, self.payload)
        return self

    @property
    def is_derived(self) -> bool:
        """Derived events (§6.1) are computed, not read off a model. They are unsound in the
        way §11 requires stating -- a reachability delta is a lower bound, not a fact."""
        return self.kind in DERIVED_KINDS


def events_at(events: Iterable[Event], side: Side, file: str, line: int) -> list[Event]:
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
