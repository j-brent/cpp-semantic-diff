"""Tier 1: declared identity, keyed on the libclang USR (ARCH-001 §5).

In this codebase `NodeId` *is* the USR, so tier 1 is a keyed intersection of the two node
sets. That is not an accident of implementation -- it is why tier 1 is the most confident
tier: the USR already encodes kind, qualified name and signature, so overloads are distinct
without any signature comparison of ours, and body edits, reformatting and file moves cannot
disturb it.

The same encoding is what tier 1 cannot survive: a function whose parameters changed has a
new USR. The name tier (`match.name`) runs next and pairs those (ADR-036).

The one place the key is not simply the USR is **internal linkage**. See `DeclaredTier`. Its
file-and-TU part is `RenameMap.file_scope`, which the name tier keys on too.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field

from cpp_semantic_diff.model import MatchTier, Model, Node, NodeId

# Re-exported, not defined here. It moved to `model/ir.py` when the producer began deciding
# `Node.tu` with it: `_producer_code_digest` hashes `ir.py` and `extract.py` and nothing
# else, so a helper the stored partials depend on must live inside that digest, or changing
# it rewrites every partial while every cache key stays byte-identical. Callers of
# `match.usr_file` are unaffected.
from cpp_semantic_diff.model.ir import usr_file as usr_file

#: Tier-1 key: the (possibly rewritten) USR, plus the defining TU for entities whose USR is
#: file-scoped. ARCH-001 §5: "Identity must include the TU".
type DeclaredKey = tuple[str, str | None]


@dataclass(frozen=True)
class RenameMap:
    """Base-side path -> head-side path, as resolved by git.

    ARCH-001 §5 is explicit that file renames are "resolved from git before matching -- not
    discovered by the matcher". Discovering them here would mean inferring identity from the
    very content whose identity is in question. So this is an input, and
    `acquire.file_changes` supplies it from `git diff --name-status --find-renames`.

    What it costs when the map is wrong in either direction is visible rather than hidden:
    a rename that reaches here unresolved shows up in
    `Correspondence.internal_linkage_residue`.
    """

    by_path: Mapping[str, str] = field(default_factory=dict)
    by_name: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def of(cls, renames: Mapping[str, str] | None) -> RenameMap:
        if not renames:
            return cls()
        by_path = {_norm(b): _norm(h) for b, h in renames.items()}
        # USRs carry the file *name*, so the name-level map is what rewrites them. A name
        # that is ambiguous -- two renamed `util.cpp` in different directories -- is left
        # alone: rewriting it would fabricate an identity, which is worse than the missed
        # match it would fix. A pure directory move leaves the name unchanged and needs no
        # entry at all.
        counts = Counter(_name(b) for b in by_path)
        by_name = {
            _name(b): _name(h)
            for b, h in by_path.items()
            if counts[_name(b)] == 1 and _name(b) != _name(h)
        }
        return cls(by_path=by_path, by_name=by_name)

    def file_scope(self, node: Node) -> tuple[str | None, str | None]:
        """A file-scoped node's file name and defining TU, in head-side coordinates; (None,
        None) for an external-linkage node.

        The part of identity that internal linkage adds (ARCH-001 §5). One definition, so
        every tier that keys on it agrees on what a file rename does to it.
        """
        file = usr_file(node.id)
        if file is None:
            return (None, None)
        tu = self.by_path.get(_norm(node.tu), _norm(node.tu)) if node.tu else None
        return (self.by_name.get(file, file), tu)


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def _name(path: str) -> str:
    return _norm(path).rsplit("/", 1)[-1]


class DeclaredTier:
    """Tier 1 (ARCH-001 §5): match on the USR, in head-side coordinates.

    Base-side keys are rewritten through the rename map so both sides are expressed in the
    same space; head-side keys are already there. For an external-linkage entity the key is
    the USR alone -- deliberately *not* including the TU, because the same declared entity
    legitimately appears in many TUs and the set of TUs that see it changes for reasons that
    have nothing to do with the change under review.

    For a file-scoped USR the key is (USR, defining TU) -- ARCH-001 §5's "identity must
    include the TU". The USR's file component is only a file *name*, so a `static void
    helper()` in `a/util.cpp` and an unrelated one in `b/util.cpp` produce the *same* USR.
    Matching them to each other would report a body change where there are two different
    functions; the TU is what says they are not the same entity.

    Where `tu` is unset the key falls back to the USR alone. The producer sets it only for a
    file-scoped USR declared in its TU's own primary source (`extract._Extractor._defining_tu`),
    so a static defined in a header has none. The known way the TU can be arbitrary is the
    same-USR collision above within one side, which USR dedup collapses to a single node holding
    whichever TU won. If that winner flips between revisions the match is lost. That is the
    *safe* direction to fail -- a lost match becomes add + remove residue, which over-reports;
    dropping the TU instead would under-report by making two entities look like one, and
    confident-and-wrong is the failure mode this design refuses.
    """

    tier = MatchTier.DECLARED

    def __init__(self, renames: Mapping[str, str] | None = None) -> None:
        self.renames = RenameMap.of(renames)

    def key(self, node: Node) -> DeclaredKey:
        original = usr_file(node.id)
        if original is None:
            return (node.id, None)
        file, tu = self.renames.file_scope(node)
        return (f"c:{file}{node.id[2 + len(original) :]}", tu)

    def propose(
        self,
        base: Model,
        head: Model,
        base_pool: frozenset[NodeId],
        head_pool: frozenset[NodeId],
        head_for: Mapping[NodeId, NodeId],
    ) -> Iterator[tuple[NodeId, NodeId]]:
        head_index, _ = _index(head, head_pool, self.key)
        base_index, _ = _index(base, base_pool, self.key)
        for key, base_id in base_index.items():
            if (head_id := head_index.get(key)) is not None:
                yield base_id, head_id

    def ambiguous(
        self,
        base: Model,
        head: Model,
        base_pool: frozenset[NodeId],
        head_pool: frozenset[NodeId],
        head_for: Mapping[NodeId, NodeId],
    ) -> tuple[frozenset[NodeId], frozenset[NodeId]]:
        """The nodes `propose` declined to pair because a key collided (ADR-034).

        A collided key is ambiguous only where the other side has the key too: that is a
        choice tier 1 refused to make. Two same-named statics that both vanished collided
        on one side with nothing to choose between, and are ordinary removals.
        """
        base_index, base_collided = _index(base, base_pool, self.key)
        head_index, head_collided = _index(head, head_pool, self.key)
        base_keys = base_index.keys() | base_collided.keys()
        head_keys = head_index.keys() | head_collided.keys()
        refused = (base_collided.keys() | head_collided.keys()) & base_keys & head_keys

        def members(
            index: dict[DeclaredKey, NodeId], collided: dict[DeclaredKey, list[NodeId]]
        ) -> frozenset[NodeId]:
            return frozenset(
                n for k in refused for n in (collided[k] if k in collided else [index[k]])
            )

        return members(base_index, base_collided), members(head_index, head_collided)


def _index(
    model: Model, pool: Iterable[NodeId], key: Callable[[Node], DeclaredKey]
) -> tuple[dict[DeclaredKey, NodeId], dict[DeclaredKey, list[NodeId]]]:
    """Key -> node id, dropping any key two nodes share; and the dropped keys -> their nodes.

    A collision means the key does not identify a single entity on that side, and pairing
    either candidate would be a coin flip presented as a fact. Both go to the residue
    instead, where a later tier or the reviewer can see them -- and are reported as such, so
    the residue says the pairing was refused rather than that there was nothing to pair.
    """
    index: dict[DeclaredKey, NodeId] = {}
    collided: dict[DeclaredKey, list[NodeId]] = {}
    for node_id in sorted(pool):
        k = key(model.nodes[node_id])
        if k in collided:
            collided[k].append(node_id)
        elif k in index:
            collided[k] = [index.pop(k), node_id]
        else:
            index[k] = node_id
    return index, collided
