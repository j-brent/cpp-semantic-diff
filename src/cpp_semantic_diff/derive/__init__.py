"""The derive stage (ARCH-001 §3, ADR-040): change events in, the same events plus derived ones out.

It sits between diff and classify. Diff reads change events off the two models and may not
compute anything a model does not state; classify links events and may not create one. A
derived change event needs both: the diff's events and the two models. So it gets a stage of
its own, and the two neighbours keep their contracts.

v0 derives one kind, `CallerOfChangedSignature` (`callers.py`).
"""

from cpp_semantic_diff.derive.callers import callers_of_changed_signatures
from cpp_semantic_diff.derive.deriver import derive_events

__all__ = ["callers_of_changed_signatures", "derive_events"]
