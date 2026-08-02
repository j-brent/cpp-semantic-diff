"""The rank stage (ARCH-001 §3, §7): what the reviewer should look at first.

`weights.py` is the tuning surface -- every number the stage uses is there, with the reason
it is that number. `exposure.py` holds the other calibration surface: the directory
conventions that decide what is library code and what is bundled test code.
"""

from cpp_semantic_diff.rank.components import Component, partition
from cpp_semantic_diff.rank.exposure import Exposure, classify_path, exposure_map
from cpp_semantic_diff.rank.graph import (
    BlastGraph,
    BlastResult,
    subject_endpoints,
    subject_home,
)
from cpp_semantic_diff.rank.ranker import HIGH_RISK, Ranking, rank_events
from cpp_semantic_diff.rank.score import (
    Factor,
    FactorName,
    RiskScore,
    ScoringContext,
    score_event,
)

__all__ = [
    "HIGH_RISK",
    "BlastGraph",
    "BlastResult",
    "Component",
    "Exposure",
    "Factor",
    "FactorName",
    "Ranking",
    "RiskScore",
    "ScoringContext",
    "classify_path",
    "exposure_map",
    "partition",
    "rank_events",
    "score_event",
    "subject_endpoints",
    "subject_home",
]
