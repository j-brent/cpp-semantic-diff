"""The diff stage (ARCH-001 §3, §6): two models plus a correspondence, one event list out."""

from cpp_semantic_diff.diff.context import TIER_ORDER, DiffContext
from cpp_semantic_diff.diff.differ import diff_models
from cpp_semantic_diff.diff.edges import edge_events
from cpp_semantic_diff.diff.nodes import node_events

__all__ = [
    "TIER_ORDER",
    "DiffContext",
    "diff_models",
    "edge_events",
    "node_events",
]
