"""Node events (ARCH-001 §6.1): added, removed, moved, renamed, attr changed, body changed,
signature changed.

The residue of the correspondence is the add/remove half and needs no comparison: a base
node no tier claimed is removed, a head node no tier claimed is added. The matched half is
a field-by-field comparison of two nodes the match stage has already declared to be the
same entity, which is what makes each difference a *change* to one thing rather than the
disappearance of one thing and the appearance of another.

One of the seven kinds cannot fire in v0, and one fires in a single case; that is the point
rather than a gap. Tier 1 keys on the USR, which encodes the entity's name and its enclosing
scope (ARCH-001 §5), and the name tier keys on the name under a parent that is already paired
(ADR-036), so a node whose parent differs from its match's does not exist -- the pair would
never have been matched -- and `NodeMoved` is written against what a later tier will produce.
`NodeRenamed` fires only for an inline namespace whose version tag was bumped (`v1` to `v2`,
ADR-044), the one rename v0 pairs, on evidence from what is inside it. Softening either here --
pairing a removed node with an added one because the names look related -- would move an
identity decision out of the matcher and destroy the measurement v0 exists to take (ARCH-001
§13).
"""

from __future__ import annotations

from collections.abc import Iterator

from cpp_semantic_diff.diff.attrs import changed_attrs
from cpp_semantic_diff.diff.context import DiffContext, node_hunks
from cpp_semantic_diff.match import Correspondence, Match, containers
from cpp_semantic_diff.model import (
    APPLIES_TO,
    AttrValue,
    ChangeEvent,
    ChangeEventKind,
    NodeId,
    NodeKind,
    Side,
)

#: The attributes a signature already spells. The canonical signature is the function type
#: (ARCH-001 §4.1), so it carries the return type and, for a method, the `const` qualifier.
#: Where the signature changed, `NodeSignatureChanged` reports these too, and an attribute
#: event beside it would say one change twice (ADR-035). Where the signature did not change,
#: neither can these, so nothing is lost by the suppression. `sugared_signature` is the same
#: function type as written: it changes with the signature in all but the alias case, where it
#: does not change at all, so an event for it beside `NodeSignatureChanged` would always repeat
#: it. Alone, it is the spelling changing while the type did not, and is reported.
SIGNATURE_ATTRS: frozenset[str] = frozenset({"return_type", "const", "sugared_signature"})

#: A record's bases in the order written (`extract._base_order`). Reported only where the
#: bases present on both sides are in a different relative order: an added or removed base is
#: already an `inherits` edge event, and reporting the order beside it would say it twice. A
#: reorder alongside an addition is still a reorder, and is still reported (ADR-043).
BASE_ORDER = "base_order"

#: A function's default arguments as clang prints them (`extract._default_arguments`). A
#: change that only adds or drops a qualifier naming a namespace the function is in carries
#: `spelling_only: true` in its payload, and rank does not count it as high risk (ADR-046).
DEFAULT_ARGUMENTS = "default_arguments"


def node_events(ctx: DiffContext) -> Iterator[ChangeEvent]:
    corr = ctx.correspondence

    for node_id in corr.unmatched_base:
        node = ctx.base.nodes[node_id]
        yield ChangeEvent(
            kind=ChangeEventKind.NODE_REMOVED,
            # Base coordinates: the subject does not exist at head, so head has no name for
            # it. Everything else in this stage is expressed in head coordinates, which is
            # what lets classify (§6.3) join an event about a node to events about its
            # edges without translating between the two sides again.
            subject=node_id,
            provenance=node_hunks((Side.BASE, node)),
            confidence=ctx.confidence(None, corr.unpaired_base[node_id]),
        )

    for node_id in corr.unmatched_head:
        node = ctx.head.nodes[node_id]
        yield ChangeEvent(
            kind=ChangeEventKind.NODE_ADDED,
            subject=node_id,
            provenance=node_hunks((Side.HEAD, node)),
            confidence=ctx.confidence(None, corr.unpaired_head[node_id]),
        )

    base_parents = containers(ctx.base)
    head_parents = containers(ctx.head)
    for match in corr.matches:
        yield from _changes(ctx, match, base_parents, head_parents)


