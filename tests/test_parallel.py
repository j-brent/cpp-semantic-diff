"""What parallel parsing must get right, stated as claims the design makes.

The budget (ARCH-001 §9) is the reason this stage exists, and it is the one thing not
asserted here: a test that fails when a laptop is busy teaches people to ignore the suite,
and speed is measured on real fixtures rather than on four-line synthetic TUs. What is
asserted is everything a worker pool could break *without* being slow, all of it invisible
in output:

- **The model must not depend on how it was produced.** N workers and one worker are the
  same model, by fingerprint and by the `Completeness.tus` record, or two revisions of the
  same code diff non-empty because one of them was built on a busier machine.
- **Nothing may vanish.** A TU parsed in a process that dies is a model missing every
  declaration only that TU could see, with no diagnostic -- ARCH-001 §11's failure mode
  arriving through the back door. It becomes a recorded failure or it becomes an error.
- **Every worker holds the verified toolchain.** A fresh process has an unconfigured
  `clang.cindex`, and an unverified libclang returns truncated ASTs without raising
  (ARCH-001 §13). One worker doing that would leave a hole in the model shaped exactly
  like whatever it parsed.

Integration tests by necessity, like `test_build.py`: there is no useful way to fake
libclang, and a faked worker pool would test the fake.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
from collections.abc import Sequence
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import cast

import pytest

import cpp_semantic_diff.produce.parallel as parallel
from cpp_semantic_diff.acquire.third_party import (
    ThirdPartyEntry,
    ThirdPartySet,
    ThirdPartySource,
)
from cpp_semantic_diff.acquire.workspace import Checkout, CompileDatabase, CompileEntry
from cpp_semantic_diff.errors import ClangToolchainError, SemanticDiffError
from cpp_semantic_diff.model.ir import Model, TuResult, TuStatus
from cpp_semantic_diff.produce.build import build_model
from cpp_semantic_diff.produce.cache import find_scanner
from cpp_semantic_diff.produce.extract import NO_BOUNDARY, Boundary, TuExtract
from cpp_semantic_diff.produce.toolchain import (
    PROBE_ARGS,
    PROBE_SOURCE,
    Toolchain,
    fatal_diagnostics,
    load,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

REV = "9f4d6c2e1b8a7d5c3e0f2a1b4c6d8e0f13579bdf"

#: Enough workers for completion order to differ from submission order, few enough that a
#: laptop running the suite does not thrash. The claims below hold at any count above one.
WORKERS = 4


@pytest.fixture(scope="session")
def toolchain() -> Toolchain:
    try:
        return load()
    except ClangToolchainError as error:  # pragma: no cover - depends on the host
        pytest.skip(f"no usable libclang: {error}")


@pytest.fixture(autouse=True)
def _cache_root_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may write to the developer's real cache.

    Copied from `test_cache.py` and autouse for the same reason: several tests here build
    the same project twice to compare the results, and a store shared with the developer's
    working set would decide which of those builds parsed anything.
    """
    monkeypatch.setenv("CPPSD_CACHE_DIR", str(tmp_path / "cppsd-cache"))


@pytest.fixture(autouse=True)
def _no_ambient_worker_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """`CPPSD_WORKERS` in the environment would silently rewrite every count below."""
    monkeypatch.delenv(parallel.WORKERS_ENV, raising=False)


def project(tmp_path: Path, sources: dict[str, str], tus: Sequence[str]) -> Checkout:
    """A synthetic checkout: some files, and a compile database naming the TUs.

    `tus` may repeat a file, which is not a mistake: fmt's compile database has 50 entries
    for 35 distinct compilations, and what the duplicates cost is a design question.
    """
    for name, text in sources.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    database = tmp_path / "compile_commands.json"
    database.write_text(
        json.dumps(
            [
                {
                    "directory": str(tmp_path),
                    "file": tu,
                    "arguments": ["clang++", "-std=c++17", "-c", tu, "-o", f"{tu}.o"],
                }
                for tu in tus
            ]
        ),
        encoding="utf-8",
    )
    return Checkout(rev=REV, path=tmp_path, compile_db=CompileDatabase.load(database))


