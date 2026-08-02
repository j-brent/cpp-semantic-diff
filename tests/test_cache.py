"""What the per-TU cache must get right, stated as claims the design makes.

ARCH-001 §9 says it in terms: *cache correctness is a test target, and cache-invalidation
tests are not optional*. The reason is that every failure here is invisible in the output.
A model assembled from a partial that describes yesterday's header has the right node
count, the right shape, and the wrong content -- it is confidently wrong, which §9 calls
the worst failure this system can have. A cache that misses when it should hit is merely
slow, and slow is visible.

So the tests that matter are the stale-hit ones, and each names the component of the key
it defends: source content, header content, compile flags, producer version. The
header-content test is the one that catches a key computed without an include closure --
the mistake that is easiest to make and impossible to see.

Integration tests by necessity, like `test_build.py`: there is no useful way to fake
libclang, and faking `clang-scan-deps` would test the fake. They are fast because each
project is a handful of lines with no includes beyond itself.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

import cpp_semantic_diff.produce.cache as cache_module
from cpp_semantic_diff.acquire.workspace import Checkout, CompileDatabase, cache_root
from cpp_semantic_diff.errors import PreconditionError
from cpp_semantic_diff.model.ir import Model
from cpp_semantic_diff.produce.build import build_model
from cpp_semantic_diff.produce.cache import (
    GENERATIONS,
    TuCache,
    dumps,
    find_scanner,
    loads,
)
from cpp_semantic_diff.produce.extract import extract
from cpp_semantic_diff.produce.toolchain import Toolchain, load

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

REV = "9f4d6c2e1b8a7d5c3e0f2a1b4c6d8e0f13579bdf"


@pytest.fixture(scope="session")
def toolchain() -> Toolchain:
    try:
        return load()
    except PreconditionError as error:  # pragma: no cover - depends on the host
        pytest.skip(f"no usable libclang: {error}")


@pytest.fixture(scope="session", autouse=True)
def _scanner_available(toolchain: Toolchain) -> None:
    """Every claim below is about a cache that is *on*, and it is off without a scanner."""
    scanner, reason = find_scanner(toolchain)
    if scanner is None:  # pragma: no cover - depends on the host
        pytest.skip(f"no usable clang-scan-deps: {reason}")


@pytest.fixture(autouse=True)
def _cache_root_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may write to the developer's real cache.

    Autouse and not a convention: every test here populates and invalidates entries, and
    one that reached the real cache would both corrupt a working set and read entries it
    did not write -- which is the failure mode under test.
    """
    monkeypatch.setenv("CPPSD_CACHE_DIR", str(tmp_path / "cppsd-cache"))


#: One header seen by every TU, and the sources that include it. The shape the cache is
#: for: an edit to `shared.h` must invalidate all three TUs, an edit to `a.cpp` only one.
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
}


def project(
    directory: Path,
    sources: dict[str, str],
    tus: Sequence[str],
    *,
    flags: Sequence[str] = ("-std=c++17",),
) -> Checkout:
    """A synthetic checkout: some files, and a compile database naming the TUs."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in sources.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    database = directory / "compile_commands.json"
    database.write_text(
        json.dumps(
            [
                {
                    "directory": str(directory),
                    "file": tu,
                    "arguments": ["clang++", *flags, "-c", tu, "-o", f"{tu}.o"],
                }
                for tu in tus
            ]
        ),
        encoding="utf-8",
    )
    return Checkout(rev=REV, path=directory, compile_db=CompileDatabase.load(database))


def store(tmp_path: Path) -> Path:
    return tmp_path / "tu-models"


def build(checkout: Checkout, toolchain: Toolchain, tmp_path: Path) -> tuple[Model, TuCache]:
    """Build with a cache pointed at this test's storage, and report what it did."""
    cache = TuCache.open(checkout, toolchain=toolchain, store=store(tmp_path))
    return build_model(checkout, toolchain=toolchain, cache=cache), cache


def hits(model: Model) -> set[str]:
    return {t.path for t in model.completeness.tus if t.cache_hit}


def edit(checkout: Checkout, name: str, text: str) -> None:
    (checkout.path / name).write_text(text, encoding="utf-8")


