"""Stage 2: turn a `Checkout` into a `Model` (ARCH-001 §4, §9, §12)."""

from __future__ import annotations

from cpp_semantic_diff.produce.extract import TuExtract, extract
from cpp_semantic_diff.produce.toolchain import (
    Toolchain,
    candidate_libraries,
    fatal_diagnostics,
    load,
)

__all__ = [
    "Toolchain",
    "TuExtract",
    "candidate_libraries",
    "extract",
    "fatal_diagnostics",
    "load",
]
