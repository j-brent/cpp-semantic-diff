"""The classify stage (ARCH-001 §3, §6.3): an event list in, cause links out.

The package is named for the pipeline stage, like `acquire`, `produce`, `match` and `diff` and
unlike the earlier draft's `delta/` (ARCH-001 §13), which grouped the events and the cause rules
into one directory. They are two stages with two contracts: the diff stage may not set `caused_by`
and this one may not create an event.
"""

from cpp_semantic_diff.classify.classifier import classify_events
from cpp_semantic_diff.classify.context import ClassifyContext
from cpp_semantic_diff.classify.rules import CAUSE_RULES, CauseRule, Locator, RuleStatus

__all__ = [
    "CAUSE_RULES",
    "CauseRule",
    "ClassifyContext",
    "Locator",
    "RuleStatus",
    "classify_events",
]