class TestAHitIsIndistinguishableFromAParse:
    def test_a_warm_build_produces_the_model_a_cold_one_did(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """The claim the whole design rests on. If a hit produced a *slightly* different
        model, the delta between a cached base and a freshly parsed head would report
        changes nobody made -- and they would look exactly like real ones."""
        checkout = project(tmp_path / "w", SHARED, ("a.cpp", "b.cpp", "c.cpp"))
        cold, _ = build(checkout, toolchain, tmp_path)
        warm, cache = build(checkout, toolchain, tmp_path)

        assert cache.stats.hits == 3
        assert cold.fingerprint() == warm.fingerprint()
        assert cold.model_dump() == _without_cache_hits(warm)

    def test_a_hit_is_recorded_in_completeness(self, tmp_path: Path, toolchain: Toolchain) -> None:
        """`Completeness` is how the model was built (ARCH-001 §4.4), and "assembled from
        four-day-old partials" is part of how it was built. Unset, the record claims every
        TU was parsed by this producer just now, which is exactly the thing a reader would
        use it to check."""
        checkout = project(tmp_path / "w", SHARED, ("a.cpp", "b.cpp", "c.cpp"))
        cold, _ = build(checkout, toolchain, tmp_path)
        assert hits(cold) == set()

        warm, _ = build(checkout, toolchain, tmp_path)
        assert hits(warm) == {"a.cpp", "b.cpp", "c.cpp"}
        assert warm.completeness.notes == cold.completeness.notes

    def test_an_entry_written_by_one_build_is_read_by_the_next(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """Entries outlive the process that wrote them -- the base model is populated by
        CI or by yesterday's review (ARCH-001 §9). Nothing may be carried in memory, so a
        second `TuCache` over the same storage is the whole test."""
        checkout = project(tmp_path / "w", SHARED, ("a.cpp",))
        build(checkout, toolchain, tmp_path)

        entries = list(store(tmp_path).rglob("*.json.gz"))
        assert len(entries) == 1
        fresh = loads(entries[0].read_bytes())
        parsed = extract(checkout.cpp_entries[0], root=checkout.path, toolchain=toolchain)
        assert _partial_dump(fresh) == _partial_dump(parsed)
        assert fresh.result.cache_hit and not parsed.result.cache_hit


class TestInvalidation:
    """One test per component of `hash(TU content, header content, flags, producer)`. Each
    one fails as a *hit* if its component is missing from the key, and a hit is silent."""

    def test_editing_a_tu_invalidates_that_tu_and_only_that_tu(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        checkout = project(tmp_path / "w", SHARED, ("a.cpp", "b.cpp", "c.cpp"))
        before, _ = build(checkout, toolchain, tmp_path)

        edit(checkout, "a.cpp", '#include "shared.h"\nint from_a() { return app::twice(7); }\n')
        after, cache = build(checkout, toolchain, tmp_path)

        assert hits(after) == {"b.cpp", "c.cpp"}
        assert cache.stats.stale == 1
        # The reparse reached the new text, rather than the old entry being served from a
        # new key -- which is what a key over a path or an mtime would do.
        assert _body_of(after, "from_a") != _body_of(before, "from_a")
        assert _body_of(after, "app::twice") == _body_of(before, "app::twice")

    def test_editing_a_header_invalidates_every_tu_that_includes_it(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """The test that catches a key computed without an include closure.

        `shared.h` is in no TU's `arguments` and in no TU's own text hash. A key built
        from the source file alone hits on all three TUs here, assembles a model of the
        *previous* header, and reports that the header did not change -- in a tool whose
        only job is noticing that it did."""
        checkout = project(tmp_path / "w", SHARED, ("a.cpp", "b.cpp", "c.cpp"))
        before, _ = build(checkout, toolchain, tmp_path)
        assert _body_of(before, "app::twice") is not None

        edit(
            checkout,
            "shared.h",
            SHARED["shared.h"].replace(
                "return helper(v) + helper(v);", "return helper(v) + helper(v) + 1;"
            ),
        )
        after, cache = build(checkout, toolchain, tmp_path)

        assert hits(after) == set(), "a header edit was invisible to the key"
        assert cache.stats.stale == 3
        assert _body_of(after, "app::twice") != _body_of(before, "app::twice")

    def test_changing_a_compile_flag_invalidates(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """One model is one preprocessor configuration (ARCH-001 §11). Two configurations
        of the same text are two different models and must not share an entry."""
        sources = {"a.cpp": "#ifdef FEATURE\nint gated() { return 1; }\n#endif\nint always();\n"}
        off = project(tmp_path / "w", sources, ("a.cpp",))
        model, _ = build(off, toolchain, tmp_path)
        assert not _named(model, "gated")

        on = project(tmp_path / "w", sources, ("a.cpp",), flags=("-std=c++17", "-DFEATURE"))
        model, cache = build(on, toolchain, tmp_path)
        assert cache.stats.hits == 0
        assert _named(model, "gated"), "a -D the parser honours did not reach the key"

    def test_a_flag_the_extractor_strips_does_not_invalidate(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """The key is the arguments the extractor *parses with* (`parse_arguments`), not
        the compile database's command. `-o` names an output that cannot exist here and
        `-Werror` is dropped because it would turn a warning into an unparseable TU;
        neither changes a single node, so neither may cost a reparse."""
        checkout = project(tmp_path / "w", SHARED, ("a.cpp",))
        build(checkout, toolchain, tmp_path)

        relinked = project(tmp_path / "w", SHARED, ("a.cpp",), flags=("-std=c++17", "-Werror"))
        # A different -o comes from `project` naming the object after the TU; -Werror is
        # the added flag. Both are dropped by `parse_arguments`.
        model, cache = build(relinked, toolchain, tmp_path)
        assert cache.stats.hits == 1
        assert hits(model) == {"a.cpp"}

    def test_a_different_producer_invalidates(self, tmp_path: Path, toolchain: Toolchain) -> None:
        """A model built by a different parser is a different model (ARCH-001 §9). LLVM 18
        and LLVM 20 disagree about USRs, attributes and canonical spellings, and a mixed
        model diffs those disagreements as source changes.

        The imposter toolchain is constructed rather than discovered, because the point is
        the key and not the parser: `TuCache.open` would refuse an LLVM 99 scanner, which
        is the *other* half of the same rule."""
        checkout = project(tmp_path / "w", SHARED, ("a.cpp",))
        _, cache = build(checkout, toolchain, tmp_path)
        assert cache.stats.stored == 1

        imposter = TuCache(
            root=checkout.path,
            toolchain=replace(toolchain, release="99.9.9"),
            store=store(tmp_path),
            closures=cache.closures,
        )
        entry = checkout.cpp_entries[0]
        assert imposter.key(entry) != cache.key(entry)
        assert imposter.identity(entry) != cache.identity(entry)
        assert imposter.load(imposter.identity(entry), imposter.key(entry) or "") is None

    def test_a_change_to_the_extraction_code_invalidates(
        self, tmp_path: Path, toolchain: Toolchain, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The producer is the parser *and* the code that reads it (ARCH-001 §9).

        `Toolchain.id` alone leaves a hole exactly the size of this project's own source:
        `extract.py` decides what becomes a node and what a signature reads as, so the same
        file, the same libclang and the same flags yield a different partial after an
        extractor change. This is not hypothetical -- extraction was corrected to stop
        leaking absolute worktree paths into `qualified_name` and to strip lazily-evaluated
        exception specifications out of `signature`, and every entry written before those
        fixes has identical inputs and wrong content.

        Patched rather than driven by editing `extract.py`, because the claim under test is
        that the digest reaches the key, not how the digest is computed."""
        checkout = project(tmp_path / "w", SHARED, ("a.cpp",))
        _, cache = build(checkout, toolchain, tmp_path)
        entry = checkout.cpp_entries[0]
        before = cache.identity(entry)

        monkeypatch.setattr(cache_module, "_producer_code_digest", lambda: "different")
        assert cache.identity(entry) != before
        assert cache.load(cache.identity(entry), cache.key(entry) or "") is None

    def test_the_key_does_not_depend_on_where_the_worktree_is(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """Worktrees are materialized per revision and named by SHA, so the same file at
        two revisions lives at two absolute paths. If the path reached the key, base and
        head would share nothing, every review would pay for a full model twice, and the
        cache would only ever hit on a rerun of the identical review -- which is the
        per-commit cache ARCH-001 §9 rejects, wearing a per-TU cache's clothes."""
        base = project(tmp_path / "worktrees" / ("a" * 40), SHARED, ("a.cpp", "b.cpp"))
        build(base, toolchain, tmp_path)

        head = project(tmp_path / "worktrees" / ("b" * 40), SHARED, ("a.cpp", "b.cpp"))
        model, cache = build(head, toolchain, tmp_path)
        assert cache.stats.hits == 2
        assert hits(model) == {"a.cpp", "b.cpp"}


class TestAHitCannotBePoisoned:
    def test_assembly_cannot_write_back_through_a_cache_hit(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """Assembly merges by mutating nodes, and merging is what a shared partial would
        be mutated by: one build's merged attributes would leak into the next build's view
        of the same TU, permanently and invisibly. Three builds over one entry, and the
        entry on disk must still describe its own TU."""
        checkout = project(tmp_path / "w", SHARED, ("a.cpp", "b.cpp", "c.cpp"))
        first, _ = build(checkout, toolchain, tmp_path)
        second, _ = build(checkout, toolchain, tmp_path)
        third, _ = build(checkout, toolchain, tmp_path)

        assert _without_cache_hits(first) == _without_cache_hits(third)
        assert second.fingerprint() == third.fingerprint()

        entry = next(e for e in checkout.cpp_entries if e.file.name == "a.cpp")
        cache = TuCache.open(checkout, toolchain=toolchain, store=store(tmp_path))
        key = cache.key(entry)
        assert key is not None
        stored = cache.load(cache.identity(entry), key)
        assert stored is not None
        parsed = extract(entry, root=checkout.path, toolchain=toolchain)
        assert _partial_dump(stored) == _partial_dump(parsed)


class TestStorage:
    def test_a_truncated_entry_is_never_read_as_a_valid_one(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """An interrupted write or a full disk must cost a reparse, not produce a partial
        model that is missing whatever had not been written yet. gzip's trailing CRC is
        what makes the difference detectable at all."""
        checkout = project(tmp_path / "w", SHARED, ("a.cpp",))
        whole, _ = build(checkout, toolchain, tmp_path)

        entry = next(store(tmp_path).rglob("*.json.gz"))
        blob = entry.read_bytes()
        entry.write_bytes(blob[: len(blob) // 2])

        model, cache = build(checkout, toolchain, tmp_path)
        assert cache.stats.hits == 0
        assert model.fingerprint() == whole.fingerprint()

    def test_a_half_written_entry_is_never_visible_under_its_key(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """Entries are renamed into place, so a concurrent reader sees the previous entry
        or this one and never a prefix of one. The observable form of that claim: no
        partial file is ever named by a key."""
        checkout = project(tmp_path / "w", SHARED, ("a.cpp", "b.cpp"))
        build(checkout, toolchain, tmp_path)
        assert not list(store(tmp_path).rglob("*.tmp"))
        for path in store(tmp_path).rglob("*.json.gz"):
            loads(path.read_bytes())  # raises if any entry is not a whole one

    def test_an_entry_from_a_future_format_is_a_miss_not_a_crash(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """The format tag is the escape hatch for changing what an entry holds. An older
        build meeting a newer entry must reparse rather than read fields that moved."""
        checkout = project(tmp_path / "w", SHARED, ("a.cpp",))
        build(checkout, toolchain, tmp_path)

        entry = next(store(tmp_path).rglob("*.json.gz"))
        payload = json.loads(gzip.decompress(entry.read_bytes()))
        payload["format"] = 999_999
        entry.write_bytes(gzip.compress(json.dumps(payload).encode()))

        with pytest.raises(ValueError):
            loads(entry.read_bytes())
        _, cache = build(checkout, toolchain, tmp_path)
        assert cache.stats.hits == 0

    def test_entries_for_one_tu_are_bounded(self, tmp_path: Path, toolchain: Toolchain) -> None:
        """A cache that grows once per edit of one file fills a disk during a rebase.
        Bounded, but not at one: base and head are two live generations of most TUs."""
        checkout = project(tmp_path / "w", {"a.cpp": "int v0();\n"}, ("a.cpp",))
        for version in range(GENERATIONS + 3):
            edit(checkout, "a.cpp", f"int v{version}();\n")
            build(checkout, toolchain, tmp_path)
        assert len(list(store(tmp_path).rglob("*.json.gz"))) == GENERATIONS

    def test_the_default_store_is_under_the_configured_cache_root(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """`acquire/workspace.py` already decides where this tool's cache lives, honouring
        CPPSD_CACHE_DIR then XDG. A second convention would put half the cache somewhere
        the user did not redirect."""
        checkout = project(tmp_path / "w", SHARED, ("a.cpp",))
        cache = TuCache.open(checkout, toolchain=toolchain)
        assert cache.store.is_relative_to(cache_root())

    def test_a_partial_survives_serialization_unchanged(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        checkout = project(tmp_path / "w", SHARED, ("b.cpp",))
        parsed = extract(checkout.cpp_entries[0], root=checkout.path, toolchain=toolchain)
        assert _partial_dump(loads(dumps(parsed))) == _partial_dump(parsed)


class TestDecliningToCache:
    """ARCH-001 §9 admits exactly one response to a closure it cannot compute: do not
    cache, and say so. A weaker key would be faster and would produce stale hits."""

    def test_no_scanner_disables_the_cache_and_the_model_says_so(
        self, tmp_path: Path, toolchain: Toolchain, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cpp_semantic_diff.produce.cache.candidate_scanners", lambda tc: [])
        checkout = project(tmp_path / "w", SHARED, ("a.cpp", "b.cpp"))
        model, cache = build(checkout, toolchain, tmp_path)

        assert cache.disabled
        assert not list(store(tmp_path).rglob("*.json.gz")), "keyed without a closure"
        assert any("cache disabled" in note for note in model.completeness.notes)
        # Slow, but not wrong: the model is the one a full parse produces.
        monkeypatch.undo()
        assert model.fingerprint() == build(checkout, toolchain, tmp_path)[0].fingerprint()

    def test_a_tu_with_no_closure_is_parsed_and_not_stored(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """A TU the scanner cannot preprocess -- against an unbuilt tree, usually one that
        includes a header the build generates (ARCH-001 §3.1). It has no closure, so it
        has no sound key, so it is parsed every time and reported."""
        sources = {**SHARED, "bad.cpp": '#include "generated-by-the-build.h"\nint gone();\n'}
        checkout = project(tmp_path / "w", sources, ("a.cpp", "bad.cpp"))
        model, cache = build(checkout, toolchain, tmp_path)

        assert cache.stats.uncacheable == 1
        assert cache.stats.stored == 1
        assert any("include closure" in note for note in model.completeness.notes)

        again, cache = build(checkout, toolchain, tmp_path)
        assert hits(again) == {"a.cpp"}
        assert cache.stats.uncacheable == 1


class TestThePathologicalCaseIsReported:
    def test_a_widely_included_header_says_that_it_rebuilt_everything(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """ARCH-001 §9: touching a widely-included header invalidates every TU that
        includes it and degrades to a full rebuild, which "must be reported as such rather
        than silently taking ten minutes". The cache knows the difference between a TU it
        never had and a TU it had and lost, and only the second is this."""
        sources = {"wide.h": "#pragma once\nint w();\n"}
        tus = [f"t{i}.cpp" for i in range(6)]
        for i, name in enumerate(tus):
            sources[name] = f'#include "wide.h"\nint f{i}() {{ return w(); }}\n'
        checkout = project(tmp_path / "w", sources, tus)

        cold, _ = build(checkout, toolchain, tmp_path)
        assert not any("invalidated" in note for note in cold.completeness.notes)

        (checkout.path / "wide.h").write_text("#pragma once\nint w();\nint extra();\n")
        model, cache = build(checkout, toolchain, tmp_path)

        assert cache.stats.stale == 6
        note = next(n for n in model.completeness.notes if "invalidated" in n)
        assert "6 of 6" in note and "widely-included header" in note

    def test_a_small_change_is_not_reported_as_a_rebuild(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """The note is a warning about cost, so it has to stay quiet on the case the whole
        design is for: a review touching a handful of TUs."""
        sources = {"wide.h": "#pragma once\nint w();\n"}
        tus = [f"t{i}.cpp" for i in range(6)]
        for i, name in enumerate(tus):
            sources[name] = f'#include "wide.h"\nint f{i}() {{ return w(); }}\n'
        checkout = project(tmp_path / "w", sources, tus)
        build(checkout, toolchain, tmp_path)

        edit(checkout, "t0.cpp", '#include "wide.h"\nint f0() { return w() + 1; }\n')
        model, cache = build(checkout, toolchain, tmp_path)

        assert cache.stats.stale == 1 and cache.stats.hits == 5
        assert not any("invalidated" in note for note in model.completeness.notes)


# -- helpers ------------------------------------------------------------------------


def _without_cache_hits(model: Model) -> dict:
    """A model's content, with the one field that is *meant* to differ removed."""
    dump = model.model_dump()
    for result in dump["completeness"]["tus"]:
        result["cache_hit"] = False
    return dump


def _partial_dump(partial) -> dict:
    return {
        "path": partial.path,
        "result": partial.result.model_dump(exclude={"cache_hit"}),
        "nodes": sorted(n.model_dump_json() for n in partial.nodes.values()),
        "edges": sorted(e.model_dump_json() for e in partial.edges.values()),
        "aliases": partial.aliases,
    }


def _named(model: Model, qualified: str) -> list:
    return [n for n in model.nodes.values() if n.qualified_name == qualified]


def _body_of(model: Model, qualified: str) -> str | None:
    matches = _named(model, qualified)
    return matches[0].body_hash if matches else None
