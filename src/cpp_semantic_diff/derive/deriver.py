"""The derive stage's driver: the diff's events in, those events plus the derived ones out.

A pure function of its four inputs, like every stage after produce (ARCH-001 §3). It removes,
merges and changes no event it is given, and it sets no `caused_by`: linking is classify's job,
and classify runs next.
"""

from __future__ import annotations

from collections.abc import Sequence

from cpp_semantic_diff.derive.callers import callers_of_changed_signatures
from cpp_semantic_diff.match import Correspondence
from cpp_semantic_diff.model import ChangeEvent, Model


def derive_events(
    events: Sequence[ChangeEvent],
    base: Model,
    head: Model,
    correspondence: Correspondence,
) -> list[ChangeEvent]:
    """Every input event, plus each derived event the inputs support (ADR-040).

    Derived only from the events the diff read off the models, never from an earlier derived
    event, so running the stage twice adds nothing the first run did not: an event whose id is
    already in the input is not added again.

    Sorted by the diff stage's key, so the same inputs give the same JSON (§3).
    """
    known = {e.id for e in events}
    derived = [
        e
        for e in callers_of_changed_signatures(events, base, head, correspondence)
        if e.id not in known
    ]
    return sorted([*events, *derived], key=lambda e: (e.kind.value, e.subject, e.id))
