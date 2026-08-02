"""Stage 2: turn a `Checkout` into a `Model` (ARCH-001 §4, §9, §13)."""

from __future__ import annotations

from cpp_semantic_diff.produce.build import assemble, build_model, tu_extracts
from cpp_semantic_diff.produce.cache import (
    CacheStats,
    TuCache,
    find_scanner,
    include_closures,
)
from cpp_semantic_diff.produce.extract import TuExtract, extract
from cpp_semantic_diff.produce.parallel import (
    default_workers,
    parallel_extracts,
    worker_count,
)
from cpp_semantic_diff.produce.toolchain import (
    Toolchain,
    candidate_libraries,
    fatal_diagnostics,
    load,
)

__all__ = [
    "CacheStats",
    "Toolchain",
    "TuCache",
    "TuExtract",
    "assemble",
    "build_model",
    "candidate_libraries",
    "default_workers",
    "extract",
    "fatal_diagnostics",
    "find_scanner",
    "include_closures",
    "load",
    "parallel_extracts",
    "tu_extracts",
    "worker_count",
]
