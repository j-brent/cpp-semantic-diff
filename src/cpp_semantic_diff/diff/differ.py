"""The diff stage: two `Model`s plus a `Correspondence` -> a list of `Event` (ARCH-001 §6).

A pure function of its three inputs and nothing else. It reads no files, consults no
parser, and does not touch the models it is given. That is what makes the stage testable
from models built by hand, and it is also the seam that lets the whole pipeline be checked
cheaply: two models with the same `Model.fingerprint()` must produce an empty list.

Three things this stage deliberately does *not* do.

**It does not decide identity.** Every "these two are the same entity" judgement arrives in
the `Correspondence`. v0 runs tier 1 alone (ARCH-001 §13), so a renamed function arrives as
one unmatched base node and one unmatched head node and leaves here as `NodeRemoved` plus
`NodeAdded`. That reads badly on purpose: the size of that residue is the measurement that
decides whether tiers 2-4 are worth building (STATUS §6), and any softening here -- a
name-based fallback, a similarity guess -- would both destroy the measurement and put an
identity decision somewhere nobody would think to look for one.

**It does not collapse cascades.** Removing a class emits the removal of the class, of
every member, and of every edge that touched them. ARCH-001 §6.3 puts collapse at render
and cause attribution in classify, so `caused_by` is left None on every event here: the
JSON always carries the full set, and a hidden real change is a far worse failure than a
redundant one.

**It does not rank or order.** The output is sorted only so that the same inputs produce
the same JSON on every run; §7 owns what a reviewer should read first.
"""

from __future__ import annotations

from cpp_semantic_diff.diff.context import DiffContext
from cpp_semantic_diff.diff.edges import edge_events
from cpp_semantic_diff.diff.nodes import node_events
from cpp_semantic_diff.match import Correspondence
from cpp_semantic_diff.model import Event, Model, NodeId, Side


def diff_models(base: Model, head: Model, correspondence: Correspondence) -> list[Event]:
    """Every node and edge event the two models and their correspondence support."""
    matches = correspondence.matches
    _check_covers(base, {m.base for m in matches}, correspondence.unmatched_base, Side.BASE)
    _check_covers(head, {m.head for m in matches}, correspondence.unmatched_head, Side.HEAD)

    ctx = DiffContext(base=base, head=head, correspondence=correspondence)
    events = [*node_events(ctx), *edge_events(ctx)]

    # Sorted by content, never by construction order. ARCH-001 §3 makes each stage a pure
    # function of the last, and an event list whose order depended on dict iteration would
    # produce a different JSON document for the same two commits.
    return sorted(events, key=lambda e: (e.kind.value, e.subject, e.id))


def _check_covers(model: Model, matched: set[NodeId], residue: list[NodeId], side: Side) -> None:
    """Every node on this side is either matched or residue -- exactly one of them.

    `Correspondence` enforces that no node is both (ARCH-001 §5) but cannot enforce that
    none is neither: it never sees the models. A node in neither list produces no event at
    all, so a correspondence built against a different pair of models -- or a tier that
    dropped part of its pool -- would delete entities from the delta silently. That is the
    §11 failure this project refuses: absence of evidence presented as evidence of absence.
    """
    accounted = matched | set(residue)
    if stray := accounted - set(model.nodes):
        raise ValueError(
            f"correspondence names {len(stray)} {side.value} node(s) the {side.value} model "
            f"does not contain, e.g. {sorted(stray)[0]!r}; it was built against a different "
            f"model"
        )
    if missing := set(model.nodes) - accounted:
        raise ValueError(
            f"{len(missing)} {side.value} node(s) are neither matched nor residue, e.g. "
            f"{sorted(missing)[0]!r}; they would produce no event at all (ARCH-001 §5, §11)"
        )
