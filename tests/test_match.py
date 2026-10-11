"""Tier-1 identity -- ARCH-001 §5.

Correspondence is where this product succeeds or fails (§2), and its failures are silent:
a wrong pairing produces a delta that reads perfectly and describes a change nobody made.
So these tests assert the claims the design makes -- a moved entity is the same entity, an
overload set does not collapse, matching is one-to-one, a match says how it was made -- not
the mechanics of the lookup that currently satisfies them.

Models are built by hand. Nothing here needs libclang: tier 1 keys on the USR, and a USR is
a string.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import ValidationError

from cpp_semantic_diff.acquire import file_changes, resolve_revs
from cpp_semantic_diff.match import (
    Correspondence,
    Match,
    match_models,
    usr_file,
)
from cpp_semantic_diff.match.inline import rewrite
from cpp_semantic_diff.model import (
    Edge,
    EdgeKind,
    Location,
    MatchTier,
    Model,
    Node,
    NodeId,
    NodeKind,
    Unpaired,
)


def node(usr: str, name: str = "f", **kw: Any) -> Node:
    defaults = {"kind": NodeKind.FUNCTION, "name": name, "qualified_name": name}
    return Node(id=NodeId(usr), **{**defaults, **kw})


def model(rev: str, *nodes: Node) -> Model:
    m = Model(rev=rev)
    for n in nodes:
        m.add_node(n)
    return m


def pairs(c: Correspondence) -> set[tuple[str, str]]:
    return {(m.base, m.head) for m in c.matches}


class TestAMovedEntityIsTheSameEntity:
    """ARCH-001 §4.1 and §5. Location is provenance; identity is the USR.

    This is the claim the whole tool rests on. If a moved function reads as delete + add,
    the output is `git diff` with more machinery.
    """

    def test_moving_a_function_to_another_file_still_matches(self) -> None:
        base = model("a", node("c:@F@f#", location=Location(file="src/old.cpp", line=1)))
        head = model("b", node("c:@F@f#", location=Location(file="src/new/other.cpp", line=91)))
        c = match_models(base, head)
        assert pairs(c) == {("c:@F@f#", "c:@F@f#")}
        assert not c.unmatched_base and not c.unmatched_head

    def test_editing_the_body_still_matches(self) -> None:
        """Tier 1 survives body edits (§5 table) -- that is what makes a body *change* an
        event about one entity rather than a remove and an add."""
        base = model("a", node("c:@F@f#", body_hash="aaa"))
        head = model("b", node("c:@F@f#", body_hash="bbb"))
        assert pairs(match_models(base, head)) == {("c:@F@f#", "c:@F@f#")}


class TestOverloadsDoNotCollapse:
    """ARCH-001 §5: an overload set does not collapse.

    The USR carries the signature, so tier 1 keeps overloads apart for free -- which is
    exactly why it needs a test. The name tier keys on the qualified name, but only over
    what tier 1 left and only where the name is one to one (ADR-036). Keying on the name
    *first* would pass every other test in this file while silently merging an overload set.
    """

    INT = "c:@F@f#I#"
    DOUBLE = "c:@F@f#d#"

    def test_two_overloads_pair_with_their_own_counterparts(self) -> None:
        def both(rev: str) -> Model:
            return model(
                rev,
                node(self.INT, signature="void f(int)"),
                node(self.DOUBLE, signature="void f(double)"),
            )

        assert pairs(match_models(both("a"), both("b"))) == {
            (self.INT, self.INT),
            (self.DOUBLE, self.DOUBLE),
        }

    def test_a_dropped_overload_does_not_get_absorbed_by_its_sibling(self) -> None:
        base = model("a", node(self.INT), node(self.DOUBLE))
        head = model("b", node(self.INT))
        c = match_models(base, head)
        assert pairs(c) == {(self.INT, self.INT)}
        assert c.unmatched_base == [self.DOUBLE]

    def test_changing_a_signature_pairs_by_name_and_says_so(self) -> None:
        """ADR-026: a function whose parameters changed is the same function. Tier 1 cannot
        see it -- the USR changed -- so the name tier pairs it, and the match says which tier
        made it, because that tier carries more doubt than a USR (ADR-036)."""
        c = match_models(model("a", node("c:@F@f#I#")), model("b", node("c:@F@f#Id#")))
        assert c.matches == [
            Match(base=NodeId("c:@F@f#I#"), head=NodeId("c:@F@f#Id#"), tier=MatchTier.NAME)
        ]
        assert not c.unmatched_base and not c.unmatched_head

    def test_two_changed_overloads_are_left_unpaired_and_called_ambiguous(self) -> None:
        """Two old `f` and two new `f`, none of them unchanged: which is which is a guess,
        so none pairs, and the residue says the tier refused rather than found nothing."""
        base = model("a", node(self.INT), node(self.DOUBLE))
        head = model("b", node("c:@F@f#l#"), node("c:@F@f#c#"))
        c = match_models(base, head)
        assert not c.matches
        assert (
            set(c.unpaired_base.values()) == set(c.unpaired_head.values()) == {Unpaired.AMBIGUOUS}
        )

    def test_one_new_overload_beside_two_removed_ones_pairs_neither(self) -> None:
        base = model("a", node(self.INT), node(self.DOUBLE))
        head = model("b", node("c:@F@f#l#"))
        c = match_models(base, head)
        assert not c.matches
        assert c.unpaired_head == {"c:@F@f#l#": Unpaired.AMBIGUOUS}

    def test_the_one_changed_overload_of_two_pairs_with_the_one_new_one(self) -> None:
        """Accepted on purpose (ADR-036): tier 1 pairs the unchanged `f(int)`, which leaves
        exactly one old `f` and one new one."""
        base = model("a", node(self.INT), node(self.DOUBLE))
        head = model("b", node(self.INT), node("c:@F@f#l#"))
        assert pairs(match_models(base, head)) == {
            (self.INT, self.INT),
            (self.DOUBLE, "c:@F@f#l#"),
        }


class TestMatchingIsOneToOne:
    """ARCH-001 §5. Splits and merges surface as residue rather than being forced into a
    match -- confident-and-wrong is the failure mode to avoid."""

    def test_a_base_node_cannot_be_matched_twice(self) -> None:
        with pytest.raises(ValidationError, match="one-to-one"):
            Correspondence(
                matches=[
                    Match(base=NodeId("a"), head=NodeId("x"), tier=MatchTier.DECLARED),
                    Match(base=NodeId("a"), head=NodeId("y"), tier=MatchTier.DECLARED),
                ]
            )

    def test_a_head_node_cannot_be_matched_twice(self) -> None:
        with pytest.raises(ValidationError, match="one-to-one"):
            Correspondence(
                matches=[
                    Match(base=NodeId("a"), head=NodeId("x"), tier=MatchTier.DECLARED),
                    Match(base=NodeId("b"), head=NodeId("x"), tier=MatchTier.DECLARED),
                ]
            )

    def test_a_node_cannot_be_both_matched_and_residue(self) -> None:
        """The diff stage reads both lists. Overlap would report one node as changed and
        added at once."""
        with pytest.raises(ValidationError, match="both matched and unmatched"):
            Correspondence(
                matches=[Match(base=NodeId("a"), head=NodeId("x"), tier=MatchTier.DECLARED)],
                unmatched_base=[NodeId("a")],
            )

    def test_a_tier_that_reuses_a_node_is_rejected_by_the_driver(self) -> None:
        """The invariant is enforced against the tiers, not merely documented for them."""

        class Greedy:
            tier = MatchTier.SIMILARITY

            def propose(
                self,
                base: Model,
                head: Model,
                base_pool: frozenset[NodeId],
                head_pool: frozenset[NodeId],
                head_for: Mapping[NodeId, NodeId],
            ) -> Iterator[tuple[NodeId, NodeId]]:
                yield NodeId("c:@F@f#"), NodeId("c:@F@x#")
                yield NodeId("c:@F@f#"), NodeId("c:@F@y#")

            def ambiguous(
                self,
                base: Model,
                head: Model,
                base_pool: frozenset[NodeId],
                head_pool: frozenset[NodeId],
                head_for: Mapping[NodeId, NodeId],
            ) -> tuple[frozenset[NodeId], frozenset[NodeId]]:
                return frozenset(), frozenset()

        base = model("a", node("c:@F@f#"))
        head = model("b", node("c:@F@x#"), node("c:@F@y#"))
        with pytest.raises(ValueError, match="one-to-one"):
            match_models(base, head, tiers=[Greedy()])


class TestEveryMatchSaysHowItWasMade:
    """ARCH-001 §5, §11: the tier propagates into the confidence of every event derived from
    the match, so a match that cannot name its tier cannot be reported honestly."""

    def test_a_tier_1_match_is_labelled_tier_1(self) -> None:
        c = match_models(model("a", node("c:@F@f#")), model("b", node("c:@F@f#")))
        assert c.matches[0].tier is MatchTier.DECLARED
        assert c.tier_for(NodeId("c:@F@f#")) is MatchTier.DECLARED

    def test_an_unmatched_node_has_no_tier(self) -> None:
        """Which is exactly what `Confidence.tier` wants for an add or a remove."""
        c = match_models(model("a", node("c:@F@f#")), model("b"))
        assert c.tier_for(NodeId("c:@F@f#")) is None

    def test_the_correspondence_records_which_tiers_ran(self) -> None:
        """ "Unmatched" means different things depending on this. With only tier 1 it
        includes every rename (§13), and output that does not say so overstates it."""
        c = match_models(model("a"), model("b"))
        assert c.tiers_run == [MatchTier.DECLARED, MatchTier.INLINE_NAMESPACE, MatchTier.NAME]

    def test_lookups_work_in_both_directions(self) -> None:
        c = match_models(model("a", node("c:@F@f#")), model("b", node("c:@F@f#")))
        assert c.head_for(NodeId("c:@F@f#")) == "c:@F@f#"
        assert c.base_for(NodeId("c:@F@f#")) == "c:@F@f#"
        assert c.head_for(NodeId("c:@F@absent#")) is None


class TestRenamesLookBadOnPurpose:
    """ARCH-001 §13. v0 ships no rename detection and measures the damage.

    This test exists to be *deleted* when tier 2 lands. Until then it pins the decision so
    that nobody softens it with a similarity guess, which would destroy the measurement
    that decides whether tiers 2-4 are worth building. The name tier keys on the qualified
    name (ADR-036), so a renamed function is still a removal plus an addition.
    """

    def test_a_renamed_function_surfaces_as_remove_plus_add(self) -> None:
        base = model("a", node("c:@F@compute#", name="compute", body_hash="same"))
        head = model("b", node("c:@F@calculate#", name="calculate", body_hash="same"))
        c = match_models(base, head)
        assert not c.matches
        assert c.unmatched_base == ["c:@F@compute#"]
        assert c.unmatched_head == ["c:@F@calculate#"]

    def test_the_residue_is_the_measurement(self) -> None:
        """The residue is a first-class output, not leftovers -- its size is the v0
        deliverable that justifies or kills tiers 2-4."""
        base = model("a", node("c:@F@a#"), node("c:@F@b#"), node("c:@F@c#"))
        head = model("b", node("c:@F@a#"), node("c:@F@b2#"), node("c:@F@c2#"))
        c = match_models(base, head)
        assert len(c.matches) == 1
        assert len(c.unmatched_base) == len(c.unmatched_head) == 2


class TestTiersRunOverTheResidue:
    """ARCH-001 §5: tiers run most-confident first, each over what the last left.

    v0 registers one tier. The sequencing is what is expensive to retrofit, so it exists
    now, and this test is the thing that keeps it working while nothing uses it.
    """

    class Structural:
        """A stand-in for tier 2: pair on body_hash. Deliberately not shipped (§13)."""

        tier = MatchTier.STRUCTURAL

        def propose(
            self,
            base: Model,
            head: Model,
            base_pool: Iterable[NodeId],
            head_pool: Iterable[NodeId],
            head_for: Mapping[NodeId, NodeId],
        ) -> Iterator[tuple[NodeId, NodeId]]:
            by_hash = {head.nodes[h].body_hash: h for h in head_pool}
            for b in sorted(base_pool):
                if (h := by_hash.get(base.nodes[b].body_hash)) is not None:
                    yield b, h

        def ambiguous(
            self,
            base: Model,
            head: Model,
            base_pool: frozenset[NodeId],
            head_pool: frozenset[NodeId],
            head_for: Mapping[NodeId, NodeId],
        ) -> tuple[frozenset[NodeId], frozenset[NodeId]]:
            return frozenset(), frozenset()

    def test_a_later_tier_only_sees_what_tier_1_left(self) -> None:
        # `kept` matches on its USR at tier 1; the rename matches only on its body hash.
        base = model(
            "a",
            node("c:@F@kept#", body_hash="shared"),
            node("c:@F@compute#", body_hash="body"),
        )
        head = model(
            "b",
            node("c:@F@kept#", body_hash="shared"),
            node("c:@F@calculate#", body_hash="body"),
        )
        from cpp_semantic_diff.match import DeclaredTier

        c = match_models(base, head, tiers=[DeclaredTier(), self.Structural()])
        assert pairs(c) == {
            ("c:@F@kept#", "c:@F@kept#"),
            ("c:@F@compute#", "c:@F@calculate#"),
        }
        # `kept` was claimed by tier 1, so the body-hash tier never got the chance to pair
        # it with the identical hash on the other side.
        assert {m.base: m.tier for m in c.matches} == {
            "c:@F@kept#": MatchTier.DECLARED,
            "c:@F@compute#": MatchTier.STRUCTURAL,
        }
        assert c.tiers_run == [MatchTier.DECLARED, MatchTier.STRUCTURAL]


class TestTheNameTierPairsOnlyWhatItCanTell:
    """ADR-036. The name tier pairs a function or method tier 1 left over with the one
    leftover of the same kind, qualified name and scope -- and refuses everything else."""

    @staticmethod
    def contained(rev: str, parent: Node, *children: Node) -> Model:
        m = model(rev, parent, *children)
        m.edges = [Edge(src=parent.id, dst=c.id, kind=EdgeKind.CONTAINS) for c in children]
        return m

    def test_a_method_pairs_under_its_paired_class(self) -> None:
        cls = node("c:@S@C", name="C", kind=NodeKind.STRUCT)
        old = node("c:@S@C@F@m#", name="C::m", kind=NodeKind.METHOD)
        new = node("c:@S@C@F@m#1", name="C::m", kind=NodeKind.METHOD)
        c = match_models(self.contained("a", cls, old), self.contained("b", cls, new))
        assert {m.base: m.tier for m in c.matches} == {
            cls.id: MatchTier.DECLARED,
            old.id: MatchTier.NAME,
        }

    def test_nothing_pairs_under_a_parent_that_did_not(self) -> None:
        """A plain namespace renamed: `lib::v1` and `lib::v2` are different namespaces, and
        pairing their members would be a claim about the namespaces first. So this tier
        never reports a move."""
        v1 = node("c:@N@lib@N@v1", name="lib::v1", kind=NodeKind.NAMESPACE, attrs={"inline": False})
        v2 = node("c:@N@lib@N@v2", name="lib::v2", kind=NodeKind.NAMESPACE, attrs={"inline": False})
        f1 = node("c:@N@lib@N@v1@F@f#", name="lib::f")
        f2 = node("c:@N@lib@N@v2@F@f#I#", name="lib::f")
        c = match_models(self.contained("a", v1, f1), self.contained("b", v2, f2))
        assert not c.matches

    def test_a_different_kind_does_not_pair(self) -> None:
        base = model("a", node("c:@F@f#", name="f"))
        head = model("b", node("c:@S@f@F@f#", name="f", kind=NodeKind.METHOD))
        assert not match_models(base, head).matches

    def test_only_functions_and_methods_pair_by_name(self) -> None:
        """Every other kind's USR is exactly its kind, name and scope, so a leftover of
        another kind is not a signature change -- here, two unrelated unnamed records that
        happen to print alike."""
        base = model("a", node("c:@SA@S1", name="(anonymous)", kind=NodeKind.STRUCT))
        head = model("b", node("c:@SA@S2", name="(anonymous)", kind=NodeKind.STRUCT))
        assert not match_models(base, head).matches

    STATIC = "c:util.cpp@F@helper#I#"
    STATIC_CHANGED = "c:util.cpp@F@helper#I#I#"

    def test_two_same_named_statics_in_different_files_do_not_pair(self) -> None:
        """Internal linkage is identity (ARCH-001 §5): the TU tells them apart, here as it
        does in tier 1."""
        base = model("a", node(self.STATIC, name="helper", tu="a/util.cpp"))
        head = model("b", node(self.STATIC_CHANGED, name="helper", tu="b/util.cpp"))
        assert not match_models(base, head).matches

    def test_a_static_in_a_renamed_file_pairs_through_the_rename_map(self) -> None:
        base = model("a", node(self.STATIC, name="helper", tu="src/util.cpp"))
        head = model("b", node("c:tool.cpp@F@helper#I#I#", name="helper", tu="src/tool.cpp"))
        c = match_models(base, head, renames={"src/util.cpp": "src/tool.cpp"})
        assert pairs(c) == {(self.STATIC, "c:tool.cpp@F@helper#I#I#")}
        assert not match_models(base, head).matches

    def test_a_template_and_a_non_template_of_one_name_pair(self) -> None:
        """Accepted (ADR-036): the change shows as `template_parameters`, compared state."""
        base = model("a", node("c:@F@f#I#", attrs={"template_parameters": None}))
        head = model("b", node("c:@FT@>1#Tf#t0.0#v#", attrs={"template_parameters": "<class>"}))
        assert len(match_models(base, head).matches) == 1

    def test_a_same_named_method_does_not_pair_across_two_different_paired_parents(
        self,
    ) -> None:
        """ADR-036: "this tier never reports a `NodeMoved`." Classes `C` and `D` are each
        paired with themselves at tier 1. `C` loses a method named `m`; `D`, a wholly
        unrelated class, gains an unrelated method also named `m`. The qualified name
        alone cannot tell them apart -- it is identical on both sides on purpose, as it
        would be for two same-named methods of two differently-named-but-printed-alike
        scopes -- so the parent is what has to keep this from reading as `m` moving from
        `C` to `D` with a changed signature. Removing `parent` from the key, reasoning
        that the qualified name already carries scope, would pass every other test in
        this class while silently reporting a move no tier is supposed to make."""
        cls_c = node("c:@S@C", name="C", kind=NodeKind.STRUCT)
        cls_d = node("c:@S@D", name="D", kind=NodeKind.STRUCT)
        removed = node("c:@S@C@F@m#", name="m", kind=NodeKind.METHOD)
        added = node("c:@S@D@F@m#I#", name="m", kind=NodeKind.METHOD)
        base = self.contained("a", cls_c, removed)
        base.add_node(cls_d)
        head = self.contained("b", cls_d, added)
        head.add_node(cls_c)
        c = match_models(base, head)
        # `C` and `D` pair with themselves at tier 1, same id both sides; `m` does not
        # pair with anything.
        assert pairs(c) == {(cls_c.id, cls_c.id), (cls_d.id, cls_d.id)}
        # Not even counted as a candidate move: the two sit under different paired
        # parents, which the slot test in `unpaired.unpaired_reasons` also keys on.
        assert c.unpaired_base[removed.id] is Unpaired.NONE
        assert c.unpaired_head[added.id] is Unpaired.NONE

    def test_an_asymmetric_three_to_two_group_leaves_every_member_ambiguous(self) -> None:
        """ADR-036: "there is no tie-break" -- for any mismatched shape, not only the
        simplest 2-old/2-new case the rest of this file pins. A shortcut that tie-breaks
        whenever the two sides are merely unequal, instead of refusing outright, would
        still pass a 2:1 or 1:2 check; this needs a genuine N:M with N, M both over 1."""
        base = model("a", node("c:@F@f#I#"), node("c:@F@f#d#"), node("c:@F@f#l#"))
        head = model("b", node("c:@F@f#c#"), node("c:@F@f#f#"))
        c = match_models(base, head)
        assert not c.matches
        assert (
            set(c.unpaired_base.values()) == set(c.unpaired_head.values()) == {Unpaired.AMBIGUOUS}
        )


def ns(usr: str, inline: bool = True) -> Node:
    return node(usr, name=usr.rsplit("@", 1)[-1], kind=NodeKind.NAMESPACE, attrs={"inline": inline})


type Member = tuple[Node, Node | None]


def tree(rev: str, *members: Member) -> Model:
    """A model from (node, parent) pairs, parent None at global scope."""
    m = model(rev, *(n for n, _ in members))
    m.edges = [
        Edge(src=parent.id, dst=n.id, kind=EdgeKind.CONTAINS)
        for n, parent in members
        if parent is not None
    ]
    return m


class TestTheInlineNamespaceTierPairsAcrossABump:
    """ADR-044. Where one inline namespace was replaced by one other under the same paired
    parent, what tier 1 left inside it pairs through an anchored rewrite of the old
    namespace's path, and only if something inside actually pairs."""

    LIB = ns("c:@N@lib", inline=False)
    V1 = ns("c:@N@lib@N@v1")
    V2 = ns("c:@N@lib@N@v2")
    F1 = node("c:@N@lib@N@v1@F@f#")
    F2 = node("c:@N@lib@N@v2@F@f#")

    def bump(self, base: Iterable[Member] = (), head: Iterable[Member] = ()) -> Correspondence:
        """`lib::v1` bumped to `lib::v2`, with `f` inside, plus `base` and `head`."""
        return match_models(
            tree("a", (self.LIB, None), (self.V1, self.LIB), (self.F1, self.V1), *base),
            tree("b", (self.LIB, None), (self.V2, self.LIB), (self.F2, self.V2), *head),
        )

    def test_the_namespace_and_its_members_pair_and_say_how(self) -> None:
        c = self.bump()
        assert {(m.base, m.head, m.tier) for m in c.matches} == {
            (self.LIB.id, self.LIB.id, MatchTier.DECLARED),
            (self.V1.id, self.V2.id, MatchTier.INLINE_NAMESPACE),
            (self.F1.id, self.F2.id, MatchTier.INLINE_NAMESPACE),
        }
        assert not c.unmatched_base and not c.unmatched_head

    def test_a_type_named_in_a_parameter_list_is_rewritten_too(self) -> None:
        """`void q(lib::S)`, outside the bumped namespace, has the namespace in its USR."""
        q1, q2 = node("c:@F@q#$@N@lib@N@v1@S@S#"), node("c:@F@q#$@N@lib@N@v2@S@S#")
        assert (q1.id, q2.id) in pairs(self.bump([(q1, None)], [(q2, None)]))

    def test_a_template_named_in_a_specialization_argument_is_rewritten_too(self) -> None:
        """fmt's shape: `show(view<C>)` spells `view` after `>`, not after `$`."""
        s1 = node("c:@N@lib@N@v1@FT@>1#Tshow#>@N@lib@N@v1@ST>1#T@view1t0.0#v#", name="show")
        s2 = node("c:@N@lib@N@v2@FT@>1#Tshow#>@N@lib@N@v2@ST>1#T@view1t0.0#v#", name="show")
        c = self.bump([(s1, self.V1)], [(s2, self.V2)])
        assert c.by_base[s1.id].tier is MatchTier.INLINE_NAMESPACE

    def test_a_member_with_internal_linkage_is_rewritten_after_its_file_name(self) -> None:
        h1 = node("c:a.cpp@N@lib@N@v1@F@h#", tu="a.cpp")
        h2 = node("c:a.cpp@N@lib@N@v2@F@h#", tu="a.cpp")
        assert (h1.id, h2.id) in pairs(self.bump([(h1, self.V1)], [(h2, self.V2)]))

    def test_the_path_is_replaced_only_where_it_starts_a_usr(self) -> None:
        """`other::lib::v1` contains `@N@lib@N@v1`, and is not `lib::v1`."""
        g1, g2 = (
            node("c:@N@other@N@lib@N@v1@S@G", kind=NodeKind.STRUCT),
            node("c:@N@other@N@lib@N@v2@S@G", kind=NodeKind.STRUCT),
        )
        c = self.bump([(g1, None)], [(g2, None)])
        assert g1.id in c.unmatched_base and g2.id in c.unmatched_head

    def test_the_path_is_replaced_only_where_it_ends_a_name(self) -> None:
        """`lib::v10` begins like `lib::v1`, and is a different namespace."""
        g1, g2 = (
            node("c:@N@lib@N@v10@S@G", kind=NodeKind.STRUCT),
            node("c:@N@lib@N@v20@S@G", kind=NodeKind.STRUCT),
        )
        c = self.bump([(g1, None)], [(g2, None)])
        assert g1.id in c.unmatched_base and g2.id in c.unmatched_head

    def test_a_mapping_nothing_inside_pairs_through_is_withdrawn(self) -> None:
        """`v12` removed and an unrelated `literals` added are one for one, and unrelated."""
        v12, literals = ns("c:@N@lib@N@v12"), ns("c:@N@lib@N@literals")
        a, b = node("c:@N@lib@N@v12@F@a#", name="a"), node("c:@N@lib@N@literals@F@b#", name="b")
        c = match_models(
            tree("a", (self.LIB, None), (v12, self.LIB), (a, v12)),
            tree("b", (self.LIB, None), (literals, self.LIB), (b, literals)),
        )
        assert pairs(c) == {(self.LIB.id, self.LIB.id)}

    def test_a_plain_namespace_is_never_a_candidate(self) -> None:
        p1, p2 = ns("c:@N@lib@N@v1", inline=False), ns("c:@N@lib@N@v2", inline=False)
        c = match_models(
            tree("a", (self.LIB, None), (p1, self.LIB), (self.F1, p1)),
            tree("b", (self.LIB, None), (p2, self.LIB), (self.F2, p2)),
        )
        assert pairs(c) == {(self.LIB.id, self.LIB.id)}

    def test_two_inline_namespaces_on_one_side_are_a_refused_choice(self) -> None:
        v3 = ns("c:@N@lib@N@v3")
        c = self.bump(head=[(v3, self.LIB)])
        assert pairs(c) == {(self.LIB.id, self.LIB.id)}
        assert c.unpaired_base[self.V1.id] is Unpaired.AMBIGUOUS
        assert c.unpaired_head[self.V2.id] is Unpaired.AMBIGUOUS
        assert c.unpaired_head[v3.id] is Unpaired.AMBIGUOUS

    def test_a_bump_at_global_scope_pairs(self) -> None:
        g1, g2 = ns("c:@N@v1"), ns("c:@N@v2")
        f1, f2 = node("c:@N@v1@F@f#"), node("c:@N@v2@F@f#")
        c = match_models(tree("a", (g1, None), (f1, g1)), tree("b", (g2, None), (f2, g2)))
        assert pairs(c) == {(g1.id, g2.id), (f1.id, f2.id)}

    def test_a_bump_inside_a_bump_is_found_once_the_outer_one_is(self) -> None:
        abi1, abi2 = ns("c:@N@lib@N@v1@N@abi1"), ns("c:@N@lib@N@v2@N@abi2")
        g1, g2 = node("c:@N@lib@N@v1@N@abi1@F@g#"), node("c:@N@lib@N@v2@N@abi2@F@g#")
        c = self.bump([(abi1, self.V1), (g1, abi1)], [(abi2, self.V2), (g2, abi2)])
        assert {(self.V1.id, self.V2.id), (abi1.id, abi2.id), (g1.id, g2.id)} <= pairs(c)

    def test_a_rewritten_key_two_nodes_share_is_a_refused_choice(self) -> None:
        """Two base overloads that differ only in which version of `S` they take rewrite to
        one USR. Which became the head one is a guess."""
        old_s = node("c:@N@lib@N@v1@F@g#$@N@lib@N@v1@S@S#", name="g")
        new_s = node("c:@N@lib@N@v1@F@g#$@N@lib@N@v2@S@S#", name="g")
        g2 = node("c:@N@lib@N@v2@F@g#$@N@lib@N@v2@S@S#", name="g")
        c = self.bump([(old_s, self.V1), (new_s, self.V1)], [(g2, self.V2)])
        assert (self.F1.id, self.F2.id) in pairs(c)
        assert c.unpaired_base[old_s.id] is Unpaired.AMBIGUOUS
        assert c.unpaired_base[new_s.id] is Unpaired.AMBIGUOUS
        assert c.unpaired_head[g2.id] is Unpaired.AMBIGUOUS


