"""The name tier: functions and methods tier 1 left over, paired by name (ADR-036).

A function's USR encodes its parameter types, and a method's encodes its `const` as well, so
tier 1 cannot pair a function with itself after its parameters changed: the old version is
reported removed and the new one added (ADR-026). This tier pairs what tier 1 left, on what a
signature change keeps -- the kind, the name and the scope -- and refuses wherever
that is not enough to say which old function is which new one. The signature, the template
parameters and everything else become compared state, which the diff stage reports.

It runs over the residue only, so a function whose USR held is never re-paired here, and it
is the reason an overload whose siblings are all unchanged pairs at all: tier 1 takes the
siblings out of the pools first.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Mapping
from typing import NamedTuple

from cpp_semantic_diff.match.declared import RenameMap
from cpp_semantic_diff.match.unpaired import containers
from cpp_semantic_diff.model import MatchTier, Model, Node, NodeId, NodeKind

#: Only functions and methods have a signature that can change while the entity stays the
#: same one. Every other kind's USR holds exactly its kind, name and scope, which is this
#: tier's key, so tier 1 has already paired everything this tier could.
PAIRED_BY_NAME: frozenset[NodeKind] = frozenset({NodeKind.FUNCTION, NodeKind.METHOD})


class NameKey(NamedTuple):
    """What identifies a function once its signature is set aside, in head coordinates."""

    kind: NodeKind
    #: The unqualified name. The paired parent below already says where it is, and the
    #: qualified name would say it again in base coordinates: under a bumped inline
    #: namespace (ADR-044) `lib::v1::f` and `lib::v2::f` are one function in one paired
    #: scope, and keying on the spelling of that scope would refuse them.
    name: str
    #: For an internal-linkage function, its file name and defining TU, through the git
    #: rename map -- the same rule tier 1 applies (`RenameMap.file_scope`). Two same-named
    #: statics in different files are different functions.
    file: str | None
    tu: str | None
    #: The head id of the parent, which the matches made so far must already have paired.
    #: None at global scope.
    parent: NodeId | None


#: Base nodes and head nodes sharing one key.
type _Group = tuple[list[NodeId], list[NodeId]]


class NameTier:
    """Pairs a leftover function or method with the one leftover of the same name.

    The rules (ADR-036):

    - **The parent must already be paired.** A function whose class or namespace is itself
      unpaired is left alone, so this tier never produces a `NodeMoved`. Pairing members of
      two unpaired namespaces would be a claim about the namespaces first. An inline
      namespace bump (`v1` to `v2`) is paired by the tier before this one (ADR-044), so a
      function whose signature changed in the same bump pairs here under its paired namespace.
    - **One to one, or nothing.** A key with exactly one leftover on each side pairs. A key
      with leftovers on both sides that are not one to one -- two old overloads and two new
      ones, say -- pairs none of them and reports them as ambiguous. There is no tie-break:
      choosing by signature similarity would be a guess presented as a fact (ADR-003). A key
      with leftovers on one side only is a genuine addition or removal.

    One consequence is accepted on purpose: with `f(int)` and `f(double)` before and
    `f(int)` and `f(long)` after, tier 1 pairs `f(int)`, and this tier pairs `f(double)` with
    `f(long)`, because they are the only two left. A template and a non-template of the same
    name pair the same way, and the change shows as `template_parameters`.
    """

    tier = MatchTier.NAME

    def __init__(self, renames: Mapping[str, str] | None = None) -> None:
        self.renames = RenameMap.of(renames)

    def propose(
        self,
        base: Model,
        head: Model,
        base_pool: frozenset[NodeId],
        head_pool: frozenset[NodeId],
        head_for: Mapping[NodeId, NodeId],
    ) -> Iterator[tuple[NodeId, NodeId]]:
        for olds, news in self._groups(base, head, base_pool, head_pool, head_for).values():
            if len(olds) == 1 and len(news) == 1:
                yield olds[0], news[0]

    def ambiguous(
        self,
        base: Model,
        head: Model,
        base_pool: frozenset[NodeId],
        head_pool: frozenset[NodeId],
        head_for: Mapping[NodeId, NodeId],
    ) -> tuple[frozenset[NodeId], frozenset[NodeId]]:
        """Every leftover under a key with leftovers on both sides that are not one to one.

        A choice this tier refused, and so the most doubtful residue there is (ADR-034).
        """
        groups = self._groups(base, head, base_pool, head_pool, head_for).values()
        refused = [(olds, news) for olds, news in groups if olds and news]
        refused = [(olds, news) for olds, news in refused if len(olds) != 1 or len(news) != 1]
        return (
            frozenset(n for olds, _ in refused for n in olds),
            frozenset(n for _, news in refused for n in news),
        )

    def _groups(
        self,
        base: Model,
        head: Model,
        base_pool: frozenset[NodeId],
        head_pool: frozenset[NodeId],
        head_for: Mapping[NodeId, NodeId],
    ) -> dict[NameKey, _Group]:
        base_parents = containers(base)
        head_parents = containers(head)
        paired_heads = frozenset(head_for.values())
        groups: dict[NameKey, _Group] = defaultdict(lambda: ([], []))

        for node_id in sorted(base_pool):
            node = base.nodes[node_id]
            if node.kind not in PAIRED_BY_NAME:
                continue
            parent = base_parents.get(node_id)
            mapped = None if parent is None else head_for.get(parent)
            if parent is not None and mapped is None:
                continue  # the parent is unpaired
            groups[self._key(node, mapped)][0].append(node_id)

        for node_id in sorted(head_pool):
            node = head.nodes[node_id]
            if node.kind not in PAIRED_BY_NAME:
                continue
            parent = head_parents.get(node_id)
            if parent is not None and parent not in paired_heads:
                continue
            groups[self._key(node, parent)][1].append(node_id)

        return groups

    def _key(self, node: Node, parent: NodeId | None) -> NameKey:
        file, tu = self.renames.file_scope(node)
        return NameKey(node.kind, node.name, file, tu, parent)
