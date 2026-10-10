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
import os
import re
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import cpp_semantic_diff.produce.cache as cache_module
from cpp_semantic_diff.acquire.workspace import Checkout, CompileDatabase, cache_root
from cpp_semantic_diff.errors import InvalidSettingError, PreconditionError
from cpp_semantic_diff.model.ir import Model, Node
from cpp_semantic_diff.produce.build import build_model
from cpp_semantic_diff.produce.cache import (
    GENERATIONS,
    TEMPORARY_FLOOR_SECONDS,
    TuCache,
    collect,
    dumps,
    find_scanner,
    loads,
)
from cpp_semantic_diff.produce.extract import TuExtract, extract
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

    def test_a_different_target_invalidates(self, tmp_path: Path, toolchain: Toolchain) -> None:
        """The determinant the content digests cannot see (ARCH-001 §12.1).

        Every other input to the model that varies between two hosts is already keyed *by
        content*: the include closure holds the standard library and clang's own builtin
        headers, so libstdc++ 12 versus 14 changes the key without anyone naming it. The
        target is the exception. The same headers under `x86_64` and `aarch64` declare
        different things -- different `sizeof`, different `__x86_64__` branches -- and hash
        byte-identically, so without the target in the identity the two hosts write two
        different partials under one key. That is the shared base cache §9 recommends,
        producing confidently wrong output.
        """
        checkout = project(tmp_path / "w", SHARED, ("a.cpp",))
        _, cache = build(checkout, toolchain, tmp_path)
        entry = checkout.cpp_entries[0]

        elsewhere = TuCache(
            root=checkout.path,
            toolchain=replace(toolchain, target="s390x-ibm-linux-gnu"),
            store=store(tmp_path),
            closures=cache.closures,
        )
        assert elsewhere.identity(entry) != cache.identity(entry)
        assert elsewhere.key(entry) != cache.key(entry)
        assert elsewhere.load(elsewhere.identity(entry), elsewhere.key(entry) or "") is None

    def test_repackaging_the_same_compiler_does_not_invalidate(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """The property that made the identity the *release* triple, kept while widening it. A
        distro rebuild changes the version string and the install path and nothing a parse can
        observe; keying on either would invalidate every cached model on every host, for
        nothing.
        """
        checkout = project(tmp_path / "w", SHARED, ("a.cpp",))
        _, cache = build(checkout, toolchain, tmp_path)
        entry = checkout.cpp_entries[0]

        rebuilt = TuCache(
            root=checkout.path,
            toolchain=replace(
                toolchain,
                version=f"Debian clang version {toolchain.release} (3+rebuild1)",
                library=Path("/opt/rebuilt/lib/libclang.so.1"),
            ),
            store=store(tmp_path),
            closures=cache.closures,
        )
        assert rebuilt.identity(entry) == cache.identity(entry)
        assert rebuilt.key(entry) == cache.key(entry)
        assert rebuilt.load(rebuilt.identity(entry), rebuilt.key(entry) or "") is not None

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

    @pytest.mark.parametrize("failure", [KeyboardInterrupt, OSError])
    def test_a_write_that_never_lands_leaves_no_temporary(
        self,
        tmp_path: Path,
        toolchain: Toolchain,
        monkeypatch: pytest.MonkeyPatch,
        failure: type[BaseException],
    ) -> None:
        """Nothing but `collect` looks for a temporary once its writer is gone, so one left
        by a failed rename or a Ctrl-C would sit in the store indefinitely. An interrupt
        still propagates -- cleaning up is not the same as swallowing it."""
        checkout = project(tmp_path / "w", SHARED, ("b.cpp",))
        partial = extract(checkout.cpp_entries[0], root=checkout.path, toolchain=toolchain)
        cache = TuCache(root=checkout.path, toolchain=toolchain, store=store(tmp_path), closures={})

        def interrupted(src: object, dst: object) -> None:
            raise failure()

        monkeypatch.setattr("cpp_semantic_diff.produce.cache.os.replace", interrupted)
        if failure is OSError:
            cache.save("ab" * 8, "k" * 16, partial)
        else:
            with pytest.raises(KeyboardInterrupt):
                cache.save("ab" * 8, "k" * 16, partial)

        assert not list(store(tmp_path).rglob("*.tmp"))
        assert not list(store(tmp_path).rglob("*.json.gz"))
        assert cache.stats.stored == 0

    def test_a_write_that_fills_the_disk_leaves_no_temporary(
        self, tmp_path: Path, toolchain: Toolchain, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A partial write is the case that matters most: leaving it keeps the disk full."""
        checkout = project(tmp_path / "w", SHARED, ("b.cpp",))
        partial = extract(checkout.cpp_entries[0], root=checkout.path, toolchain=toolchain)
        cache = TuCache(root=checkout.path, toolchain=toolchain, store=store(tmp_path), closures={})
        real_write = Path.write_bytes

        def disk_full(self: Path, data: bytes) -> int:
            real_write(self, data[: len(data) // 2])
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(Path, "write_bytes", disk_full)
        cache.save("ab" * 8, "k" * 16, partial)
        monkeypatch.undo()

        assert not list(store(tmp_path).rglob("*.tmp"))

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

    def test_a_missing_scanner_override_is_named_not_replaced(
        self, tmp_path: Path, toolchain: Toolchain, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A scanner that works is on this host (the module skips otherwise), so falling
        back would succeed -- and would run a scanner the user did not choose without
        saying so. The cache goes off instead, and the reason names the setting."""
        missing = tmp_path / "no-such-clang-scan-deps"
        monkeypatch.setenv("CPPSD_SCAN_DEPS", str(missing))

        scanner, reason = find_scanner(toolchain)
        assert scanner is None
        assert f"CPPSD_SCAN_DEPS={missing}" in reason
        assert "does not exist" in reason

        checkout = project(tmp_path / "w", SHARED, ("a.cpp",))
        model, cache = build(checkout, toolchain, tmp_path)
        assert cache.disabled == reason
        notes = " ".join(model.completeness.notes)
        assert f"CPPSD_SCAN_DEPS={missing}" in notes
        assert "unset it" in notes, "the remedy is the override, not a package install"

    def test_a_scanner_override_that_is_not_executable_is_named(
        self, tmp_path: Path, toolchain: Toolchain, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inert = tmp_path / "clang-scan-deps"
        inert.write_text("", encoding="utf-8")
        inert.chmod(0o644)
        monkeypatch.setenv("CPPSD_SCAN_DEPS", str(inert))

        scanner, reason = find_scanner(toolchain)
        assert scanner is None
        assert f"CPPSD_SCAN_DEPS={inert}" in reason

    def test_a_working_scanner_override_is_used(
        self, toolchain: Toolchain, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CPPSD_SCAN_DEPS", raising=False)
        found, _ = find_scanner(toolchain)
        assert found is not None
        monkeypatch.setenv("CPPSD_SCAN_DEPS", str(found))
        assert find_scanner(toolchain) == (found, "")

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


class TestAnOverriddenResourceDirIsCoveredOrDeclined:
    """`CPPSD_RESOURCE_DIR` changes what the parser reads and not what the scanner scans.

    `clang-scan-deps` is given the compile database's own command, which carries no
    `-resource-dir`, so the closure describes the resource directory *it* resolved. The
    override's path is a parse argument and reaches the identity; its contents reach
    nothing. Whether that is a stale hit waiting to happen depends entirely on whether the
    two directories are the same one, so the cache asks rather than assumes -- the two
    tests below are the two answers, and getting the first one wrong is silent.
    """

    def test_a_resource_dir_outside_every_closure_disables_the_cache(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """The builtin headers this parse reads would be keyed by path, so nothing is stored. A
        key that names a directory instead of hashing it invalidates when the override is
        pointed somewhere else and hits after the directory's contents change -- and the
        contents are what the parse reads (ARCH-001 §9; declining to cache beats a weaker
        key)."""
        # A second resource directory, holding what this build's TUs need of one. Its
        # contents are irrelevant to the claim and its *path* is the whole point: the
        # hazard is builtin headers that the scanner never reported, whether or not they
        # happen to say the same thing as the ones it did.
        unscanned = tmp_path / "resource-dir" / "include"
        unscanned.mkdir(parents=True)
        (unscanned / "stddef.h").write_text(
            "typedef unsigned long size_t;\ntypedef long ptrdiff_t;\n", encoding="utf-8"
        )
        (unscanned / "stdarg.h").write_text("\n", encoding="utf-8")

        checkout = project(tmp_path / "w", SHARED, ("a.cpp", "b.cpp"))
        model, cache = build(checkout, replace(toolchain, resource_dir=unscanned.parent), tmp_path)

        assert cache.disabled and "CPPSD_RESOURCE_DIR" in cache.disabled
        assert not list(store(tmp_path).rglob("*.json.gz")), "keyed on a path, not content"
        assert any("CPPSD_RESOURCE_DIR" in note for note in model.completeness.notes)
        # Slow, not wrong: the model is the one a full parse produces.
        assert _named(model, "app::twice")

    def test_the_resource_dir_the_closure_already_names_is_cached_as_usual(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """The other half of the same rule, and the reason the check is a check.

        The ordinary use of the override is a libclang that cannot find its own builtin headers
        being pointed at the ones the matching `clang-scan-deps` uses. Those headers are then
        closure members like any other and are content-hashed already, so refusing to cache --
        or adding the directory to the key a second time -- would spend a full reparse
        defending a hazard the key already defends."""
        sources = {"a.cpp": "#include <cstddef>\nstd::size_t width() { return sizeof(int); }\n"}
        checkout = project(tmp_path / "w", sources, ("a.cpp",))
        scanned = _scanner_resource_dir(TuCache.open(checkout, toolchain=toolchain))
        if scanned is None:  # pragma: no cover - depends on the host's header layout
            pytest.skip("no resource-directory header in the include closure to point at")

        covered = replace(toolchain, resource_dir=scanned)
        model, cache = build(checkout, covered, tmp_path)
        assert not cache.disabled
        assert cache.stats.stored == 1

        warm, cache = build(checkout, covered, tmp_path)
        assert cache.stats.hits == 1
        assert warm.fingerprint() == model.fingerprint()


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

    def test_the_cost_it_quotes_is_measured_on_this_build(
        self, tmp_path: Path, toolchain: Toolchain
    ) -> None:
        """The note is read as the explanation of why a run was slow, so the cost in it has
        to be this run's.

        It used to quote a constant. The constant was measured on one host at one worker
        count, was true when it was written, and became false without anyone editing the
        line -- a fact asserted in prose goes stale silently, while a fact read off the
        clock cannot. What this asserts is that the figure is a function of how long the
        build took: the same counters over two more minutes must move the quoted cost by
        two minutes."""
        sources = {"wide.h": "#pragma once\nint w();\n"}
        tus = [f"t{i}.cpp" for i in range(6)]
        for i, name in enumerate(tus):
            sources[name] = f'#include "wide.h"\nint f{i}() {{ return w(); }}\n'
        checkout = project(tmp_path / "w", sources, tus)
        build(checkout, toolchain, tmp_path)

        (checkout.path / "wide.h").write_text("#pragma once\nint w();\nint extra();\n")
        _, cache = build(checkout, toolchain, tmp_path)
        assert cache.stats.stale == 6

        quick = _quoted_seconds(cache.notes())
        cache.opened -= 120  # the same six TUs, on a machine two minutes slower
        slow = _quoted_seconds(cache.notes())

        assert slow - quick == pytest.approx(120, abs=1), "the cost is not read off a clock"


# -- helpers ------------------------------------------------------------------------


def _without_cache_hits(model: Model) -> dict[str, Any]:
    """A model's content, with the one field that is *meant* to differ removed."""
    dump = model.model_dump()
    for result in dump["completeness"]["tus"]:
        result["cache_hit"] = False
    return dump


class TestCollectingSupersededGenerations:
    """`_evict` cannot reach the thing that actually accumulates.

    It trims within one TU's directory and only runs on a *write*. `Cache.identity` hashes the
    producer code digest, so changing `extract.py` gives every TU a new identity directory and
    nothing ever writes to the old ones again -- so nothing ever evicts them. Every producer
    change has left a full copy of the cache behind. Measured on 2026-08-04: 1,156 entries and
    229 MB, of which 576 entries and 147 MB were unreachable (v0 tracker §6,
    reports/2026-08-01--v0-build/STATUS.md).
    """

    def entry(self, store: Path, name: str, *, age_days: float) -> Path:
        path = store / name[:2] / name / f"{name}.json.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 1024)
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def test_an_unused_entry_goes_and_a_used_one_stays(self, tmp_path: Path) -> None:
        old = self.entry(tmp_path, "aaaaaa", age_days=90)
        fresh = self.entry(tmp_path, "bbbbbb", age_days=1)

        sweep = collect(tmp_path, max_age_days=30)

        assert not old.exists()
        assert fresh.exists()
        assert (sweep.removed, sweep.kept) == (1, 1)
        assert sweep.freed == 1024

    def test_a_dry_run_reports_without_removing(self, tmp_path: Path) -> None:
        """The number is worth having before the deletion, given the deletion is the point."""
        old = self.entry(tmp_path, "aaaaaa", age_days=90)
        sweep = collect(tmp_path, max_age_days=30, dry_run=True)
        assert old.exists()
        assert sweep.removed == 1

    def test_it_reaches_across_producer_generations(self, tmp_path: Path) -> None:
        """The case `_evict` structurally cannot: two identity directories for one TU, the
        older one orphaned by a producer change and never written to again."""
        superseded = self.entry(tmp_path / "v1", "old111", age_days=90)
        current = self.entry(tmp_path / "v1", "new222", age_days=0)

        collect(tmp_path, max_age_days=30)

        assert not superseded.exists()
        assert current.exists()

    def test_emptied_directories_do_not_survive_their_entries(self, tmp_path: Path) -> None:
        old = self.entry(tmp_path, "aaaaaa", age_days=90)
        collect(tmp_path, max_age_days=30)
        assert not old.parent.exists(), "the sharded skeleton is swept with its contents"

    def test_a_missing_store_is_not_an_error(self, tmp_path: Path) -> None:
        """`cppsd cache gc` on a machine that has never run a diff is a no-op, not a crash."""
        assert collect(tmp_path / "nothing-here").removed == 0


class TestCollectingAbandonedWrites:
    """A write killed mid-flight leaves a temporary that no key names and no run reads.
    `collect` is the only thing that can find one, so it has to look -- without touching a
    temporary that another run is writing right now."""

    def temporary(self, store: Path, *, age_seconds: float) -> Path:
        path = store / "aa" / "aaaaaa" / f".key.{os.getpid()}.{age_seconds:.0f}.tmp"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 512)
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    def test_an_old_temporary_goes_and_a_fresh_one_stays(self, tmp_path: Path) -> None:
        old = self.temporary(tmp_path, age_seconds=90 * 86400)
        fresh = self.temporary(tmp_path, age_seconds=60)

        sweep = collect(tmp_path, max_age_days=30)

        assert not old.exists()
        assert fresh.exists()
        assert (sweep.abandoned, sweep.abandoned_freed) == (1, 512)
        assert sweep.total == 0, "a temporary was never an entry"

    def test_age_zero_still_spares_a_write_in_progress(self, tmp_path: Path) -> None:
        """`max_age_days=0` means every entry, and a live temporary is not an entry."""
        live = self.temporary(tmp_path, age_seconds=60)
        abandoned = self.temporary(tmp_path, age_seconds=TEMPORARY_FLOOR_SECONDS + 60)

        sweep = collect(tmp_path, max_age_days=0)

        assert live.exists()
        assert not abandoned.exists()
        assert sweep.abandoned == 1

    def test_a_dry_run_counts_temporaries_without_removing_them(self, tmp_path: Path) -> None:
        old = self.temporary(tmp_path, age_seconds=90 * 86400)
        sweep = collect(tmp_path, max_age_days=30, dry_run=True)
        assert old.exists()
        assert sweep.abandoned == 1


class TestANegativeAgeIsRefused:
    def test_a_negative_age_raises_and_deletes_nothing(self, tmp_path: Path) -> None:
        """A cutoff in the future reads as "collect everything" -- the whole cache gone on a
        typo. Zero stays allowed: it is a real request, for everything not in use now."""
        entry = tmp_path / "aa" / "aaaaaa" / "aaaaaa.json.gz"
        entry.parent.mkdir(parents=True)
        entry.write_bytes(b"x")

        with pytest.raises(InvalidSettingError, match="max_age_days"):
            collect(tmp_path, max_age_days=-1)
        assert entry.exists()
        collect(tmp_path, max_age_days=0)


class TestAHitKeepsAnEntryAlive:
    """`collect` sweeps on mtime, so a read has to refresh it.

    Otherwise the sweep collects exactly backwards: a TU that never changes is the one the
    cache serves most and writes least, so its entry would look staler the better the cache
    was working.
    """

    def test_loading_an_entry_refreshes_it(self, tmp_path: Path, toolchain: Toolchain) -> None:
        checkout = project(tmp_path / "src", {"a.cpp": "int a();\n"}, ["a.cpp"])
        cache = TuCache.open(checkout, toolchain=toolchain, store=store(tmp_path))

        entry = checkout.cpp_entries[0]
        list(cache.extracts([entry]))  # populates
        key = cache.key(entry)
        assert key is not None, "the entry must be cacheable or this tests nothing"
        path = cache._path(cache.identity(entry), key)

        stale = time.time() - 90 * 86400
        os.utime(path, (stale, stale))
        list(cache.extracts([entry]))  # a hit

        assert path.stat().st_mtime > stale, "a served entry must not look unused"


def _partial_dump(partial: TuExtract) -> dict[str, object]:
    return {
        "path": partial.path,
        "result": partial.result.model_dump(exclude={"cache_hit"}),
        "nodes": sorted(n.model_dump_json() for n in partial.nodes.values()),
        "edges": sorted(e.model_dump_json() for e in partial.edges.values()),
        "aliases": partial.aliases,
    }


def _quoted_seconds(notes: Sequence[str]) -> float:
    """The production cost the invalidation note reports, as a number."""
    note = next(n for n in notes if "invalidated" in n)
    match = re.search(r"([\d.]+) s to produce", note)
    assert match is not None, f"the note quotes no measured cost: {note}"
    return float(match.group(1))


def _scanner_resource_dir(cache: TuCache) -> Path | None:
    """The resource directory `clang-scan-deps` resolved, read out of the closure itself.

    Asked of the scan rather than of a sibling binary, because the claim under test is
    about agreement between the scanner and the parser, and `clang -print-resource-dir`
    would answer for whichever clang is on PATH -- which is not necessarily either of them.
    """
    for closure in cache.closures.values():
        for path in closure:
            resolved = path.resolve()
            if resolved.name == "stddef.h" and "clang" in resolved.parts:
                return resolved.parent.parent
    return None


def _named(model: Model, qualified: str) -> list[Node]:
    return [n for n in model.nodes.values() if n.qualified_name == qualified]


def _body_of(model: Model, qualified: str) -> str | None:
    matches = _named(model, qualified)
    return matches[0].body_hash if matches else None
