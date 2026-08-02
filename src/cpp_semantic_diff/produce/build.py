"""A whole checkout in, one `Model` out.

`extract.py` produces one partial model per translation unit. This is the only place that
sees all of them at once, and ARCH-001 puts three jobs here for exactly that reason:

**Dedup across TUs.** `NodeId` *is* the USR, and every header is reparsed once per TU that
includes it, so the same entity arrives once per includer -- fmt extracts 134,366 nodes for
10,574 distinct entities. Merging is `extract.merge_node`, the same function the extractor
uses within a TU, because "a declaration and its definition are one entity" has to mean the
same thing at both scales (ARCH-001 §5).

**Resolve edges against the merged node set.** A partial model has dangling edges by
construction, and some of them dangle only because the TU that declared the target was a
different one. Template instantiations are resolved here too: nothing calls a template, so
a third of fmt's call edges point at instantiation USRs that belong to no written
declaration. The extractor offers the mapping; applying it needs the merged node set,
because an *explicit* specialization is hand-written code with its own identity and must
not be collapsed into its primary template.

**Fill `Completeness` honestly.** One `TuResult` per TU including the ones that failed, the
producer that ran, and the count of what could not be resolved (ARCH-001 §4.4, §11). The
last of those is `edges_total`/`edges_dropped` -- two integers rather than a sentence,
because §10 makes the JSON the product and a consumer cannot act on prose.

Parsing dominates -- 68 % of extraction time, and a cold fmt model is ~125 s per side -- so
the parse loop and the assembly are separate functions. `assemble` consumes an *iterable of
partials* and never touches libclang or the disk, which is the seam the per-TU cache
(ARCH-001 §9) plugs into: `TuCache.extracts` is a different producer of the same
`Iterable[TuExtract]`, and nothing below this line knows which one ran.

`parallel.parallel_extracts` is the third producer through that same seam, and the one that
closes the budget. The cache removes the TUs that did not change; what remains is the
invalidated set, which is serial libclang time and nothing else -- a warm fmt review that
touched 20 sources reparsed 24 TUs and took 55 s against a 30 s budget, with seven of eight
cores idle. Over eight workers the same build is 19 s. Which producer runs is decided here
and is invisible below: `assemble` sorts, and the partials arrive in compile-database order
whatever parsed them, so the model is the same one either way.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

import clang.cindex as ci

from cpp_semantic_diff.acquire.workspace import Checkout, CompileEntry
from cpp_semantic_diff.model.ir import (
    Completeness,
    Edge,
    Model,
    Node,
    NodeId,
    TuResult,
)
from cpp_semantic_diff.produce.cache import TuCache
from cpp_semantic_diff.produce.extract import (
    TuExtract,
    extract,
    merge_edge,
    merge_node,
)
from cpp_semantic_diff.produce.parallel import parallel_extracts, worker_count
from cpp_semantic_diff.produce.toolchain import Toolchain


def tu_extracts(
    entries: Iterable[CompileEntry],
    *,
    root: Path,
    toolchain: Toolchain,
    index: ci.Index | None = None,
) -> Iterator[TuExtract]:
    """Parse each entry into a partial model, one `Index` for all of them.

    An `Index` owns clang's per-process state; creating one per TU pays for that state
    50 times over on fmt and buys nothing, since a TU is disposed as soon as its partial
    is built.

    A generator rather than a list: partials are large (fmt's TUs average 2,700 nodes
    each), `assemble` consumes each one exactly once, and streaming keeps only the edges
    alive rather than every AST-derived dictionary at once.
    """
    index = index or ci.Index.create()
    for entry in entries:
        yield extract(entry, root=root, toolchain=toolchain, index=index)


def resolve(usr: str, nodes: dict[NodeId, Node], aliases: dict[str, str]) -> NodeId | None:
    """The node an edge endpoint means, or None if this model does not contain it.

    The alias is consulted *only* when the USR is not itself a node (ARCH-001 §5). That
    ordering is the whole rule: an implicit instantiation is not a node and must resolve to
    its template, while an explicit specialization *is* a node -- fmt has 173 -- and
    collapsing it into the primary template would hide changes to hand-written code.

    Chained because instantiations nest: a method of an instantiated class template is an
    instantiation whose template is itself reached through the class. The visited set is
    for a cycle no compiler should produce and no run has shown; a hang here would be
    indistinguishable from a slow parse.
    """
    current = usr
    seen: set[str] = set()
    while current not in nodes:
        if current in seen:
            return None
        seen.add(current)
        target = aliases.get(current)
        if target is None:
            return None
        current = target
    return NodeId(current)


def assemble(
    rev: str,
    extracts: Iterable[TuExtract],
    *,
    toolchain: Toolchain,
    build_available: bool = False,
    notes: Sequence[str] = (),
) -> Model:
    """Merge partial models into the model of one revision.

    Pure: no parsing, no disk, no order dependence. Whatever produces the partials -- this
    module's parse loop today, a cache or a worker pool later -- hands them here, and the
    result is the same model for the same set.
    """
    nodes: dict[NodeId, Node] = {}
    edges: dict[str, Edge] = {}
    aliases: dict[str, str] = {}
    results: list[TuResult] = []

    # Edges cannot be resolved while nodes are still arriving: an endpoint declared by the
    # last TU is not dangling, it is early. So they are held and resolved in one pass once
    # the node set is closed.
    pending: list[Edge] = []

    for tu in extracts:
        results.append(tu.result)
        for node in tu.nodes.values():
            # A partial is not ours to modify -- merging mutates the stored node, and a
            # cached partial mutated by one build would silently change the next one
            # (ARCH-001 §9: a stale cache is confidently wrong output). Only the first
            # arrival is copied; every later one merges into a node this model owns.
            merge_node(nodes, node if node.id in nodes else node.model_copy(deep=True))
        for instantiation, template in tu.aliases.items():
            aliases.setdefault(instantiation, template)
        pending.extend(tu.edges.values())

    # Distinct dropped edges, not dropped arrivals. fmt yields 224,401 arrivals for 26,071
    # distinct edges, and a count of arrivals would report a five-figure loss against a
    # five-figure graph while both numbers mean something the other does not. The
    # denominator this pairs with is `len(unresolved) + len(edges)`, so the ratio
    # `Completeness.edge_residue` is over the same population on both sides of the bar.
    unresolved: set[str] = set()

    for edge in pending:
        src = resolve(edge.src, nodes, aliases)
        dst = resolve(edge.dst, nodes, aliases)
        if src is None or dst is None:
            # Dropped, and counted in `Completeness` rather than discarded quietly. An
            # unresolvable endpoint is not a defect to hide: measured on fmt, 3,361 of
            # these are genuinely external (`std::`, libc, builtins) and the rest are calls
            # to implicit special members that no one declared. Synthesizing nodes for
            # entities nobody wrote would put half a dozen phantoms on every class and make
            # them diffable; reporting the count says what is true (ARCH-001 §11).
            unresolved.add(edge.id)
            continue
        # Aliasing merges as well as resolves: two calls to two instantiations of one
        # template are one call to the template, which is 2,399 of fmt's edges and the
        # answer ARCH-001 §5 asks for -- an instantiation set that grows because someone
        # called a template with a new type is not a change to review. `merge_edge` also
        # swallows the src == dst this newly creates, when a template calls itself.
        merge_edge(edges, src, dst, edge.kind, edge.locations, edge.attrs)

    completeness = Completeness(
        tus_total=len(results),
        tus=results,
        # Counted, not narrated. The drop is real and large -- 5,113 of fmt's 26,071
        # distinct edges -- and it used to reach the output as one English sentence in
        # `notes`, which made every consumer either parse prose or claim a completeness it
        # did not have (ARCH-001 §10, §11). These two integers are the whole fact; whether
        # it is alarming is `Completeness.edges_closed`'s question and not this module's.
        edges_total=len(unresolved) + len(edges),
        edges_dropped=len(unresolved),
        build_available=build_available,
        # `producers` names what ran, `producer_version` is the cache-key component
        # (ARCH-001 §9, §13). Both are the release triple rather than the library's own
        # version string, so a distro rebuild that changes nothing observable does not read
        # as a different producer.
        producers=[toolchain.id],
        producer_version=toolchain.id,
        notes=list(notes),
    )

    # Sorted so that the model is a function of its content and not of the order the
    # partials happened to arrive in -- which a cache or a worker pool would change, and
    # which nothing downstream should be able to notice.
    return Model(
        rev=rev,
        nodes=dict(sorted(nodes.items())),
        edges=sorted(edges.values(), key=lambda e: e.id),
        completeness=completeness,
    )


def _noting(cache: TuCache, partials: Iterable[TuExtract], notes: list[str]) -> Iterator[TuExtract]:
    """Stream partials through, then append what the cache has to say about the build.

    The cache cannot know how much it invalidated until the last TU has gone past, and
    ARCH-001 §9 requires the pathological case -- a widely-included header invalidating
    everything -- to be reported rather than silently endured. `assemble` reads `notes`
    only after the stream is exhausted, so appending here lands them in `Completeness`
    without materializing 50 partials at once to find out.
    """
    yield from partials
    notes.extend(cache.notes())


def build_model(
    checkout: Checkout,
    *,
    toolchain: Toolchain,
    index: ci.Index | None = None,
    cache: TuCache | bool = True,
    workers: int | None = None,
) -> Model:
    """The model of one revision, from a materialized checkout and a verified parser.

    `checkout.rev` is a resolved SHA -- `acquire` resolves before it materializes, and the
    worktree cache is keyed by it -- so the model records a commit and never a ref, which
    would name different content on a later run.

    Never raises for a bad translation unit; a TU that fails is a `TuResult` the model
    carries (ARCH-001 §4.4). A broken toolchain is the opposite kind of problem and is
    refused before this runs.

    The cache is on by default and ships *with* the producer (ARCH-001 §9) -- a cold model
    is ~117 s for fmt against a 30 s budget, so a producer without one is a producer that
    misses its only stated performance requirement. `cache=False` forces every TU to be
    parsed, which is what a measurement of the cold path needs.

    `workers` defaults to what the machine can run (`$CPPSD_WORKERS` overrides it, and 1 is
    the serial path this function had before). The cache decides *what* gets parsed; the
    workers decide how fast the remainder goes, and neither is much use without the other:
    parallel parsing of a fully warm build has nothing to parse, and a cached build with 20
    invalidated TUs is 20 serial parses. `index` applies to the serial path only -- a
    worker's `Index` belongs to the process that owns it.
    """
    entries = checkout.cpp_entries
    notes: list[str] = []

    # The database describes the build, which is wider than the v0 model: C sources,
    # assembly, and anything else CMake compiles. Excluding them is correct, but the
    # exclusion is stated rather than implied by a count that does not add up.
    non_cpp = len(checkout.compile_db) - len(entries)
    if non_cpp:
        notes.append(
            f"{non_cpp} of {len(checkout.compile_db)} compile-database entries are not C++ "
            f"and were not modelled"
        )

    tu_cache = TuCache.open(checkout, toolchain=toolchain) if cache is True else (cache or None)

    # One worker is the serial loop, not a pool of one: no spawn, no pickling, no second
    # `Index`. It is the path every measurement is compared against and the answer to a
    # host where processes are expensive or memory is short (ARCH-001 §11).
    count = worker_count(workers, tasks=len(entries))
    if count == 1:
        partials: Iterable[TuExtract] = (
            tu_extracts(entries, root=checkout.path, toolchain=toolchain, index=index)
            if tu_cache is None
            else tu_cache.extracts(entries, index=index)
        )
    else:
        partials = parallel_extracts(
            entries,
            root=checkout.path,
            toolchain=toolchain,
            cache=tu_cache,
            workers=count,
        )
    if tu_cache is not None:
        partials = _noting(tu_cache, partials, notes)

    return assemble(
        checkout.rev,
        partials,
        toolchain=toolchain,
        # ARCH-001 §3.1 configures and never builds -- configure is seconds where a build
        # is minutes, which is what makes a compile database at both revisions affordable.
        # So no binary artifacts exist, and the evidence layer (§8) that would need them
        # must see that stated rather than assumed.
        build_available=False,
        notes=notes,
    )
