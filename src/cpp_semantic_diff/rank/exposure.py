"""Is the subject reachable from a public header, or internal? ARCH-001 §7's second factor.

This is the factor that decides whether the ranking works on a real repository or only on the
fixtures, and the reason is a measurement rather than a theory. With its bundled googletest
left out, the model built from `fmt` is **69 % test code**: 5,918 of 8,618 nodes at
`HEAD~12`, measured 2026-10-11
(reports/2026-10-11--third-party-boundary/measurements.md). Extraction is right to include
all of it: those TUs are in the compile database, they are the project's own, and their
entities are real. But a ranking that scores by change shape alone puts a
modified test helper above a modified `fmt::format`, and a reviewer who is shown that once
will not look at the second page.

So exposure is, first and foremost, a **demotion** of code that is in the model without
being what the review is about, and only secondarily a promotion of public API. Three
classes, ordered:

    TEST < INTERNAL < PUBLIC

Third-party code is not a class here, because it is not in the model (ADR-025). Recognizing
it is not a ranking question: `acquire/third_party.py` builds the set from submodules and
`--third-party`, and extraction leaves everything in it out. What reaches this module is the
repository's own code, all of it scored.

**The heuristic is path convention plus containment, and nothing else.** That is a real
limitation and it is stated rather than hidden. The model carries `Node.location.file`
(repository-relative, forward slashes), `Node.symbol`, `Node.tu` and the edge graph. `symbol`
says an entity has linkage, not that anyone outside the library may use it. `Node.tu` is
populated now, but only where the USR is file-scoped *and* the declaration is in the TU's own
primary source. File-scoped USRs were 1.5 % of fmt's nodes and 10.3 % of leveldb's, measured
2026-08-04 (v0 tracker §3, reports/2026-08-01--v0-build/STATUS.md), and `tu` is set on a subset
of those, so it is `None` for almost every node this rule has to classify. It is a fact about
internal linkage rather than a key exposure could work from, and this rule does not read it.
Whether it should sharpen the INTERNAL/PUBLIC boundary where it is set -- an entity nobody
outside one TU can name is internal by the language, not by directory convention -- is
undecided. The `target` node kind and `includes` edges in §4.2 -- which is what would let this
be answered from the build system instead of from directory names -- are outside v0's
extraction scope (§13). Directory convention is what is left.

**Failure modes, plainly.**

- *A library that publishes from `src/` and keeps private headers under `include/internal`*
  is read backwards. The convention list below is the calibration surface; ARCH-001 §14
  already expects per-repository calibration for the tier-4 threshold, and this needs the
  same treatment for the same reason.
- *A project whose subject matter is testing* -- googletest, Catch2, a fuzzing harness --
  has its entire library demoted. The rule cannot distinguish "test code" from "code about
  tests" from a path, and analysing a test framework with this heuristic will rank
  everything flat. Catch2 survives by accident because its library lives in `src/catch2`.
- *A copied library that is not declared third-party* is scored as the repository's own
  code. Its directory name produces a hint at most (`acquire/third_party.py`); it is never
  read here.
- *An entity declared in a public header and defined only in a `.cpp`* is INTERNAL unless a
  `contains` edge lifts it, because the model has no `declares`/`defines` edges to join the
  two (§4.2, out of scope in §13). Header-only and template-heavy libraries are unaffected;
  a classically split library under-ranks its own implementation files.
- *Containment promotion runs through class-like containers only.* A namespace is reopened
  per file and the deduped node keeps one arbitrary location, so promoting through it would
  make the public-ness of `namespace fmt` depend on which header the merge happened to see
  last.

The vocabulary below is deliberately the *only* thing to edit for a repository whose layout
is unusual, and it lives here rather than with the numeric weights because it is vocabulary:
`weights.py` holds every number the stage tunes, and these are names.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath

from cpp_semantic_diff.model import Model, Node, NodeId
from cpp_semantic_diff.rank.graph import containers


class Exposure(StrEnum):
    """Ordered least- to most-exposed. `EXPOSURE_ORDER` is the comparison, not the name."""

    TEST = "test"
    INTERNAL = "internal"
    PUBLIC = "public"


#: Least to most exposed. Containment promotion takes the maximum along this order, so a
#: method of a public class is public wherever its definition was written.
EXPOSURE_ORDER: tuple[Exposure, ...] = (
    Exposure.TEST,
    Exposure.INTERNAL,
    Exposure.PUBLIC,
)

#: Path components that mean "this project's code, but not its product". Tests, examples
#: and benchmarks are first-party and worth diffing; they are not what a reviewer checks
#: first, and on fmt they are half the model.
NON_PRODUCT_SEGMENTS: frozenset[str] = frozenset(
    {
        "bench",
        "benchmark",
        "benchmarks",
        "doc",
        "docs",
        "example",
        "examples",
        "fuzz",
        "fuzzing",
        "sample",
        "samples",
        "spec",
        "test",
        "testing",
        "tests",
        "unittest",
        "unittests",
    }
)

#: Filename shapes that mean the same thing where the directory does not say so.
NON_PRODUCT_STEM_PREFIXES: tuple[str, ...] = ("test_", "test-")
NON_PRODUCT_STEM_SUFFIXES: tuple[str, ...] = (
    "_test",
    "-test",
    "_tests",
    "-tests",
    "_bench",
    "-bench",
    "_benchmark",
    "-benchmark",
    "_fuzzer",
    "-fuzzer",
)

#: Directories that conventionally hold the installed interface.
INCLUDE_ROOT_SEGMENTS: frozenset[str] = frozenset({"api", "inc", "include", "interface", "public"})

#: Directories that mean "a header, but not part of the promise". `spdlog/details` is the
#: canonical case: shipped in the include tree, documented as off-limits.
PRIVATE_SEGMENTS: frozenset[str] = frozenset(
    {"detail", "details", "impl", "internal", "priv", "private", "src"}
)

HEADER_SUFFIXES: frozenset[str] = frozenset(
    {".h", ".hh", ".hpp", ".hxx", ".h++", ".inc", ".inl", ".ipp", ".tcc"}
)


def classify_path(path: str) -> Exposure:
    """One file path to one exposure class. Order of the checks is the whole rule."""
    p = PurePosixPath(path)
    segments = {s.lower() for s in p.parts[:-1]}
    stem = p.stem.lower()
    name = p.name.lower()

    # The directory first: everything under `test/` is test code, whatever it is named.
    if segments & NON_PRODUCT_SEGMENTS:
        return Exposure.TEST
    if stem.startswith(NON_PRODUCT_STEM_PREFIXES) or stem.endswith(NON_PRODUCT_STEM_SUFFIXES):
        return Exposure.TEST

    if p.suffix.lower() not in HEADER_SUFFIXES and "." in name:
        # A translation unit. Whatever it defines, the declaration a client compiles
        # against is not in here.
        return Exposure.INTERNAL
    if segments & PRIVATE_SEGMENTS:
        return Exposure.INTERNAL
    # A header outside any private directory: either under a conventional include root, or
    # in a repository that has no include root at all and publishes from where it builds.
    # Both are public by the only evidence available.
    return Exposure.PUBLIC


def classify_node(node: Node) -> Exposure:
    """A node's own exposure, before containment promotion.

    A node with no location is INTERNAL rather than unknown. It is ordinary library code as
    far as anything here can tell, and the two alternatives are both worse: PUBLIC would let
    missing provenance manufacture importance, and a fifth `UNKNOWN` class would have to be
    given a weight anyway and would then be a third name for the same number.
    """
    if node.location is None:
        return Exposure.INTERNAL
    return classify_path(node.location.file)


def exposure_map(model: Model) -> dict[NodeId, Exposure]:
    """Every node's exposure, with class-like containers promoting their members.

    This is ARCH-001 §7's "reachable from a public header" at the one hop the v0 edge set
    supports. A method's own location is wherever its definition was written -- frequently a
    `.cpp` -- while the thing a client compiles against is the class, so a method of a
    public class is public. Walking the chain (rather than one hop) covers nested classes.
    """
    own = {node_id: classify_node(node) for node_id, node in model.nodes.items()}
    parents = containers(model, coupling_only=True)

    resolved: dict[NodeId, Exposure] = {}

    def resolve(node_id: NodeId, guard: frozenset[NodeId]) -> Exposure:
        if node_id in resolved:
            return resolved[node_id]
        result = own.get(node_id, Exposure.INTERNAL)
        parent = parents.get(node_id)
        # `guard` is not defensive tidiness: `contains` comes from the semantic parent and
        # a malformed or partially resolved model can present a cycle, which would recurse
        # until the interpreter stopped.
        if parent is not None and parent not in guard:
            result = max(result, resolve(parent, guard | {node_id}), key=EXPOSURE_ORDER.index)
        resolved[node_id] = result
        return result

    for node_id in sorted(own):
        resolve(node_id, frozenset({node_id}))
    return resolved