class TestTheRewriteIsAnchored:
    """`match.inline.rewrite` on its own: where a namespace path may be replaced."""

    PATHS: ClassVar[dict[str, str]] = {"@N@lib@N@v1": "@N@lib@N@v2"}

    @pytest.mark.parametrize(
        ("usr", "expected"),
        [
            ("c:@N@lib@N@v1", "c:@N@lib@N@v2"),
            ("c:@N@lib@N@v1@F@f#", "c:@N@lib@N@v2@F@f#"),
            ("c:@N@lib@N@v1@ST>1#T@X", "c:@N@lib@N@v2@ST>1#T@X"),
            ("c:a.cpp@N@lib@N@v1@F@h#", "c:a.cpp@N@lib@N@v2@F@h#"),
            ("c:@F@q#$@N@lib@N@v1@S@S#", "c:@F@q#$@N@lib@N@v2@S@S#"),
            (
                "c:@N@lib@N@v1@F@g#$@N@lib@N@v1@S@X>#$@N@lib@N@v1@S@S#",
                "c:@N@lib@N@v2@F@g#$@N@lib@N@v2@S@X>#$@N@lib@N@v2@S@S#",
            ),
            (
                "c:@N@lib@N@v1@FT@>1#Tshow#>@N@lib@N@v1@ST>1#T@view1t0.0#v#",
                "c:@N@lib@N@v2@FT@>1#Tshow#>@N@lib@N@v2@ST>1#T@view1t0.0#v#",
            ),
            (
                "c:@N@lib@N@v1@S@fmt>#>@N@lib@N@v1@ST>1#T@view#I",
                "c:@N@lib@N@v2@S@fmt>#>@N@lib@N@v2@ST>1#T@view#I",
            ),
            (
                "c:@F@g#>@N@other@N@lib@N@v1@ST>1#T@X1t0.0#",
                "c:@F@g#>@N@other@N@lib@N@v1@ST>1#T@X1t0.0#",
            ),
            ("c:@N@other@N@lib@N@v1@F@g#", "c:@N@other@N@lib@N@v1@F@g#"),
            ("c:@N@lib@N@v10@F@g#", "c:@N@lib@N@v10@F@g#"),
            ("c:@F@q#$@N@other@N@lib@N@v1@S@S#", "c:@F@q#$@N@other@N@lib@N@v1@S@S#"),
        ],
    )
    def test_only_a_path_that_starts_and_ends_a_scope_is_replaced(
        self, usr: str, expected: str
    ) -> None:
        assert rewrite(usr, self.PATHS) == expected

    def test_the_longest_path_wins(self) -> None:
        paths = {"@N@lib@N@v1": "@N@lib@N@v2", "@N@lib@N@v1@N@abi1": "@N@lib@N@v2@N@abi2"}
        assert rewrite("c:@N@lib@N@v1@N@abi1@F@f#", paths) == "c:@N@lib@N@v2@N@abi2@F@f#"
        assert rewrite("c:@N@lib@N@v1@N@detail@F@f#", paths) == "c:@N@lib@N@v2@N@detail@F@f#"


