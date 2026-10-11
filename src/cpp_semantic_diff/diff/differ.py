"""The diff stage: two `Model`s plus a `Correspondence` -> a list of `ChangeEvent` (ARCH-001 §6).

A pure function of its three inputs and nothing else. It reads no files, consults no
parser, and does not touch the models it is given. That is what makes the stage testable
from models built by hand, and it is also the seam that lets the whole pipeline be checked
cheaply: two models with the same `Model.fingerprint()` must produce an empty list.

Three things this stage deliberately does *not* do.

**It does not decide identity.** Every "these two are the same entity" judgement arrives in the
`Correspondence`. v0 runs tier 1 alone (ARCH-001 §13), so a renamed function arrives as one
unmatched base node and one unmatched head node and leaves here as `NodeRemoved` plus
`NodeAdded`. That reads badly on purpose: the size of that residue is the measurement that
decides whether tiers 2-4 are worth building, and any softening here -- a name-based fallback,
a similarity guess -- would both destroy the measurement and put an identity decision somewhere
nobody would think to look for one.

**It does not collapse cascades.** Removing a class emits the removal of the class, of
every member, and of every edge that touched them. ARCH-001 §6.3 puts collapse at render
and cause attribution in classify, so `caused_by` is left None on every event here: the
JSON always carries the full set, and a hidden real change is a far worse failure than a
redundant one.

**It does not rank or order.** The output is sorted only so that the same inputs produce
the same JSON on every run; §7 owns what a reviewer should read first.

What it *does* do beyond subtracting is check the two preconditions that no earlier stage
is in a position to check: that both models were produced by one toolchain (§12.2), and
that the correspondence was built against these two models (§5). Both are checks on the
relationship between the inputs, and this is the first stage that holds all of them.
"""

from __future__ import annotations

from cpp_semantic_diff.diff.context import DiffContext
from cpp_semantic_diff.diff.edges import edge_events
from cpp_semantic_diff.diff.nodes import node_events
from cpp_semantic_diff.errors import ToolchainMismatchError
from cpp_semantic_diff.match import Correspondence
from cpp_semantic_diff.model import ChangeEvent, Model, NodeId, Side


def diff_models(base: Model, head: Model, correspondence: Correspondence) -> list[ChangeEvent]:
    """Every node and edge event the two models and their correspondence support."""
    _check_one_toolchain(base, head)

    matches = correspondence.matches
    _check_covers(base, {m.base for m in matches}, correspondence.unmatched_base, Side.BASE)
    _check_covers(head, {m.head for m in matches}, correspondence.unmatched_head, Side.HEAD)

    ctx = DiffContext(base=base, head=head, correspondence=correspondence)
    events = [*node_events(ctx), *edge_events(ctx)]

    # Sorted by content, never by construction order. ARCH-001 §3 makes each stage a pure
    # function of the last, and an event list whose order depended on dict iteration would
    # produce a different JSON document for the same two commits.
    return sorted(events, key=lambda e: (e.kind.value, e.subject, e.id))


def _check_one_toolchain(base: Model, head: Model) -> None:
    """Both models came from one toolchain, or there is no diff (ARCH-001 §12.2).

    Asserted here because here is where it is relied on. The cache makes it true
    *incidentally* -- a changed producer misses cache and forces a reparse -- but that is a
    consequence of a caching decision, and a caching decision is free to change. Nothing at
    the match boundary compares the two `Completeness.producers` records either, and
    `Correspondence` could not: it never sees a model. This stage holds both, which is the
    same reason `_check_covers` lives here.

    It is a `PreconditionError` and not a soft note on purpose. §11's rule is that a
    condition eliminating the analysis is a hard failure, and this one does not degrade the
    delta, it fabricates entries in it -- a `NodeAdded` for a member `std::string` gained
    between two libstdc++ versions is indistinguishable from a real one.

    Be clear about what this catches *today*: two models built in one process share one
    memoized `Toolchain` and always agree, so on the current `cppsd diff` path it cannot
    fire. It guards the paths §9 and §12.3 make inevitable rather than one that exists --
    a base model precomputed by CI and handed to a later process, a cache directory shared
    between hosts, an API caller assembling two models it did not build together. The
    parser is host-dependent by choice (§12.3), so those are the paths where the identity
    is the only thing standing between a host difference and an invented event.
    """
    b, h = base.completeness, head.completeness
    # Both records, not just the one §12.2 names. `producers` says what ran and
    # `producer_version` is the value folded into every cache key (§9); `build.py` fills
    # both from `Toolchain.id`, so a model where they disagree came from somewhere else and
    # that is exactly the caller this check exists for.
    differs = [
        field
        for field, left, right in (
            ("producers", list(b.producers), list(h.producers)),
            ("producer_version", b.producer_version, h.producer_version),
        )
        if left != right
    ]
    if differs:
        raise ToolchainMismatchError(
            " · ".join(b.producers) or b.producer_version,
            " · ".join(h.producers) or h.producer_version,
            differs=differs,
        )


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
