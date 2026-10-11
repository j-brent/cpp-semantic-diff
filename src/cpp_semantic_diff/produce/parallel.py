"""Parsing translation units in worker processes -- the other half of ARCH-001 §9.

The cache answers "do not parse what has not changed". It has nothing to say about the TUs that
*did* change, and those are what the budget is actually about: **a review touching <= 20 TUs
completes in under 30 s on a warm cache**. Measured on fmt on 2026-08-02 (v0 tracker §4,
reports/2026-08-01--v0-build/STATUS.md): a review that touched 20 sources invalidated 24 TUs
and took **55 s** serially, with seven of the host's eight cores idle throughout. The same
build over eight worker processes took **19 s**, which is the difference between meeting the
budget and missing it by a factor of two.

So this module is a third producer of the same `Iterable[TuExtract]` that `build.py`'s parse
loop and `TuCache.extracts` produce, plugged into the same seam. `assemble` cannot tell
which one ran, and that is the whole reason the seam exists.

**Processes, not threads.** Measured, not assumed: four of fmt's TUs took 10.4 s in one thread
and 8.9 s in four -- 1.16x for four cores (measured 2026-08-02, v0 tracker §5,
reports/2026-08-01--v0-build/STATUS.md). `ctypes` does drop the GIL around the call into
`index.parse`, which is where that gain comes from, but the extraction half of the work is
Python walking cursors and holds the GIL throughout, and clang's own state is not shared across
parses anyway. A thread pool buys a rounding error; a process pool buys the cores.

**Every worker parses with the verified toolchain, or refuses to parse.** A fresh process has
an unconfigured `clang.cindex`, and a `Config` that defaults to the bundled wheel library
returns *truncated ASTs without raising* (ARCH-001 §13) -- one worker silently producing
partial models is the failure this project exists to prevent, arriving through a back door.
`_configure_worker` therefore pins `CPPSD_LIBCLANG` to the library the parent already proved
and calls `toolchain.load()`, which is the only place allowed to call
`Config.set_library_file`. The probe runs once per *worker*, not once per TU, all workers in
parallel, and only on a build that has something to parse. A worker whose verified toolchain is
not the parent's is a hard failure, not a fallback -- two models built by two parsers are two
different models.

**Order in is order out.** Futures are collected by entry index, never by completion, so
`Completeness.tus` lists TUs in compile-database order exactly as the serial path does.
`assemble` sorts nodes and edges anyway, but the TU list is a record of the build, and a
record that reshuffled itself with the weather would be a diff between two runs of the same
revision.

**Nothing vanishes.** A worker that dies takes its TU's partial with it, and a dropped TU is
a model missing every declaration only that TU could see -- with no symptom, which ARCH-001
§11 calls worse than no tool at all. Every entry that goes in comes out: as a partial, or as
a `TuResult` failure naming what happened.
"""

from __future__ import annotations

import multiprocessing as mp
import os
from collections.abc import Iterable, Iterator
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path

import clang.cindex as ci

from cpp_semantic_diff.acquire.workspace import CompileEntry
from cpp_semantic_diff.errors import ClangToolchainError, SemanticDiffError
from cpp_semantic_diff.model.ir import TuResult, TuStatus
from cpp_semantic_diff.produce.cache import TuCache
from cpp_semantic_diff.produce.extract import TuExtract, extract
from cpp_semantic_diff.produce.toolchain import Toolchain, load

#: Overrides the worker count from the environment, for a machine this cannot see: a CI
#: runner that reports 32 cores and is allowed two, or a laptop where the reviewer would
#: rather keep a core. `1` is the serial path, unchanged.
WORKERS_ENV = "CPPSD_WORKERS"

#: A ceiling on the *default*, never on an explicit request. Each worker holds one translation
#: unit's AST, which for fmt's larger TUs is a few hundred megabytes, so the default has to be
#: bounded by something other than the core count on a host with many cores and ordinary
#: memory. Past this the budget is met anyway: 20 TUs over 8 workers is three rounds of
#: parsing.
MAX_DEFAULT_WORKERS = 8

#: How many times a dead pool is rebuilt before the whole build is declared a failure.
#: Each death costs one TU (below), so a handful of restarts is a handful of unparseable
#: files -- while a pool that dies repeatedly is the machine saying something systemic
#: (memory, most likely), and ARCH-001 §11 wants that stated, not absorbed.
MAX_POOL_RESTARTS = 8


