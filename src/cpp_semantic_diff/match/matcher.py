"""The match stage: two `Model`s -> one `Correspondence` (ARCH-001 §3, §5).

The stage is a driver over a *sequence* of tiers, even though v0 registers exactly one.
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

from collections.abc import Iterable, Mapping, Sequence
from typing import Protocol

from cpp_semantic_diff.match.correspondence import Correspondence, Match
from cpp_semantic_diff.match.declared import DeclaredTier
from cpp_semantic_diff.model import MatchTier, Model, NodeId


class Tier(Protocol):
    """One identity tier (ARCH-001 §5).

    A tier sees both whole models -- later tiers need parents, body hashes and signatures,
    not just ids -- but may only pair nodes that are still in the pools, which are the
    residue the previous tiers left. Resolving its own ambiguity is the tier's job: proposing
    two pairs that share a node is a contract violation, not something for the driver to
    break arbitrarily.
    """

    tier: MatchTier

    def propose(
        self,
        base: Model,
        head: Model,
        base_pool: frozenset[NodeId],
        head_pool: frozenset[NodeId],
    ) -> Iterable[tuple[NodeId, NodeId]]: ...


def default_tiers(renames: Mapping[str, str] | None = None) -> tuple[Tier, ...]:
    """v0 runs tier 1 and nothing else (ARCH-001 §13, STATUS §3).

    Tiers 2-4 are excluded on purpose: a rename surfaces as a remove plus an add, and how
    badly that reads on real branches is a v0 deliverable. Softening it here -- a name-based
    fallback, a similarity guess -- would destroy the measurement that decides whether the
    remaining tiers are worth building.
    """
    return (DeclaredTier(renames),)


def match_models(
    base: Model,
    head: Model,
    *,
    renames: Mapping[str, str] | None = None,
    tiers: Sequence[Tier] | None = None,
) -> Correspondence:
    """Pair base nodes with head nodes, most-confident tier first.

    `renames` is base path -> head path as resolved by git. ARCH-001 §5 requires it to be an
    input rather than something the matcher infers; it only affects entities whose USR is
    file-scoped (see `declared.usr_file`).
    """
    tiers = default_tiers(renames) if tiers is None else tiers

    base_pool = set(base.nodes)
    head_pool = set(head.nodes)
    matches: list[Match] = []

    for tier in tiers:
        # Snapshot the pools: a tier reads the residue as it stood when the tier began, so
        # its proposals cannot depend on the order the driver consumed its own output.
        for base_id, head_id in tier.propose(
            base, head, frozenset(base_pool), frozenset(head_pool)
        ):
            if base_id not in base_pool or head_id not in head_pool:
                raise ValueError(
                    f"tier {tier.tier.value!r} proposed {base_id!r} -> {head_id!r}, but one "
                    f"of them is already matched or was never in the residue; identity is "
                    f"one-to-one (ARCH-001 §5)"
                )
            base_pool.discard(base_id)
            head_pool.discard(head_id)
            matches.append(Match(base=base_id, head=head_id, tier=tier.tier))

    return Correspondence(
        # Sorted so the stage is deterministic: the same two models produce the same
        # correspondence, and therefore the same events and the same JSON, on every run.
        matches=sorted(matches, key=lambda m: (m.base, m.head)),
        unmatched_base=sorted(base_pool),
        unmatched_head=sorted(head_pool),
        tiers_run=[t.tier for t in tiers],
    )