def named(model: Model, qualified: str) -> list[str]:
    return [i for i, n in model.nodes.items() if n.qualified_name == qualified]


def record(model: Model) -> list[dict[str, object]]:
    """The `Completeness` TU record, whole -- path, status, detail and cache_hit."""
    return [t.model_dump() for t in model.completeness.tus]


#: Three TUs over one header: the same entity arrives from three parses, which is what
#: makes merge order observable at all.
SHARED = {
    "shared.h": """
    #pragma once
    namespace app {
    struct Widget {
      int size() const;
      int width_;
    };
    int helper(int v);
    inline int twice(int v) { return helper(v) + helper(v); }
    }
    """,
    "a.cpp": '#include "shared.h"\nint from_a() { return app::twice(1); }\n',
    "b.cpp": """
    #include "shared.h"
    namespace app {
    int helper(int v) { return v + 1; }
    int Widget::size() const { return width_; }
    }
    """,
    "c.cpp": '#include "shared.h"\nint from_c() { return app::twice(2); }\n',
    "d.cpp": '#include "shared.h"\nint from_d() { return app::twice(3); }\n',
}
SHARED_TUS = ("a.cpp", "b.cpp", "c.cpp", "d.cpp")


class TestTheModelDoesNotDependOnHowItWasParsed:
    """ARCH-001 §9 assembles a model from partials that may arrive from a parse loop, a
    cache, or a pool of processes. If which one ran is observable in the model, then two
    revisions built by two producers diff non-empty over nothing, and the tool reports
    changes that are really scheduling."""

    def test_the_model_is_the_same_at_one_worker_and_at_many(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        checkout = project(tmp_path, SHARED, SHARED_TUS)
        serial = build_model(checkout, toolchain=toolchain, cache=False, workers=1)
        parallel_model = build_model(checkout, toolchain=toolchain, cache=False, workers=WORKERS)

        assert serial.fingerprint() == parallel_model.fingerprint()
        # The fingerprint is provenance-free by design, so the record of *how* the model
        # was built -- which is exactly what a worker pool changes -- is compared whole.
        assert record(serial) == record(parallel_model)
        assert list(serial.nodes) == list(parallel_model.nodes)
        assert [e.id for e in serial.edges] == [e.id for e in parallel_model.edges]
        assert serial.completeness.notes == parallel_model.completeness.notes

    def test_workers_apply_the_same_boundary(self, tmp_path: Path, toolchain: Toolchain) -> None:
        """ADR-025, ADR-037. Every layer between `build_model` and a worker defaults to no
        boundary, so a hand-off dropped anywhere on the parallel path would quietly put the
        third-party library and the untracked header back in the model."""
        (tmp_path / "vendor").mkdir()
        sources = {
            "vendor/lib.h": "namespace lib { int f(); struct Base { virtual void run(); }; }\n",
            "gen.h": "namespace gen { int value(); }\n",
            "a.cpp": '#include "vendor/lib.h"\nint from_a() { return lib::f(); }\n',
            "b.cpp": (
                '#include "vendor/lib.h"\nstruct App : lib::Base { void run() override; };\n'
            ),
            "c.cpp": '#include "gen.h"\nint from_c() { return gen::value(); }\n',
        }
        checkout = project(tmp_path, sources, ("a.cpp", "b.cpp", "c.cpp"))
        third_party = ThirdPartySet(
            entries=(
                ThirdPartyEntry(path="vendor", sources=frozenset({ThirdPartySource.DECLARED})),
            )
        )
        models = [
            build_model(
                checkout,
                toolchain=toolchain,
                cache=False,
                workers=count,
                third_party=third_party,
                tracked=frozenset({"a.cpp", "b.cpp", "c.cpp", "vendor"}),
            )
            for count in (1, 2)
        ]
        serial, parallel_model = models
        assert not named(serial, "lib::f") and not named(serial, "gen::value")
        assert serial.completeness.crossings_by_label == {"untracked": 1, "vendor": 3}
        assert list(serial.nodes) == list(parallel_model.nodes)
        assert [c.model_dump() for c in serial.crossings] == [
            c.model_dump() for c in parallel_model.crossings
        ]
        assert (
            serial.completeness.crossings_by_label == parallel_model.completeness.crossings_by_label
        )

    def test_completion_order_does_not_reach_the_completeness_record(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """`Completeness.tus` is in compile-database order, not in the order the workers
        happened to finish. The first TU here is much the most expensive, so with four
        workers it finishes last -- and the record must not show that."""
        sources = {
            **SHARED,
            "slow.cpp": "#include <regex>\n#include <map>\n#include <string>\n"
            '#include "shared.h"\n'
            'int from_slow() { std::regex r("x"); return app::twice(4); }\n',
        }
        tus = ("slow.cpp", *SHARED_TUS)
        model = build_model(
            project(tmp_path, sources, tus), toolchain=toolchain, cache=False, workers=WORKERS
        )
        assert [t.path for t in model.completeness.tus] == list(tus)

    def test_a_warm_build_is_the_same_model_however_many_workers_read_it(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """The composition that matters (ARCH-001 §9): the cache decides what is parsed,
        the pool decides how fast the rest goes, and neither may change the answer. Both
        builds below are fully warm, so `cache_hit` is part of the comparison."""
        checkout = project(tmp_path, SHARED, SHARED_TUS)
        build_model(checkout, toolchain=toolchain, workers=1)  # prime

        serial = build_model(checkout, toolchain=toolchain, workers=1)
        parallel_model = build_model(checkout, toolchain=toolchain, workers=WORKERS)
        assert serial.fingerprint() == parallel_model.fingerprint()
        assert record(serial) == record(parallel_model)


class TestNothingVanishes:
    """A dropped TU is a model missing declarations with no symptom -- ARCH-001 §11 calls
    that worse than no tool at all. Every entry that goes in comes out, as a partial or as
    a recorded failure."""

    def test_a_translation_unit_that_fails_in_a_worker_is_recorded_not_dropped(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        sources = {**SHARED, "bad.cpp": '#include "nonexistent-header.h"\nint gone();\n'}
        model = build_model(
            project(tmp_path, sources, ("a.cpp", "bad.cpp", "b.cpp")),
            toolchain=toolchain,
            cache=False,
            workers=WORKERS,
        )

        assert [t.path for t in model.completeness.tus] == ["a.cpp", "bad.cpp", "b.cpp"]
        failure = next(t for t in model.completeness.tus if t.status is TuStatus.FAILED)
        assert failure.path == "bad.cpp"
        assert failure.detail is not None and "nonexistent-header.h" in failure.detail
        assert not model.completeness.is_complete
        # The healthy TUs still contribute: one bad file is incompleteness to report, not
        # a reason to abandon the review.
        assert named(model, "app::helper")

    def test_a_worker_that_dies_becomes_a_failure_and_the_other_tus_survive(
        self, tmp_path: Path, toolchain: Toolchain, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real worker process is killed mid-build, which breaks the whole pool and fails
        every future in flight -- not just the one TU. The TU at the head of the queue is
        charged with the crash (nothing can say which of the in-flight TUs caused it) and
        everything else is re-parsed on a fresh pool. The alternative is a model that is
        silently short a file, or a review that dies over one bad translation unit."""
        monkeypatch.setattr(parallel, "_Pool", _PoolThatLosesAWorker)

        model = build_model(
            project(tmp_path, SHARED, SHARED_TUS),
            toolchain=toolchain,
            cache=False,
            workers=WORKERS,
        )

        assert [t.path for t in model.completeness.tus] == list(SHARED_TUS)
        died = next(t for t in model.completeness.tus if t.path == CRASHING_TU)
        assert died.status is TuStatus.FAILED
        assert died.detail is not None and "worker process" in died.detail
        # b.cpp defines what the header declares, and it was in flight when the pool died.
        # Re-parsed rather than lost: the model still has the definition.
        assert [t.status for t in model.completeness.tus].count(TuStatus.PARSED) == 3
        assert named(model, "app::helper")

    @pytest.mark.parametrize("marked_late", [False, True], ids=["marked", "marked-late"])
    def test_one_death_is_charged_to_one_translation_unit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marked_late: bool
    ) -> None:
        """The test above, with the timing pinned down. Which TUs a real death catches in
        flight depends on how busy the machine is, and the one behind the charged TU is
        caught only sometimes (#20). Here every TU is caught, so the TU that moves up to
        the head of the queue is an innocent casualty of a death already paid for.
        `marked_late` is the executor marking the dead pool's futures one at a time, so
        that TU's future may not even look broken yet when it reaches the head."""
        pool = _PoolWithScriptedDeaths({0: {"a.cpp", "b.cpp", "c.cpp"}}, marked_late=marked_late)
        monkeypatch.setattr(
            parallel, "_Pool", lambda workers, toolchain, boundary=NO_BOUNDARY: pool
        )

        assert scripted_build(tmp_path) == [
            ("a.cpp", TuStatus.FAILED),
            ("b.cpp", TuStatus.PARSED),
            ("c.cpp", TuStatus.PARSED),
        ]

    @pytest.mark.parametrize("marked_late", [False, True], ids=["marked", "marked-late"])
    def test_a_reparsed_translation_unit_that_kills_its_new_pool_is_charged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marked_late: bool
    ) -> None:
        """Re-parsing a casualty is not a pardon. A TU that kills the fresh pool too is a
        second death, and it is charged like the first, or a TU that reliably crashes
        libclang would be retried until the restart limit ends the build."""
        pool = _PoolWithScriptedDeaths(
            {0: {"a.cpp", "b.cpp", "c.cpp"}, 1: {"b.cpp"}}, marked_late=marked_late
        )
        monkeypatch.setattr(
            parallel, "_Pool", lambda workers, toolchain, boundary=NO_BOUNDARY: pool
        )

        assert scripted_build(tmp_path) == [
            ("a.cpp", TuStatus.FAILED),
            ("b.cpp", TuStatus.FAILED),
            ("c.cpp", TuStatus.PARSED),
        ]

    def test_a_pool_that_keeps_dying_is_a_hard_error_not_a_thin_model(
        self, toolchain: Toolchain, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each death costs one TU, so a few are survivable. A pool that dies over and over
        is the machine saying something systemic -- memory, most likely -- and ARCH-001 §11
        wants that stated with an actionable message rather than absorbed into a model that
        reports half a repository."""
        pool = parallel._Pool(WORKERS, toolchain)
        monkeypatch.setattr(pool, "_start", _NullExecutor)

        for _ in range(parallel.MAX_POOL_RESTARTS):
            pool.replace(pool.generation)
        with pytest.raises(SemanticDiffError, match="CPPSD_WORKERS=1"):
            pool.replace(pool.generation)


class TestTheVerifiedToolchainReachesEveryWorker:
    """ARCH-001 §13. The parent proves one libclang can reach the compiler's builtin headers
    before any model is built; a worker process starts with none of that. An unverified library
    does not raise -- it returns a truncated AST -- so a worker with the wrong one produces
    partial models that are quietly missing declarations, and they get merged in as if they
    agreed."""

    def test_a_worker_parses_builtin_headers(self, tmp_path: Path, toolchain: Toolchain) -> None:
        """End to end, through the same door a review comes in by. `size_t` and `va_list`
        come from the compiler's own headers, which is precisely what the wheel's bundled
        library cannot find -- a worker holding that library would report this TU as
        failed, or worse, parse it short."""
        model = build_model(
            project(
                tmp_path,
                {
                    "builtins.cpp": "#include <stddef.h>\n#include <stdarg.h>\n"
                    "struct Probe { size_t size; ptrdiff_t offset; };\n"
                    "size_t sizeof_probe() { return sizeof(Probe); }\n",
                    **SHARED,
                },
                ("builtins.cpp", "a.cpp"),
            ),
            toolchain=toolchain,
            cache=False,
            workers=WORKERS,
        )
        assert model.completeness.is_complete, [t.detail for t in model.completeness.tus]
        assert named(model, "Probe")
        assert named(model, "sizeof_probe")

    def test_the_worker_reports_the_parent_toolchain_and_a_clean_probe(
        self, toolchain: Toolchain
    ) -> None:
        """Directly, in the worker: the same library the parent verified, and a probe parse
        that is clean *in that process*. The end-to-end test above would also pass if the
        worker had silently found some other working libclang; this one would not."""
        with ProcessPoolExecutor(
            max_workers=1,
            mp_context=mp.get_context("spawn"),
            initializer=parallel._configure_worker,
            initargs=(str(toolchain.library), str(toolchain.resource_dir or ""), toolchain.id),
        ) as pool:
            report = pool.submit(_worker_report).result()

        assert report["id"] == toolchain.id
        assert report["library"] == str(toolchain.library)
        assert report["probe_failures"] == []

    def test_a_worker_that_resolves_a_different_library_refuses_to_parse(
        self, toolchain: Toolchain, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The check is a hard failure, not a fallback. Two libclangs are two producers
        (ARCH-001 §9), and partials from both merged into one model would be a model no
        parser could have produced."""
        monkeypatch.setenv("CPPSD_LIBCLANG", str(toolchain.library))
        with pytest.raises(ClangToolchainError, match="verified against"):
            parallel._configure_worker(
                str(toolchain.library),
                str(toolchain.resource_dir or ""),
                "libclang 0.0.0",
            )


class TestOneWorkerAndTheWorkerCount:
    def test_one_worker_builds_the_model_it_always_did(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """`workers=1` is the serial loop -- no pool, no pickling -- and it stays the
        reference every measurement and every claim above is stated against."""
        checkout = project(tmp_path, SHARED, SHARED_TUS)
        model = build_model(checkout, toolchain=toolchain, cache=False, workers=1)
        assert model.completeness.is_complete
        assert [t.path for t in model.completeness.tus] == list(SHARED_TUS)
        assert named(model, "app::helper")

    def test_the_count_adapts_to_the_machine_and_is_overridable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ARCH-001 §11: a knob for the host this cannot see -- a CI runner that reports 32
        cores and is allowed two."""
        assert parallel.default_workers() >= 1
        assert parallel.worker_count(None) == parallel.default_workers()
        assert parallel.worker_count(3) == 3
        assert parallel.worker_count(0) == 1

        monkeypatch.setenv(parallel.WORKERS_ENV, "2")
        assert parallel.worker_count(None) == 2
        # An explicit argument outranks the environment; a review that asked for serial
        # parsing gets serial parsing.
        assert parallel.worker_count(1) == 1
        monkeypatch.setenv(parallel.WORKERS_ENV, "not a number")
        assert parallel.worker_count(None) == parallel.default_workers()

    def test_the_count_never_exceeds_the_work(self) -> None:
        """A pool larger than the compile database is startup cost with nothing to show."""
        assert parallel.worker_count(8, tasks=3) == 3
        assert parallel.worker_count(8, tasks=0) == 1


class TestTheCacheStillDoesItsWork:
    """ARCH-001 §9. The parallelism that matters is over the invalidated set -- a hit costs a
    small fraction of a miss -- so a pool that dispatched cached TUs to workers would be
    parallelizing the part that was already free."""

    def test_a_cached_translation_unit_is_not_reparsed_by_a_worker(
        self, tmp_path: Path, toolchain: Toolchain, scanner: None
    ) -> None:
        checkout = project(tmp_path, SHARED, SHARED_TUS)
        build_model(checkout, toolchain=toolchain, workers=1)

        warm = build_model(checkout, toolchain=toolchain, workers=WORKERS)
        assert all(t.cache_hit for t in warm.completeness.tus)

    def test_a_repeated_compilation_is_parsed_once_and_recorded_twice(
        self, tmp_path: Path, toolchain: Toolchain, scanner: None
    ) -> None:
        """fmt's compile database has 50 entries for 35 distinct compilations, and on a
        cold build the serial path serves 15 of them from entries written moments earlier
        in the same run. Dispatching identical TUs to different workers would parse each of
        them twice and lose that -- while still owing `Completeness` one record per entry,
        because the database has two."""
        checkout = project(tmp_path, SHARED, ("a.cpp", "b.cpp", "a.cpp"))
        cold = build_model(checkout, toolchain=toolchain, workers=WORKERS)

        assert [t.path for t in cold.completeness.tus] == ["a.cpp", "b.cpp", "a.cpp"]
        # The first `a.cpp` was parsed; the second was served from what the first stored.
        assert [t.cache_hit for t in cold.completeness.tus] == [False, False, True]


@pytest.fixture
def scanner(toolchain: Toolchain) -> None:
    """Claims about the cache are claims about a cache that is *on*, and it is off without
    `clang-scan-deps` (ARCH-001 §9: a key without an include closure produces stale hits,
    so no key means no caching)."""
    found, reason = find_scanner(toolchain)
    if found is None:  # pragma: no cover - depends on the host
        pytest.skip(f"no usable clang-scan-deps: {reason}")


# -- helpers that have to live in a worker process ------------------------------------

#: The TU whose worker is killed in `test_a_worker_that_dies...`. First in the database, so
#: the rest of the build is in flight behind it when the pool goes down.
CRASHING_TU = "a.cpp"


def _worker_report() -> dict[str, object]:
    """Run *inside a worker*: what parser is this process holding, and is it whole?"""
    import clang.cindex as ci

    assert parallel._toolchain is not None
    index = ci.Index.create()
    tu = index.parse(
        "cppsd_probe.cpp",
        args=parallel._toolchain.parse_args(list(PROBE_ARGS)),
        unsaved_files=[("cppsd_probe.cpp", PROBE_SOURCE)],
    )
    return {
        "id": parallel._toolchain.id,
        "library": str(parallel._toolchain.library),
        "probe_failures": fatal_diagnostics(tu),
    }


def _die() -> TuExtract:
    """A worker that stops existing, the way a libclang segfault stops one.

    Typed as the task it replaces: it never returns, so the pool sees a parse that died.
    """
    os._exit(1)


class _PoolThatLosesAWorker(parallel._Pool):
    """The real pool, except that one TU's task kills the process it lands in."""

    def __init__(
        self, workers: int, toolchain: Toolchain, boundary: Boundary = NO_BOUNDARY
    ) -> None:
        super().__init__(workers, toolchain, boundary)
        self.killed = False

    def submit(self, entry: CompileEntry, root: Path) -> tuple[int, Future[TuExtract]]:
        if entry.file.name == CRASHING_TU and not self.killed:
            self.killed = True
            if self._executor is None:
                self._executor = self._start()
            return self.generation, self._executor.submit(_die)
        return super().submit(entry, root)


class _BrokenWhenAwaited(Future[TuExtract]):
    """A dead pool's future that the executor has not marked broken yet. It is marked when
    the producer waits on it, which is the latest the real executor could get to it."""

    def result(self, timeout: float | None = None) -> TuExtract:
        if not self.done():
            self.set_exception(BrokenProcessPool("a worker died"))
        return super().result(timeout)


class _PoolWithScriptedDeaths:
    """Stands in for `_Pool` with no processes: `deaths[g]` names the TUs that pool
    generation `g` fails with `BrokenProcessPool`, and every other TU parses."""

    def __init__(self, deaths: dict[int, set[str]], *, marked_late: bool) -> None:
        self.deaths = deaths
        self.marked_late = marked_late
        self.generation = 0

    def submit(self, entry: CompileEntry, root: Path) -> tuple[int, Future[TuExtract]]:
        name = entry.file.name
        future: Future[TuExtract]
        if name not in self.deaths.get(self.generation, set()):
            future = Future()
            future.set_result(
                TuExtract(path=name, result=TuResult(path=name, status=TuStatus.PARSED))
            )
        elif self.marked_late:
            future = _BrokenWhenAwaited()
        else:
            future = Future()
            future.set_exception(BrokenProcessPool("a worker died"))
        return self.generation, future

    def replace(self, generation: int) -> None:
        if generation == self.generation:
            self.generation += 1

    def close(self) -> None:
        return None


def scripted_build(root: Path) -> list[tuple[str, TuStatus]]:
    """a.cpp, b.cpp and c.cpp through the parallel producer, with `_Pool` already faked."""
    entries = [CompileEntry(root / n, root, []) for n in ("a.cpp", "b.cpp", "c.cpp")]
    # The fake pool never loads a parser, so no toolchain is needed.
    extracts = parallel.parallel_extracts(
        entries, root=root, toolchain=cast(Toolchain, None), workers=WORKERS
    )
    return [(e.result.path, e.result.status) for e in extracts]


class _NullExecutor:
    """Stands in for a pool that would cost a second to start and is never asked to work."""

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        return None
