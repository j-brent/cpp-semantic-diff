"""The match stage: two `Model`s -> one `Correspondence` (ARCH-001 §3, §5).

The stage is a driver over a *sequence* of tiers. v0 registers two: tier 1, and the name
tier for the functions and methods it leaves (ADR-036).
ARCH-001 §5 defines the scheme as tiers running "most-confident first, each tier running
only over the residue the previous tiers left unmatched", and that sequencing -- not the
tier-1 lookup -- is the part that is expensive to retrofit. Tier 1 is a keyed intersection;
if the driver did it inline, every later tier would arrive as a rewrite of this module
instead of an entry in a list.

The package is named for the pipeline stage (§3) rather than the earlier draft's `identity/`
(ARCH-001 §13), for the same reason `acquire`/`produce` are: stages are the vocabulary everything
else uses.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Protocol

from cpp_semantic_diff.match.correspondence import Correspondence, Match
from cpp_semantic_diff.match.declared import DeclaredTier
from cpp_semantic_diff.match.name import NameTier
from cpp_semantic_diff.match.unpaired import unpaired_reasons
from cpp_semantic_diff.model import MatchTier, Model, NodeId


class Tier(Protocol):
    """One identity tier (ARCH-001 §5).

    A tier sees both whole models -- later tiers need parents, body hashes and signatures,
    not just ids -- but may only pair nodes that are still in the pools, which are the
    residue the previous tiers left. Resolving its own ambiguity is the tier's job: proposing
    two pairs that share a node is a contract violation, not something for the driver to
    break arbitrarily.

    `head_for` is the pairs the earlier tiers made, base id to head id. A tier that asks
    whether two nodes sit under the same parent asks it through these, the way
    `unpaired.unpaired_reasons` does; the name tier pairs nothing under an unpaired parent.
    """

    tier: MatchTier

    def propose(
        self,
        base: Model,
        head: Model,
        base_pool: frozenset[NodeId],
        head_pool: frozenset[NodeId],
        head_for: Mapping[NodeId, NodeId],
    ) -> Iterable[tuple[NodeId, NodeId]]: ...

    def ambiguous(
        self,
        base: Model,
        head: Model,
        base_pool: frozenset[NodeId],
        head_pool: frozenset[NodeId],
        head_for: Mapping[NodeId, NodeId],
    ) -> tuple[Collection[NodeId], Collection[NodeId]]:
        """(base, head) nodes this tier found partners for but refused to pair, because it
        could not choose between them. Asked over the same pools as `propose`. Only the
        tier knows it declined a choice, and that is the most doubtful residue there is
        (ADR-034)."""
        ...


def default_tiers(renames: Mapping[str, str] | None = None) -> tuple[Tier, ...]:
    """v0 runs tier 1, then the name tier over the functions and methods it left (ARCH-001
    §5, §13).

    The name tier pairs a function with itself after its signature changed (ADR-036). It
    keys on the qualified name, so it cannot pair a rename: tiers 2-4 stay excluded, and a
    renamed entity still surfaces as a remove plus an add, which is the measurement that
    decides whether they are worth building.
    """
    return (DeclaredTier(renames), NameTier(renames))


def match_models(
    base: Model,
    head: Model,
    *,
    renames: Mapping[str, str] | None = None,
    missed_rename_files: Collection[str] = (),
    tiers: Sequence[Tier] | None = None,
) -> Correspondence:
    """Pair base nodes with head nodes, most-confident tier first.

    `renames` is base path -> head path as resolved by git. ARCH-001 §5 requires it to be an
    input rather than something the matcher infers; it only affects entities whose USR is
    file-scoped (see `declared.usr_file`).

    `missed_rename_files` is non-empty only when git's rename map is known to be short
    (`acquire.FileChanges.missed_rename_files`); see `unpaired.unpaired_reasons`.
    """
    tiers = default_tiers(renames) if tiers is None else tiers

    base_pool = set(base.nodes)
    head_pool = set(head.nodes)
    matches: list[Match] = []
    ambiguous_base: set[NodeId] = set()
    ambiguous_head: set[NodeId] = set()

    for tier in tiers:
        # Snapshot the pools and the pairs: a tier reads the residue as it stood when the
        # tier began, so its proposals cannot depend on the order the driver consumed its
        # own output.
        pools = (frozenset(base_pool), frozenset(head_pool))
        made = {m.base: m.head for m in matches}
        refused_base, refused_head = tier.ambiguous(base, head, *pools, made)
        ambiguous_base.update(refused_base)
        ambiguous_head.update(refused_head)
        for base_id, head_id in tier.propose(base, head, *pools, made):
            if base_id not in base_pool or head_id not in head_pool:
                raise ValueError(
                    f"tier {tier.tier.value!r} proposed {base_id!r} -> {head_id!r}, but one "
                    f"of them is already matched or was never in the residue; identity is "
                    f"one-to-one (ARCH-001 §5)"
                )
            base_pool.discard(base_id)
            head_pool.discard(head_id)
            matches.append(Match(base=base_id, head=head_id, tier=tier.tier))

    # A node one tier refused may still be paired by a later one; only what is left over
    # at the end is residue, and only residue gets a reason.
    unpaired_base, unpaired_head = unpaired_reasons(
        base,
        head,
        base_pool,
        head_pool,
        {m.base: m.head for m in matches},
        ambiguous_base & base_pool,
        ambiguous_head & head_pool,
        missed_rename_files,
    )
    return Correspondence(
        # Sorted so the stage is deterministic: the same two models produce the same
        # correspondence, and therefore the same events and the same JSON, on every run.
        matches=sorted(matches, key=lambda m: (m.base, m.head)),
        unmatched_base=sorted(base_pool),
        unmatched_head=sorted(head_pool),
        unpaired_base=dict(sorted(unpaired_base.items())),
        unpaired_head=dict(sorted(unpaired_head.items())),
        tiers_run=[t.tier for t in tiers],
    )