def _changes(
    ctx: DiffContext,
    match: Match,
    base_parents: dict[NodeId, NodeId],
    head_parents: dict[NodeId, NodeId],
) -> Iterator[ChangeEvent]:
    base = ctx.base.nodes[match.base]
    head = ctx.head.nodes[match.head]

    def event(kind: ChangeEventKind, **payload: AttrValue) -> ChangeEvent:
        return ChangeEvent(
            kind=kind,
            subject=match.head,
            payload=dict(payload),
            provenance=node_hunks((Side.BASE, base), (Side.HEAD, head)),
            confidence=ctx.confidence(match.tier),
        )

    old_parent = base_parents.get(match.base)
    new_parent = head_parents.get(match.head)
    if _parent_changed(ctx.correspondence, old_parent, new_parent):
        yield event(ChangeEventKind.NODE_MOVED, old_parent=old_parent, new_parent=new_parent)

    if base.name != head.name:
        yield event(ChangeEventKind.NODE_RENAMED, old_name=base.name, new_name=head.name)

    # The signature is a field of the node (§4.1), never an attribute, so it gets its own
    # kind. Only a function or a method has one (§6.1's applicability table, `APPLIES_TO`).
    signature_changed = (
        head.kind in APPLIES_TO[ChangeEventKind.NODE_SIGNATURE_CHANGED]
        and base.signature is not None
        and head.signature is not None
        and base.signature != head.signature
    )
    if signature_changed:
        yield event(
            ChangeEventKind.NODE_SIGNATURE_CHANGED,
            old_signature=base.signature,
            new_signature=head.signature,
        )

    # One event per attribute, each naming the attribute it is about (§6.1). The rule --
    # union of the key sets, compared with the type -- is `attrs.changed_attrs`, shared with
    # the edge half so that `NodeAttrChanged` and `EdgeAttrChanged` cannot come to mean
    # different things.
    for attr, old, new in changed_attrs(base.attrs, head.attrs):
        if signature_changed and attr in SIGNATURE_ATTRS:
            continue
        if attr == BASE_ORDER and not bases_reordered(old, new):
            continue
        if attr == DEFAULT_ARGUMENTS and defaults_differ_only_in_spelling(
            old, new, _enclosing_namespaces(ctx, match, base_parents, head_parents)
        ):
            yield event(
                ChangeEventKind.NODE_ATTR_CHANGED, attr=attr, old=old, new=new, spelling_only=True
            )
            continue
        yield event(ChangeEventKind.NODE_ATTR_CHANGED, attr=attr, old=old, new=new)

    # ARCH-001 §4.1: `body_hash` is deliberately not an attribute, so a differing body is
    # never a `NodeAttrChanged`. An attribute change has a specific known meaning; a body
    # change only means "look closer", and the two carry different weight in rank (§7) and
    # render differently.
    both_have_bodies = base.body_hash is not None and head.body_hash is not None
    has_a_body = head.kind in APPLIES_TO[ChangeEventKind.NODE_BODY_CHANGED]
    if has_a_body and both_have_bodies and base.body_hash != head.body_hash:
        yield event(ChangeEventKind.NODE_BODY_CHANGED)
    # A hash on one side and None on the other is *not* a body change and gets no event.
    # It says a definition appeared or disappeared while the declaration stayed -- a
    # `defines`/`declares` edge fact in ARCH-001 §4.2's vocabulary, and §13 keeps both
    # edge kinds out of v0, so the closed event set has no way to say it. Reporting it as
    # a body change would claim something inside changed when there was nothing inside
    # before. It is also indistinguishable, from the models alone, from a completeness
    # artifact: the TU holding the definition failing to parse on one side produces the
    # identical shape, and turning that into an event is §11's laundering of absence of
    # evidence into evidence of absence.


def defaults_differ_only_in_spelling(
    old: AttrValue, new: AttrValue, namespaces: frozenset[str]
) -> bool:
    """Whether two `default_arguments` values differ only in qualifiers that name a namespace
    the function is itself in (ADR-046).

    Clang prints a default as it was written, so `spdlog::details::x` becoming `details::x`
    inside `spdlog` is a change to the attribute and not to what the default is. Two defaults
    match where one is the other with a leading enclosing-namespace qualifier added. Removing
    a qualifier from both sides is not enough: inside `ns::details`, `ns::x` and
    `ns::details::x` both reduce to `x`, and they name different entities. Only a whole
    leading qualifier is removed, so `spdlog::` never matches inside `spdlogx::`, and a
    qualifier naming any other namespace still counts as a change.
    """
    if not isinstance(old, str) or not isinstance(new, str):
        return False
    before = old.split("; ") if old else []
    after = new.split("; ") if new else []
    if len(before) != len(after):
        return False
    return all(
        a in _unqualified(b, namespaces) or b in _unqualified(a, namespaces)
        for a, b in zip(before, after, strict=True)
    )


def _unqualified(default: str, namespaces: frozenset[str]) -> set[str]:
    """`default`, and `default` without each leading qualifier that is an enclosing namespace.

    A leading `::` goes only with such a qualifier: inside `ns`, `::x` names the global `x`,
    and `x` may find `ns::x`.
    """
    spellings = {default}
    for text in (default, default.removeprefix("::")):
        for namespace in namespaces:
            if text.startswith(namespace + "::"):
                spellings.add(text[len(namespace) + 2 :])
                spellings.add(text)
    return spellings


def _enclosing_namespaces(
    ctx: DiffContext,
    match: Match,
    base_parents: dict[NodeId, NodeId],
    head_parents: dict[NodeId, NodeId],
) -> frozenset[str]:
    """The qualified names of the namespaces a matched node is in, on either side."""
    names: set[str] = set()
    for model, parents, node_id in (
        (ctx.base, base_parents, match.base),
        (ctx.head, head_parents, match.head),
    ):
        seen: set[NodeId] = set()
        parent = parents.get(node_id)
        while parent is not None and parent not in seen and parent in model.nodes:
            seen.add(parent)
            node = model.nodes[parent]
            if node.kind is NodeKind.NAMESPACE:
                names.add(node.qualified_name)
            parent = parents.get(parent)
    return frozenset(names)


def bases_reordered(old: AttrValue, new: AttrValue) -> bool:
    """Whether the bases on both sides of a `base_order` change are in a different order.

    False where either side is not determined: there is no order to compare against.
    """
    if not isinstance(old, str) or not isinstance(new, str):
        return False
    before = old.split("; ") if old else []
    after = new.split("; ") if new else []
    common = set(before) & set(after)
    return [b for b in before if b in common] != [b for b in after if b in common]


def _parent_changed(
    corr: Correspondence, old_parent: NodeId | None, new_parent: NodeId | None
) -> bool:
    """Whether the two parents are different *entities* -- not different ids.

    The comparison has to go through the correspondence for the same reason edges do: if
    the container was itself renamed, its base and head ids differ while the containment
    did not change, and comparing ids would report every member of a renamed class as
    moved.
    """
    if old_parent is None or new_parent is None:
        return (old_parent is None) != (new_parent is None)
    return corr.head_for(old_parent) != new_parent
