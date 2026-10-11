"""The inline-namespace tier: entities tier 1 left because an inline namespace was bumped
(ADR-044).

A library that versions its ABI with an inline namespace -- fmt's `fmt::v12` becoming
`fmt::v13` -- changes the USR of everything inside it, and of every function elsewhere whose
parameters name one of its types, because the namespace is spelled into each. Callers do not
notice: `fmt::format` still finds it, since the namespace is inline. Tier 1 sees every one of
those entities removed and an unrelated one added.

This tier undoes exactly that and nothing else. Where one inline namespace was replaced by one
other under the same paired parent, it rewrites base USRs as if the old namespace had always
been called by the new name, and then keys them exactly as tier 1 does. The rewrite is a
textual one on a string this codebase otherwise treats as opaque, so it is anchored: the old
namespace's path is replaced only where it starts a USR, never wherever it happens to occur.
And the claim that the two namespaces are one rests on what is inside them: a mapping no
descendant pairs through is withdrawn.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field

from cpp_semantic_diff.match.declared import DeclaredKey, DeclaredTier, _index
from cpp_semantic_diff.match.unpaired import containers
from cpp_semantic_diff.model import MatchTier, Model, NodeId, NodeKind
from cpp_semantic_diff.model.ir import usr_file

#: What may follow a namespace path inside a USR for the path to be the whole namespace: the
#: next component (`@`), the end of a name before its signature (`#`), the end of a template
#: argument list (`>`), or the end. Anything else -- `@N@v1` followed by `0` -- is a different,
#: longer name that merely begins the same way.
_PATH_ENDS = frozenset("@#>")

#: What may precede a namespace path inside a USR, besides the USR's own start: `$` opens a
#: type in a parameter list, `>` the template a specialization argument names. See `rewrite`.
_PATH_STARTS = frozenset("$>")


def _path(usr: str) -> str:
    """A USR's scope path: what follows `c:` and, for a file-scoped USR, its file name.

    `c:@N@lib@N@v1` -> `@N@lib@N@v1`. A member of an anonymous namespace carries its file
    name (`c:a.cpp@aN@N@v1@F@f#`) while the namespace itself does not (`c:@aN@N@v1`), so the
    path is what the two share.
    """
    file = usr_file(usr)
    return usr[2 + (len(file) if file else 0) :]


def rewrite(usr: str, paths: Mapping[str, str]) -> str:
    """`usr` with each old namespace path in `paths` replaced by its new one, anchored.

    A path is replaced only where it starts a USR -- right after `c:`, after a file-scoped
    USR's file name, after `$`, which opens a type USR embedded in a function's parameter
    list, or after `>`, which opens the template a specialization argument names
    (`#>@N@fmt@N@v11@ST>1#T@basic_string_view1t0.0#`) -- and only where it ends there too
    (`_PATH_ENDS`). Elsewhere a `>` is followed by a digit or `#` (`@ST>1#T@X`, `X>#`), so it
    never starts a path. Without `>`, every fmt template taking a `basic_string_view<Char>`
    kept its old version tag after the bump. Unanchored, `@N@lib@N@v1` would
    also match inside `other::lib::v1`, and `@N@v1` inside `lib::v10`, and pair entities that
    are not the same. The longest path wins at each anchor, so a nested bump (`v1::abi1` to
    `v2::abi2`) is not undone by its outer one.
    """
    if not usr.startswith("c:") or not paths:
        return usr
    start = len(usr) - len(_path(usr))
    anchors = [start, *(i + 1 for i, ch in enumerate(usr) if ch in _PATH_STARTS and i >= start)]
    ordered = sorted(paths.items(), key=lambda item: len(item[0]), reverse=True)
    out: list[str] = []
    done = 0
    for anchor in anchors:
        if anchor < done:
            continue
        for old, new in ordered:
            end = anchor + len(old)
            if usr.startswith(old, anchor) and (end == len(usr) or usr[end] in _PATH_ENDS):
                out += (usr[done:anchor], new)
                done = end
                break
    return "".join((*out, usr[done:]))


@dataclass
class _Plan:
    """What this tier decides over one pair of pools: its pairs, and what it refused."""

    pairs: dict[NodeId, NodeId] = field(default_factory=dict)
    refused_base: set[NodeId] = field(default_factory=set)
    refused_head: set[NodeId] = field(default_factory=set)


class InlineNamespaceTier:
    """Pairs what tier 1 left under a bumped inline namespace (ADR-044).

    The rules:

    - **One inline namespace replaced by one other.** For each parent already paired, or the
      global scope, the leftover inline namespaces under it on each side: exactly one on each
      side is a candidate mapping. More than one on either side, with any on the other, is a
      choice this tier refuses, and every one of them is reported as an ambiguous pairing. A
      plain namespace is never a candidate: renaming one breaks every caller that names it,
      which is a change worth seeing as such.
    - **Outer first.** A mapped namespace is a paired parent for the next round, so a bump
      nested inside a bump is found once its outer one is.
    - **Evidence.** A mapping stands only if some descendant of the old namespace that is not
      itself a namespace pairs through the rewrite. `inline namespace v12` removed and an
      unrelated `inline namespace literals` added are one-for-one under one parent, and
      nothing in them is the same.
    - **Then tier 1's key.** The rewritten USR is keyed exactly as tier 1 keys (including
      internal linkage and the git rename map), and only a key unique on both sides pairs. A
      key shared by two nodes on one side, and present on the other, is refused.

    Only base nodes the rewrite changes are offered, so nothing tier 1 could have paired is
    re-paired here, and the namespace pair itself falls out of the same keyed intersection.
    The namespace pair is then a `NodeRenamed` in the diff stage, and nothing inside it is a
    `NodeMoved`: the parents are paired.
    """

    tier = MatchTier.INLINE_NAMESPACE

    def __init__(self, renames: Mapping[str, str] | None = None) -> None:
        self.declared = DeclaredTier(renames)

    def propose(
        self,
        base: Model,
        head: Model,
        base_pool: frozenset[NodeId],
        head_pool: frozenset[NodeId],
        head_for: Mapping[NodeId, NodeId],
    ) -> Iterator[tuple[NodeId, NodeId]]:
        yield from self._plan(base, head, base_pool, head_pool, head_for).pairs.items()

    def ambiguous(
        self,
        base: Model,
        head: Model,
        base_pool: frozenset[NodeId],
        head_pool: frozenset[NodeId],
        head_for: Mapping[NodeId, NodeId],
    ) -> tuple[frozenset[NodeId], frozenset[NodeId]]:
        """The inline namespaces it could not choose between, and the nodes whose rewritten
        key collided with a partner on the other side (ADR-034)."""
        plan = self._plan(base, head, base_pool, head_pool, head_for)
        return frozenset(plan.refused_base), frozenset(plan.refused_head)

    def _plan(
        self,
        base: Model,
        head: Model,
        base_pool: frozenset[NodeId],
        head_pool: frozenset[NodeId],
        head_for: Mapping[NodeId, NodeId],
    ) -> _Plan:
        base_parents = containers(base)
        base_groups = _inline_children(base, base_pool, base_parents)
        head_groups = _inline_children(head, head_pool, containers(head))
        if not base_groups or not head_groups:
            return _Plan()
        head_index, head_collided = _index(head, head_pool, self.declared.key)

        # A mapping whose evidence failed is withdrawn and the whole plan is made again
        # without it, so nothing found under it -- a nested mapping, a refusal -- survives it.
        withdrawn: set[NodeId] = set()
        while True:
            plan = _Plan()
            maps: dict[NodeId, NodeId] = {}
            decided: set[NodeId | None] = set()
            paired: dict[NodeId, NodeId] = dict(head_for)
            while True:
                found = False
                for parent, olds in base_groups.items():
                    if parent in decided or withdrawn.intersection(olds):
                        continue
                    mapped = None if parent is None else paired.get(parent)
                    if parent is not None and mapped is None:
                        continue  # the parent is unpaired, so far
                    decided.add(parent)
                    news = head_groups.get(mapped, [])
                    if len(olds) == 1 and len(news) == 1:
                        maps[olds[0]] = news[0]
                        found = True
                    elif news:
                        plan.refused_base.update(olds)
                        plan.refused_head.update(news)
                if not found:
                    break
                pairs, _, _ = self._pair(base, head, base_pool, maps, head_index, head_collided)
                paired = {**head_for, **pairs}

            plan.pairs, refused_base, refused_head = self._pair(
                base, head, base_pool, maps, head_index, head_collided
            )
            if failed := set(maps) - _evidenced(base, plan.pairs, maps, base_parents):
                withdrawn |= failed
                continue
            plan.refused_base |= refused_base
            plan.refused_head |= refused_head
            return plan

    def _pair(
        self,
        base: Model,
        head: Model,
        base_pool: frozenset[NodeId],
        maps: Mapping[NodeId, NodeId],
        head_index: dict[DeclaredKey, NodeId],
        head_collided: dict[DeclaredKey, list[NodeId]],
    ) -> tuple[dict[NodeId, NodeId], set[NodeId], set[NodeId]]:
        """Pairs through the rewrite, and the nodes on each side of a collided key."""
        paths = {_path(old): _path(new) for old, new in maps.items()}
        rewritten = {n: rewrite(n, paths) for n in base_pool}
        offered = [n for n, usr in rewritten.items() if usr != n]
        base_index, base_collided = _index(
            base, offered, lambda node: self.declared.key_of(rewritten[node.id], node)
        )
        pairs = {
            base_id: head_index[key] for key, base_id in base_index.items() if key in head_index
        }
        base_keys = base_index.keys() | base_collided.keys()
        head_keys = head_index.keys() | head_collided.keys()
        refused = (base_collided.keys() | head_collided.keys()) & base_keys & head_keys

        def members(
            index: dict[DeclaredKey, NodeId], collided: dict[DeclaredKey, list[NodeId]]
        ) -> set[NodeId]:
            return {n for k in refused for n in (collided[k] if k in collided else [index[k]])}

        return pairs, members(base_index, base_collided), members(head_index, head_collided)


def _inline_children(
    model: Model, pool: Iterable[NodeId], parents: Mapping[NodeId, NodeId]
) -> dict[NodeId | None, list[NodeId]]:
    """Leftover inline namespaces by parent, None for the global scope."""
    groups: dict[NodeId | None, list[NodeId]] = defaultdict(list)
    for node_id in sorted(pool):
        node = model.nodes[node_id]
        if node.kind is NodeKind.NAMESPACE and node.attrs.get("inline") is True:
            groups[parents.get(node_id)].append(node_id)
    return dict(groups)


def _evidenced(
    base: Model,
    pairs: Mapping[NodeId, NodeId],
    maps: Mapping[NodeId, NodeId],
    parents: Mapping[NodeId, NodeId],
) -> set[NodeId]:
    """The mapped base namespaces with a paired descendant that is not a namespace.

    A namespace pairing alone is not evidence: two namespaces that hold only namespaces, or
    nothing, are as easily unrelated as not.
    """
    found: set[NodeId] = set()
    for node_id in pairs:
        if base.nodes[node_id].kind is NodeKind.NAMESPACE:
            continue
        seen: set[NodeId] = set()
        ancestor = parents.get(node_id)
        while ancestor is not None and ancestor not in seen:
            seen.add(ancestor)
            if ancestor in maps:
                found.add(ancestor)
            ancestor = parents.get(ancestor)
    return found