def default_workers() -> int:
    """What this machine can actually run, not what it advertises.

    `sched_getaffinity` and not `cpu_count`: a container pinned to two cores of a 32-core
    host reports 32 from `cpu_count`, and 16 of those workers would swap rather than parse.
    """
    try:
        available = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):  # pragma: no cover - not Linux
        available = os.cpu_count() or 1
    return max(1, min(available, MAX_DEFAULT_WORKERS))


def worker_count(requested: int | None = None, *, tasks: int | None = None) -> int:
    """Resolve the worker count: explicit request, then environment, then the machine.

    Clamped to `tasks` because a pool larger than the work is startup cost with nothing to
    show for it -- and because a build with one TU to parse should take the same shape as a
    build with one TU, whatever the host has.
    """
    if requested is None and (override := os.environ.get(WORKERS_ENV, "").strip()):
        # A malformed override is ignored rather than fatal: it is a performance knob, and
        # refusing to run a review over a typo in an environment variable is worse than
        # running it at the default.
        requested = int(override) if override.lstrip("-").isdigit() else None
    resolved = default_workers() if requested is None else max(1, requested)
    if tasks is not None:
        resolved = max(1, min(resolved, tasks))
    return resolved


# -- the worker side -----------------------------------------------------------------

#: Per-process state, set once by the initializer. A module global rather than a task
#: argument because both are expensive to send and neither may vary between tasks: an
#: `Index` owns clang's per-process state and is reused across every TU the worker parses,
#: and the toolchain is the thing being guaranteed.
_toolchain: Toolchain | None = None
_index: ci.Index | None = None


def _configure_worker(library: str, resource_dir: str, expected_id: str) -> None:
    """Give this process the parent's verified parser, or make it unable to parse.

    The parent has already paid for discovery and the builtin-header probe (ARCH-001 §13).
    Pinning `CPPSD_LIBCLANG` hands that answer over so the worker does not re-derive it --
    `load()` takes a set override as the only candidate and skips the search -- while still
    going through `toolchain.load()`, which is the only function permitted to call
    `Config.set_library_file` and the only one that runs the probe.

    Re-probing per worker is deliberate. It costs ~0.6 s once per process against ~2 s per TU
    (measured 2026-08-02: v0 tracker §6, reports/2026-08-01--v0-build/STATUS.md, and commit
    `33681d8`), and it is what makes the guarantee a property of *this* process rather than an
    inference about how it was started. Under `fork` the parent's memoized toolchain is
    inherited and `load()` returns it immediately, so the check below is free and still true;
    under `spawn` it is the check that stops an unverified libclang from ever parsing.

    That figure was mostly `candidate_libraries()` shelling out to `llvm-config` and
    `ldconfig`; the probe parse itself is negligible. `load()` no longer searches when the
    override is set, so the pinned library is verified without being rediscovered. A pin that
    names no file is refused rather than replaced, so a library deleted between the parent's
    load and a worker's start fails the build instead of quietly swapping parsers.
    """
    global _toolchain, _index

    os.environ["CPPSD_LIBCLANG"] = library
    if resource_dir:
        os.environ["CPPSD_RESOURCE_DIR"] = resource_dir

    toolchain = load()
    if toolchain.id != expected_id or str(toolchain.library) != library:
        raise ClangToolchainError(
            f"worker resolved {toolchain.library} ({toolchain.id}), but the build was "
            f"verified against {library} ({expected_id}).",
            hint=(
                "A worker parsing with a different libclang produces partial models that "
                "belong to a different producer, and they would be merged into one model "
                "as if they agreed. Set CPPSD_WORKERS=1 to parse in this process only."
            ),
        )
    _toolchain = toolchain
    _index = ci.Index.create()


def _parse_in_worker(entry: CompileEntry, root: Path) -> TuExtract:
    """One TU, in this process, with the verified toolchain.

    Raising is reserved for what the worker cannot describe: `extract` already turns an
    unparseable TU into a `TuResult` (ARCH-001 §4.4), so anything that reaches here is the
    worker itself being broken, and the parent records it against this entry rather than
    letting it become a TU that never arrived.
    """
    if _toolchain is None or _index is None:  # pragma: no cover - initializer failed
        raise SemanticDiffError("worker was never configured with a verified toolchain")
    return extract(entry, root=root, toolchain=_toolchain, index=_index)


# -- the parent side -----------------------------------------------------------------


def tu_path(entry: CompileEntry, root: Path) -> str:
    """The path a `TuResult` names this entry by, computed without parsing it.

    `extract` derives the same string from the same rule, but only after libclang has
    returned. A worker that died returned nothing, and the failure still has to name the
    file the way every other record of that file names it -- a `Completeness` entry a
    reviewer cannot match to a source file is barely a record at all.
    """
    candidate = entry.file if entry.file.is_absolute() else entry.directory / entry.file
    try:
        return candidate.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return entry.file.name


