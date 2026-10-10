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

from collections.abc import Iterable, Iterator
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
    """ARCH-001 §5: "Name is never identity."

    The USR carries the signature, so tier 1 gets this for free -- which is exactly why it
    needs a test. The tempting "improvement" is to key on the qualified name, and it would
    pass every other test in this file while silently merging an overload set.
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

    def test_changing_a_signature_is_not_a_match(self) -> None:
        """§5: a signature change breaks tier 1, by design. It surfaces as residue rather
        than as a quiet claim that the two are the same function."""
        c = match_models(model("a", node("c:@F@f#I#")), model("b", node("c:@F@f#Id#")))
        assert not c.matches
        assert c.unmatched_base and c.unmatched_head


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
            ) -> Iterator[tuple[NodeId, NodeId]]:
                yield NodeId("c:@F@f#"), NodeId("c:@F@x#")
                yield NodeId("c:@F@f#"), NodeId("c:@F@y#")

            def ambiguous(
                self,
                base: Model,
                head: Model,
                base_pool: frozenset[NodeId],
                head_pool: frozenset[NodeId],
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
        assert c.tiers_run == [MatchTier.DECLARED]

    def test_lookups_work_in_both_directions(self) -> None:
        c = match_models(model("a", node("c:@F@f#")), model("b", node("c:@F@f#")))
        assert c.head_for(NodeId("c:@F@f#")) == "c:@F@f#"
        assert c.base_for(NodeId("c:@F@f#")) == "c:@F@f#"
        assert c.head_for(NodeId("c:@F@absent#")) is None


class TestRenamesLookBadOnPurpose:
    """ARCH-001 §13. v0 ships tier 1 only and measures the damage.

    This test exists to be *deleted* when tier 2 lands. Until then it pins the decision so
    that nobody softens it with a name-based fallback, which would destroy the measurement
    that decides whether tiers 2-4 are worth building.
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
        c = match_models(model("a", node("c:@F@compute#")), model("b", node("c:@F@calculate#")))
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
        m = node("c:@S@C@F@m#", kind=NodeKind.METHOD)
        n = node("c:@S@D@F@n#", kind=NodeKind.METHOD)
        n2 = node("c:@S@C@F@n2#", kind=NodeKind.METHOD)
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

    def test_every_leftover_has_a_reason_and_nothing_else_does(self) -> None:
        with pytest.raises(ValidationError, match="have no unpaired reason"):
            Correspondence(unmatched_base=[NodeId("a")])
        with pytest.raises(ValidationError, match="not unmatched"):
            Correspondence(unpaired_head={NodeId("x"): Unpaired.NONE})
