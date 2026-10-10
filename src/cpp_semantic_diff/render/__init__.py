"""The render stage (ARCH-001 §3, §10): stage outputs in, one versioned JSON document out.

**JSON is the product; the terminal is a consumer.** The package is arranged around that
sentence and the arrangement is the design:

    document.py   the §10 contract, as pydantic types. The shape and its invariants.
    build.py      stage outputs -> `DiffDocument`. Assembly only; computes nothing.
    collapse.py   the §6.3 cascade tree, over event *ids*. Never touches an event.
    terminal.py   one consumer. Reads a document and nothing else.

The dependency arrow runs one way: `terminal` imports `document` and `collapse`, and
neither of them imports `terminal`. That is what makes the next view -- HTML, a PR
annotation, an editor plugin -- an addition rather than a refactor, and it is the property
§10 says the whole contract exists to protect.

Cascade collapse lives here and only here (§6.3, STATUS §8). Every earlier stage is
forbidden from it: diff says so, classify says so, and the reason both say so is that the
document must carry the complete event set for the views that choose *not* to collapse.
"""

from cpp_semantic_diff.render.build import build_document, compression_sentence
from cpp_semantic_diff.render.collapse import Collapsed, CollapseNode, collapse
from cpp_semantic_diff.render.document import (
    SCHEMA_VERSION,
    SYSTEMATIC_LIMITS,
    CompletenessBlock,
    CompletenessReport,
    DiffDocument,
    EntitySummary,
    Finding,
    Meta,
    Residue,
    Summary,
    Timings,
    TuFailure,
)
from cpp_semantic_diff.render.terminal import DEFAULT_LIMIT, render

__all__ = [
    "DEFAULT_LIMIT",
    "SCHEMA_VERSION",
    "SYSTEMATIC_LIMITS",
    "CollapseNode",
    "Collapsed",
    "CompletenessBlock",
    "CompletenessReport",
    "DiffDocument",
    "EntitySummary",
    "Finding",
    "Meta",
    "Residue",
    "Summary",
    "Timings",
    "TuFailure",
    "build_document",
    "collapse",
    "compression_sentence",
    "render",
]