def _failure(entry: CompileEntry, root: Path, detail: str) -> TuExtract:
    path = tu_path(entry, root)
    return TuExtract(path=path, result=TuResult(path=path, status=TuStatus.FAILED, detail=detail))


class _Pool:
    """A process pool that survives one of its processes dying.

    `ProcessPoolExecutor` breaks *whole* when a worker segfaults: every pending future is
    failed with `BrokenProcessPool`, including the ones whose TU had nothing to do with it.
    A libclang crash on one pathological file would therefore cost every TU still in
    flight, which turns one unparseable file into a model missing a dozen of them.

    So a death is not the end of the build. The pool is rebuilt and the TUs that were only
    in the blast radius are re-submitted; the one at the head of the queue is charged with
    the crash, because nothing can say which of the in-flight TUs actually caused it and
    silently retrying all of them would loop forever on a deterministic crash. Each death
    therefore costs exactly one recorded failure and makes progress.
    """

    def __init__(self, workers: int, toolchain: Toolchain) -> None:
        self.workers = workers
        self.toolchain = toolchain
        self.restarts = 0
        #: Which pool a future belongs to. Two futures can both be broken by the same death
        #: and be noticed at different times, and without a generation the second notice
        #: would tear down the healthy pool the first one just built.
        self.generation = 0
        self._executor: ProcessPoolExecutor | None = None

    def _initargs(self) -> tuple[str, str, str]:
        return (
            str(self.toolchain.library),
            str(self.toolchain.resource_dir or ""),
            self.toolchain.id,
        )

    def _start(self) -> ProcessPoolExecutor:
        # `spawn` rather than the platform default. A forked child inherits the parent's
        # already-loaded libclang, which is convenient and is exactly why it is the wrong thing
        # to rely on: the guarantee would hold by accident of start method, silently stop
        # holding wherever the default is `spawn`, and fork a process that has native library
        # state and possibly threads in it. The initializer makes the guarantee explicit and is
        # paid once per pool, not per TU.
        return ProcessPoolExecutor(
            max_workers=self.workers,
            mp_context=mp.get_context("spawn"),
            initializer=_configure_worker,
            initargs=self._initargs(),
        )

    def submit(self, entry: CompileEntry, root: Path) -> tuple[int, Future[TuExtract]]:
        # Started on the first TU that actually needs parsing, never on `open`: a fully
        # warm build serves every TU from the cache, and paying pool startup to discover
        # that would be a regression on the case ARCH-001 §9 is most proud of.
        if self._executor is None:
            self._executor = self._start()
        try:
            return self.generation, self._executor.submit(_parse_in_worker, entry, root)
        except BrokenProcessPool:
            # Submitting is where a death is usually noticed, because it is the only call
            # that talks to the pool when nothing is being awaited. Replacing here is what
            # makes the pool self-healing without a health check anywhere else.
            self.replace(self.generation)
            assert self._executor is not None
            return self.generation, self._executor.submit(_parse_in_worker, entry, root)

    def replace(self, generation: int) -> None:
        """Rebuild the pool that broke, or give up honestly (ARCH-001 §11)."""
        if generation != self.generation:
            return  # already rebuilt, by whoever noticed this death first
        self.restarts += 1
        if self.restarts > MAX_POOL_RESTARTS:
            raise SemanticDiffError(
                f"parser worker processes died {self.restarts} times; this build cannot be "
                f"completed in parallel. Re-run with CPPSD_WORKERS=1 to parse serially, "
                f"which is slower and reports each failing translation unit on its own. "
                f"Repeated worker deaths are usually memory: {self.workers} workers each "
                f"hold one translation unit's AST."
            )
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
        self._executor = self._start()
        self.generation += 1

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._executor = None


@dataclass
class _Slot:
    """One entry's place in the output, and how its partial is going to arrive.

    Slots exist because the answer is decided in a different order than it is yielded: keys
    are computed and work is dispatched as far ahead as the window allows, while the output
    stays in compile-database order.
    """

    entry: CompileEntry
    identity: str = ""
    key: str = ""
    partial: TuExtract | None = None  #: a cache hit, already in hand
    future: Future[TuExtract] | None = None  #: being parsed by a worker
    generation: int = 0  #: which pool that worker belongs to
    duplicate: bool = False  #: same key as an earlier entry in this same build