class TestInternalLinkage:
    """ARCH-001 §5's sharpest hazard for tier 1.

    Clang writes the source file name into the USR of anything with internal linkage, so a
    file rename changes the identity of every static and every anonymous-namespace member in
    it. §5 requires the rename to be resolved from git *before* matching -- inferring it here
    would mean deriving identity from the content whose identity is in question.
    """

    STATIC_BASE = "c:util.cpp@F@helper#"
    STATIC_HEAD = "c:tool.cpp@F@helper#"

    def test_a_file_scoped_usr_is_told_apart_from_an_external_one(self) -> None:
        assert usr_file(self.STATIC_BASE) == "util.cpp"
        assert usr_file("c:@F@f#") is None
        assert usr_file("c:@N@ns@F@f#") is None

    def test_a_file_rename_breaks_the_match_and_is_counted(self) -> None:
        """Without the git rename map the damage is real. It is *counted* rather than
        assumed, so a review that renamed nothing and still shows residue here is a bug
        report rather than a mystery."""
        base = model("a", node(self.STATIC_BASE, name="helper"))
        head = model("b", node(self.STATIC_HEAD, name="helper"))
        c = match_models(base, head)
        assert not c.matches
        assert c.internal_linkage_residue == 2

    def test_the_git_rename_map_restores_it(self) -> None:
        base = model("a", node(self.STATIC_BASE, name="helper"))
        head = model("b", node(self.STATIC_HEAD, name="helper"))
        c = match_models(base, head, renames={"src/util.cpp": "src/tool.cpp"})
        assert pairs(c) == {(self.STATIC_BASE, self.STATIC_HEAD)}
        assert c.internal_linkage_residue == 0

    def test_an_ambiguous_rename_is_left_alone(self) -> None:
        """Two renamed files sharing a basename cannot be told apart in a USR, which carries
        only the name. Rewriting either would fabricate an identity -- the worse error."""
        base = model("a", node(self.STATIC_BASE, name="helper"))
        head = model("b", node(self.STATIC_HEAD, name="helper"))
        c = match_models(
            base,
            head,
            renames={"a/util.cpp": "a/tool.cpp", "b/util.cpp": "b/other.cpp"},
        )
        assert not c.matches

    def test_a_directory_move_needs_no_rewriting(self) -> None:
        """The USR carries the file name, not the path, so moving a file between
        directories does not disturb it."""
        base = model("a", node(self.STATIC_BASE, tu="src/util.cpp"))
        head = model("b", node(self.STATIC_BASE, tu="lib/util.cpp"))
        c = match_models(base, head, renames={"src/util.cpp": "lib/util.cpp"})
        assert pairs(c) == {(self.STATIC_BASE, self.STATIC_BASE)}

    def test_two_statics_sharing_a_file_name_are_not_the_same_entity(self) -> None:
        """`static helper` in `a/util.cpp` and an unrelated one in `b/util.cpp` produce the
        *same* USR. ARCH-001 §5: identity must include the TU. Matching them would report a
        body change between two different functions."""
        base = model("a", node(self.STATIC_BASE, tu="a/util.cpp"))
        head = model("b", node(self.STATIC_BASE, tu="b/util.cpp"))
        c = match_models(base, head)
        assert not c.matches

    def test_the_tu_is_only_required_where_the_usr_is_file_scoped(self) -> None:
        """An external-linkage entity legitimately appears in many TUs, and which TU the
        producer recorded changes for reasons unrelated to the change under review."""
        base = model("a", node("c:@F@f#", tu="a.cpp"))
        head = model("b", node("c:@F@f#", tu="b.cpp"))
        assert pairs(match_models(base, head)) == {("c:@F@f#", "c:@F@f#")}

    def test_an_unset_tu_falls_back_to_the_usr(self) -> None:
        """What the producer yields today. When it starts populating `tu`, matching of
        internal-linkage entities gets stricter -- deliberately, and in the safe direction."""
        base = model("a", node(self.STATIC_BASE))
        head = model("b", node(self.STATIC_BASE))
        assert pairs(match_models(base, head)) == {(self.STATIC_BASE, self.STATIC_BASE)}

    def test_gits_answer_reaches_the_matcher(self, renamed_repo: Path) -> None:
        """The seam, which is where the defect actually lived.

        `renames=` worked and was tested from the day it was written; nothing ever produced
        the argument. So a test over the matcher alone cannot catch a regression here -- the
        assertion has to run a real `git mv` through `acquire` and into `match_models`, with
        the residue as the verdict. No libclang, so it costs milliseconds.
        """
        revs = resolve_revs(renamed_repo)
        renames = file_changes(renamed_repo, revs).renames

        base = model("a", node(self.STATIC_BASE, name="helper"))
        head = model("b", node("c:support.cpp@F@helper#", name="helper"))

        assert match_models(base, head).internal_linkage_residue == 2, (
            "the fixture must actually break tier 1, or the next assertion proves nothing"
        )
        assert match_models(base, head, renames=renames).internal_linkage_residue == 0


