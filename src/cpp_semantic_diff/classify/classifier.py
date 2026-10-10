"""The classify stage: an event list in, the same event list out with `caused_by` set.

ARCH-001 §3, §6.3. This stage **adds links and nothing else**. It removes no event, merges
none, groups none and suppresses none, and it returns exactly as many events as it was given.
§6.3 puts cascade collapse at render: the JSON always carries the full event set and the view
hides consequences under causes, because a hidden real change is a far worse failure than a
redundant one. A stage that deduplicated here -- and the temptation is real, since 200
consequences under one cause look like 199 events too many -- would delete changes from the
document every downstream view reads, with no way for any of them to know.

The rules live in `rules.py` and are a table, per §6.3's insistence that cause links are
explicit rather than inferred. This module is only the driver: it indexes the events, asks
each rule, and refuses any link that would close a cycle.

Purity (§3) has a specific consequence here worth stating. `Event.id` is derived from kind,
subject and payload and deliberately *excludes* `caused_by` (`events.py`), so assigning a
cause cannot renumber an event that other events already point at. That is what makes a
single pass sound: the ids are fixed before this stage begins and are still the same ids
when it ends.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence

from cpp_semantic_diff.classify.context import ClassifyContext
from cpp_semantic_diff.classify.rules import CAUSE_RULES, CauseRule
from cpp_semantic_diff.match import Correspondence
from cpp_semantic_diff.model import AnyId, AttrValue, Event, EventId, EventKind, Model

#: (kind, subject) -> the events with that kind and subject, by id. A pair is not unique:
#: `NodeAttrChanged` emits one event per attribute on the same subject (§6.1), which is why
#: the rule table can filter a cause on its payload.
type EventIndex = Mapping[tuple[EventKind, AnyId], Sequence[Event]]


def classify_events(
    events: Sequence[Event],
    base: Model,
    head: Model,
    correspondence: Correspondence,
) -> list[Event]:
    """Link consequences to causes (ARCH-001 §6.3). Same events, same order, same ids.

    Pure: the inputs are not touched, and the returned events are copies carrying the one
    field this stage owns.
    """
    ctx = ClassifyContext.build(base, head, correspondence)
    index = _index(events)

    # Assigned in id order, not input order. The cycle check below reads the links assigned
    # so far, so the order in which links are proposed is part of the answer -- and input
    # order is a property of the caller, which would make the stage a function of how it was
    # called rather than of what it was given (§3).
    links: dict[EventId, EventId] = {}
    for event in sorted(events, key=lambda e: e.id):
        cause = _cause_for(ctx, index, event)
        if cause is not None and not _reaches(links, cause, event.id):
            links[event.id] = cause

    # A copy per event, including the unlinked ones. Returning the caller's objects would
    # leave the two lists sharing mutable state, and `Event` is not frozen -- a later stage
    # setting a field would edit the diff stage's output retroactively.
    return [event.model_copy(update={"caused_by": links.get(event.id)}) for event in events]


def _index(events: Sequence[Event]) -> EventIndex:
    by_key: dict[tuple[EventKind, AnyId], list[Event]] = defaultdict(list)
    for event in events:
        by_key[(event.kind, event.subject)].append(event)
    # Sorted by id so that a rule whose candidate is ambiguous at the payload level still
    # gets the same answer on every run.
    return {key: sorted(group, key=lambda e: e.id) for key, group in by_key.items()}


def _cause_for(ctx: ClassifyContext, index: EventIndex, event: Event) -> EventId | None:
    """The first cause the table finds for this event, or None -- which is a real answer.

    §6.3: events with no matching rule get no cause link and are shown. Guessing at one from
    proximity, or from a shared file, is the inference the rule table exists to replace.
    """
    for rule in CAUSE_RULES:
        if rule.consequence is not event.kind:
            continue
        for subject in rule.subjects(ctx, event):
            for candidate in index.get((rule.cause, subject), ()):
                # An event cannot cause itself. Reachable in principle -- a malformed model
                # containing a node in itself would ask for exactly that -- and §6.3 would
                # rather show the event than record a link that means nothing.
                if candidate.id != event.id and _payload_matches(candidate.payload, rule):
                    return candidate.id
    return None


def _payload_matches(payload: Mapping[str, AttrValue], rule: CauseRule) -> bool:
    return all(payload.get(key) == value for key, value in rule.cause_payload.items())


def _reaches(links: Mapping[EventId, EventId], start: EventId, target: EventId) -> bool:
    """Whether following `caused_by` from `start` arrives at `target`.

    A cause chain must be a chain. `caused_by` is a single optional id, so the link graph is
    a forest of chains and adding `event -> cause` closes a cycle exactly when `cause`
    already reaches `event`; checking that before each assignment keeps the whole graph
    acyclic by construction. The cycle is not hypothetical: containment comes from the
    model, and a model with a containment cycle -- which the IR does not forbid -- would
    otherwise produce two events each claiming to be the other's consequence, and render
    would hide both under each other and show neither.

    The refused link becomes no link, not an error. §6.3: when cause attribution is
    uncertain, default to showing.
    """
    seen: set[EventId] = set()
    current: EventId | None = start
    while current is not None and current not in seen:
        if current == target:
            return True
        seen.add(current)
        current = links.get(current)
    return False