class _Producer:
    """Turns compile-database entries into partials, using the cache and a pool of parsers.

    The cache's own `extracts()` does this serially and cannot be reused here: the loop is
    the thing being replaced. Its bookkeeping is mirrored rather than approximated, because
    `TuCache.notes()` reads those counters and ARCH-001 §9 requires the pathological case --
    one widely-included header invalidating everything -- to be reported rather than
    silently endured. A parallel producer that reported nothing would take that away.
    """

    def __init__(
        self,
        *,
        root: Path,
        toolchain: Toolchain,
        cache: TuCache | None,
        workers: int,
    ) -> None:
        self.root = root
        self.cache = cache
        self.pool = _Pool(workers, toolchain)
        # How far ahead of the consumer keys are computed and work dispatched. Enough to keep
        # every worker fed while one slow TU sits at the head of the queue, and small enough
        # that the partials waiting behind it are bounded -- fmt's partials pickled to 2.6-4.3
        # MB each (commit `33681d8`, 2026-08-02), so this window is tens of megabytes and not
        # the whole model twice.
        self.window = max(workers * 2, workers + 4)
        #: Keys already claimed by an earlier entry of this build. fmt's compile database had
        #: 50 entries and 35 distinct compilations (measured 2026-08-02, v0 tracker §5,
        #: reports/2026-08-01--v0-build/STATUS.md), so 15 TUs are parsed by the serial path and
        #: then served from the entry the *same run* just wrote. Dispatching those to workers
        #: would parse them a second time and lose fifteen parses on a cold build.
        self.claimed: set[tuple[str, str]] = set()
        #: Pool generations whose death has already been charged to a TU. One death breaks
        #: every future in flight, and the next one to reach the head of the queue is as
        #: innocent as the rest: charging it too would cost two TUs for one crash.
        self.charged: set[int] = set()

    def _prepare(self, entry: CompileEntry) -> _Slot:
        """Decide where this entry's partial comes from, and start it if it needs parsing."""
        slot = _Slot(entry=entry)
        cache = self.cache
        key = None if cache is None or cache.disabled else cache.key(entry)

        if key is not None and cache is not None:
            slot.key = key
            slot.identity = cache.identity(entry)
            if (hit := cache.load(slot.identity, key)) is not None:
                cache.stats.hits += 1
                slot.partial = hit
                return slot
            if (slot.identity, key) in self.claimed:
                # Deferred, not dispatched: the earlier entry with this key is ahead of
                # this one in the output, so by the time this slot is yielded that entry
                # has been parsed *and* stored, and this one is a cache hit like any other.
                # Counted then, and not here -- the serial path records it as the hit it
                # turns out to be, and a second miss for the same compilation would report
                # an invalidation that did not happen.
                slot.duplicate = True
                return slot
            self.claimed.add((slot.identity, key))
            # Distinguishing "this TU changed" from "this TU was never cached" is what
            # makes the pathological case reportable; the probe is one `stat`.
            if cache._directory(slot.identity).is_dir():
                cache.stats.stale += 1
            else:
                cache.stats.cold += 1
        elif cache is not None and not cache.disabled:
            cache.stats.uncacheable += 1

        slot.generation, slot.future = self.pool.submit(entry, self.root)
        return slot

    def _await(self, slot: _Slot) -> TuExtract:
        """The partial for a slot that is being parsed, or the record of why there is none."""
        assert slot.future is not None
        try:
            partial = slot.future.result()
        except BrokenProcessPool:
            if slot.generation in self.charged:
                # A death some earlier TU has already paid for. This one was only in the
                # blast radius and is parsed again on a fresh pool. `_recover` usually
                # gets there first, but not when this TU reached the head before the pool
                # had marked its future broken.
                self.pool.replace(slot.generation)
                slot.generation, slot.future = self.pool.submit(slot.entry, self.root)
                return self._await(slot)
            # The pool took this TU's worker down with it. Every *other* future the death
            # broke is re-submitted to a fresh pool (`_recover`); this one is at the head
            # of the queue and is charged with the crash, so that a TU which reliably
            # crashes libclang ends the build's progress on that TU instead of being
            # retried forever. Recorded, never dropped (ARCH-001 §11).
            self.charged.add(slot.generation)
            return _failure(
                slot.entry,
                self.root,
                "the worker process parsing this translation unit died (it may have been "
                "another translation unit in the same pool that crashed it)",
            )
        except Exception as error:
            # An exception `extract` chose not to turn into a `TuResult` -- an unreadable
            # file, an OOM in the worker's Python. One TU's problem, reported as one TU's
            # problem (ARCH-001 §4.4).
            return _failure(slot.entry, self.root, f"{type(error).__name__}: {error}")

        if self.cache is not None and slot.key and partial.ok:
            # Written by the parent and by nobody else. `TuCache.save` is safe against
            # concurrent writers by construction -- unique temporary name, `os.replace` --
            # but its eviction pass globs a directory it is also writing into, and the
            # `CacheStats` it keeps live in this process anyway. Keeping writes here costs
            # nothing: the entry has already crossed the process boundary.
            self.cache.save(slot.identity, slot.key, partial)
        return partial

    def _materialize(self, slot: _Slot) -> TuExtract:
        if slot.partial is not None:
            return slot.partial
        if slot.duplicate:
            assert self.cache is not None
            if (hit := self.cache.load(slot.identity, slot.key)) is not None:
                self.cache.stats.hits += 1
                return hit
            # The entry that claimed this key did not produce a storable one: it failed to
            # parse, or the store could not be written. Parse it after all rather than
            # invent a second failure out of the first one's -- this TU is entitled to its
            # own answer, and the case is rare enough to be worth a serial stall.
            slot.generation, slot.future = self.pool.submit(slot.entry, self.root)
        return self._await(slot)

    @staticmethod
    def _ready(slot: _Slot) -> bool:
        """Can this slot be yielded without waiting for a worker?

        A cache hit is in hand, and a duplicate is one `load` away from being one. Only a
        parse that has not finished is worth reading ahead for.
        """
        if slot.partial is not None or slot.duplicate:
            return True
        return slot.future is not None and slot.future.done()

    def _recover(self, buffered: dict[int, _Slot], head: int) -> None:
        """Re-dispatch the TUs a dead pool took down with it, minus the one being charged.

        `ProcessPoolExecutor` fails every pending future the moment a worker dies, so the
        broken ones are all `done()` and answer `exception()` without blocking. Only they
        are re-submitted: a future that was already running on a newer pool must not be
        parsed twice. The head is left to be charged only if its pool's death has not been
        charged already.
        """
        for index, slot in buffered.items():
            if slot.future is None or not slot.future.done():
                continue
            if index == head and slot.generation not in self.charged:
                continue
            if isinstance(slot.future.exception(), BrokenProcessPool):
                self.pool.replace(slot.generation)
                slot.generation, slot.future = self.pool.submit(slot.entry, self.root)

    def extracts(self, entries: Iterable[CompileEntry]) -> Iterator[TuExtract]:
        """The seam `build.py` left: partials in compile-database order (ARCH-001 §9)."""
        pending = iter(entries)
        buffered: dict[int, _Slot] = {}
        prepared = 0
        emitted = 0
        exhausted = False

        try:
            while True:
                self._recover(buffered, emitted)
                # Read ahead only while the next partial to yield is not already in hand. A
                # fully warm build is every TU a cache hit, and reading a window ahead there
                # would hold twenty partials alive to hand out one -- measured slower than the
                # serial path on fmt, which is a regression on the case ARCH-001 §9 cares most
                # about. Work is dispatched when there is something to wait for, and not
                # otherwise.
                head = buffered.get(emitted)
                while (
                    not exhausted
                    and len(buffered) < self.window
                    and (head is None or not self._ready(head))
                ):
                    entry = next(pending, None)
                    if entry is None:
                        exhausted = True
                        break
                    buffered[prepared] = self._prepare(entry)
                    prepared += 1
                    head = buffered.get(emitted)

                slot = buffered.pop(emitted, None)
                if slot is None:
                    return
                emitted += 1
                yield self._materialize(slot)
        finally:
            # Reached on exhaustion, on an exception, and on a consumer that walks away
            # mid-stream. Workers outliving the build would hold a libclang and a TU each.
            self.pool.close()


def parallel_extracts(
    entries: Iterable[CompileEntry],
    *,
    root: Path,
    toolchain: Toolchain,
    cache: TuCache | None = None,
    workers: int,
) -> Iterator[TuExtract]:
    """Partial models for these TUs, parsed by `workers` processes, in the order given.

    The same `Iterable[TuExtract]` as `build.tu_extracts` and `TuCache.extracts`, so
    `assemble` consumes it without knowing which producer ran (ARCH-001 §9). Cached TUs
    never reach a worker: the parallelism that matters is over the invalidated set, which
    is what the budget is written in terms of.
    """
    producer = _Producer(root=root, toolchain=toolchain, cache=cache, workers=workers)
    yield from producer.extracts(entries)