class TestTheResidueSaysWhyItIsUnpaired:
    """ADR-034: an unmatched entity is not, by that fact, a doubtful pairing.

    Every genuine addition and removal has no partner by definition. What makes one doubtful
    is evidence that a partner was missed -- a same-kind entity left over under the same
    parent on the other side -- or a tier that found partners and refused to choose. The
    match stage is the only stage that saw both sides' leftovers, so it records which.
    """

    @staticmethod
    def contained(rev: str, parent: Node, *children: Node) -> Model:
        m = model(rev, parent, *children)
        m.edges = [Edge(src=parent.id, dst=c.id, kind=EdgeKind.CONTAINS) for c in children]
        return m

    def test_a_plain_addition_has_no_candidate(self) -> None:
        c = match_models(model("a", node("c:@F@f#")), model("b", node("c:@F@f#"), node("c:@F@g#")))
        assert c.unpaired_head == {"c:@F@g#": Unpaired.NONE}
        assert c.unpaired_base == {}

    def test_a_rename_tier_1_cannot_see_leaves_a_candidate_on_both_sides(self) -> None:
        """Same kind, same parent (top level), one gone and one new: §5's tier-4 candidate
        set, counted. Possibly a rename, and worth a human's eye."""
        c = match_models(
            model("a", node("c:@F@compute#", name="compute")),
            model("b", node("c:@F@calculate#", name="calculate")),
        )
        assert c.unpaired_base == {"c:@F@compute#": Unpaired.CANDIDATE}
        assert c.unpaired_head == {"c:@F@calculate#": Unpaired.CANDIDATE}

    def test_a_different_kind_is_not_a_candidate(self) -> None:
        base = model("a", node("c:@F@f#"))
        head = model("b", node("c:@S@F", kind=NodeKind.STRUCT))
        c = match_models(base, head)
        assert c.unpaired_base == {"c:@F@f#": Unpaired.NONE}
        assert c.unpaired_head == {"c:@S@F": Unpaired.NONE}

    def test_a_candidate_must_share_the_parent_through_the_matches(self) -> None:
        """`m` under class `C` and `n` under class `D` are not candidates for each other,
        even though both are methods; `n2` under `C` is, because `C` matched."""
        cls = node("c:@S@C", kind=NodeKind.STRUCT)
        other = node("c:@S@D", kind=NodeKind.STRUCT)
        m = node("c:@S@C@F@m#", name="C::m", kind=NodeKind.METHOD)
        n = node("c:@S@D@F@n#", name="D::n", kind=NodeKind.METHOD)
        n2 = node("c:@S@C@F@n2#", name="C::n2", kind=NodeKind.METHOD)
        base = self.contained("a", cls, m)
        base.add_node(other)
        head = self.contained("b", other, n)
        head.add_node(cls)
        assert match_models(base, head).unpaired_base == {m.id: Unpaired.NONE}
        head.add_node(n2)
        head.edges.append(Edge(src=cls.id, dst=n2.id, kind=EdgeKind.CONTAINS))
        c = match_models(base, head)
        assert c.unpaired_base == {m.id: Unpaired.CANDIDATE}
        assert c.unpaired_head == {n.id: Unpaired.NONE, n2.id: Unpaired.CANDIDATE}

    def test_children_of_an_unmatched_parent_are_not_candidates(self) -> None:
        """Pairing them would be a claim about the parents first, and counting is not the
        place to make one."""
        old = node("c:@S@Old", kind=NodeKind.STRUCT)
        new = node("c:@S@New", kind=NodeKind.STRUCT)
        m_old = node("c:@S@Old@F@m#", kind=NodeKind.METHOD)
        m_new = node("c:@S@New@F@m#", kind=NodeKind.METHOD)
        c = match_models(self.contained("a", old, m_old), self.contained("b", new, m_new))
        assert c.unpaired_base == {old.id: Unpaired.CANDIDATE, m_old.id: Unpaired.NONE}
        assert c.unpaired_head == {new.id: Unpaired.CANDIDATE, m_new.id: Unpaired.NONE}

    #: A file rename that lands one file-scoped USR on another's key: `old.cpp`'s helper,
    #: rewritten into head coordinates, is indistinguishable from `new.cpp`'s (no TU on
    #: either, as for a static declared in a header).
    RENAMES: ClassVar[dict[str, str]] = {"src/old.cpp": "src/new.cpp"}
    OLD = "c:old.cpp@F@helper#"
    NEW = "c:new.cpp@F@helper#"

    def test_a_collision_with_a_partner_on_the_other_side_is_ambiguous(self) -> None:
        """Tier 1 drops a key two nodes share rather than pick one (`declared._index`), and
        says so: the pairing itself is in doubt, which is the most doubt there is."""
        base = model("a", node(self.OLD), node(self.NEW))
        head = model("b", node(self.NEW))
        c = match_models(base, head, renames=self.RENAMES)
        assert not c.matches
        assert c.unpaired_base == {self.NEW: Unpaired.AMBIGUOUS, self.OLD: Unpaired.AMBIGUOUS}
        assert c.unpaired_head == {self.NEW: Unpaired.AMBIGUOUS}

    def test_a_collision_with_nothing_to_choose_between_is_not_ambiguous(self) -> None:
        """Two colliding nodes that both vanished were never a choice tier 1 declined."""
        base = model("a", node(self.OLD), node(self.NEW))
        c = match_models(base, model("b"), renames=self.RENAMES)
        assert set(c.unpaired_base.values()) == {Unpaired.NONE}

    def test_a_truncated_rename_map_leaves_no_confident_residue_in_added_or_deleted_files(
        self,
    ) -> None:
        """ARCH-001 §11. Git skipped inexact rename detection, so `a.cpp -> a2.cpp` was not
        applied. The members of a class in an anonymous namespace sit under a parent that is
        itself unpaired, so the slot test says NONE -- a confident add and remove resting on a
        rename map known to be short. Every leftover in a file the diff added or deleted is
        floored at CANDIDATE; a leftover elsewhere is untouched."""

        def at(usr: str, file: str, kind: NodeKind) -> Node:
            return node(usr, kind=kind, location=Location(file=file, line=1))

        old = at("c:a.cpp@aN@S@Impl", "src/a.cpp", NodeKind.STRUCT)
        new = at("c:a2.cpp@aN@S@Impl", "src/a2.cpp", NodeKind.STRUCT)
        m_old = at("c:a.cpp@aN@S@Impl@F@run#", "src/a.cpp", NodeKind.METHOD)
        m_new = at("c:a2.cpp@aN@S@Impl@F@run#", "src/a2.cpp", NodeKind.METHOD)
        elsewhere = at("c:@F@gone#", "src/kept.cpp", NodeKind.FUNCTION)
        base = self.contained("a", old, m_old)
        base.add_node(elsewhere)
        head = self.contained("b", new, m_new)

        complete = match_models(base, head)
        assert complete.unpaired_base[m_old.id] is Unpaired.NONE, "the gap must exist"

        c = match_models(base, head, missed_rename_files={"src/a.cpp", "src/a2.cpp"})
        assert c.unpaired_base == {
            old.id: Unpaired.CANDIDATE,
            m_old.id: Unpaired.CANDIDATE,
            elsewhere.id: Unpaired.NONE,
        }
        assert c.unpaired_head == {new.id: Unpaired.CANDIDATE, m_new.id: Unpaired.CANDIDATE}

    def test_every_leftover_has_a_reason_and_nothing_else_does(self) -> None:
        with pytest.raises(ValidationError, match="have no unpaired reason"):
            Correspondence(unmatched_base=[NodeId("a")])
        with pytest.raises(ValidationError, match="not unmatched"):
            Correspondence(unpaired_head={NodeId("x"): Unpaired.NONE})
