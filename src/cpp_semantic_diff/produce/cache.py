"""The per-TU model cache -- what makes the performance budget reachable.

ARCH-001 §9 sets the budget: **a review touching <= 20 TUs completes in under 30 s on a
warm cache**. A cold `fmt` model is 50 TUs in 76-117 s depending on the host, so the budget
is not a matter of making the parser faster; it is a matter of not parsing what has not
changed. Measured on fmt: 76 s cold, 6.0 s fully warm, 18.9 s for a review that touched
five TUs -- a marginal 2.6 s per translation unit the diff actually invalidated.

**The unit is the translation unit, not the commit.** Caching whole models would mean the
first review against any new base pays full price, and the base is exactly what every
review shares. Keyed per TU, a commit's model is assembled from cached partials plus a
reparse of only the invalid ones, so the marginal cost of a review is proportional to what
the diff touched -- and base and head share every TU the diff did not touch.

    key = hash(TU source content, transitive header content, compile flags, producer version)

Each component defends against a specific way of being wrong:

- **Content, never mtime or path.** Worktrees are materialized fresh per revision
  (`acquire/workspace.py`), so mtimes are meaningless across them and the path carries a
  SHA that changes when nothing else did. Absolute paths are normalized out of the flags
  for the same reason: two worktrees of the same commit-shaped tree must key alike, or the
  cache never hits across revisions and the whole design is pointless.
- **Transitive header content**, from `clang-scan-deps`. Keying on the TU's own text only
  would hit after a header edit -- the model would be built from a partial that describes
  the *old* header, in a tool whose entire job is noticing that the header changed. That
  is the stale hit ARCH-001 §9 calls confidently wrong output, and it is invisible: the
  model looks complete.
- **Compile flags**, as the extractor actually parses with them (`parse_arguments`), not
  as the compile database wrote them. Keying on the raw command would let a flag the
  extractor strips (`-o`, `-Werror`) invalidate an entry that is still perfectly valid.
- **Producer version**, which is two things, not one. `Toolchain.id` identifies the parser
  -- the release triple rather than the distro's version string, precisely so a package
  rebuild that changes nothing observable does not invalidate every entry. But libclang
  only supplies the AST; `extract.py` decides what becomes a node and `ir.py` fixes the
  shape it is stored in, so a digest of their source is keyed too. A model built by a
  different producer is a different model, and the extractor is half the producer.

**When the closure cannot be computed, nothing is cached.** `clang-scan-deps` is the only
thing here that knows which headers a TU reads, and a key without them produces stale hits
rather than misses. So a missing or mismatched scanner disables the cache and says so in
`Completeness`; a single TU the scanner cannot handle is parsed from source and not stored.
Declining to cache is slow. A stale hit is wrong.
"""

from __future__ import annotations

import functools
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import uuid
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from glob import glob
from pathlib import Path

import clang.cindex as ci

# Imported as modules, not names, because `_producer_code_digest` hashes their source.
# `cpp_semantic_diff.produce` re-exports a *function* named `extract`, so `from ... import
# extract` here would bind the function and silently hash nothing.
import cpp_semantic_diff.model.ir as ir
import cpp_semantic_diff.produce.extract as extract_module
from cpp_semantic_diff.acquire.workspace import Checkout, CompileEntry, cache_root
from cpp_semantic_diff.model.ir import Edge, Node, NodeId, TuResult
from cpp_semantic_diff.produce.extract import TuExtract, extract, parse_arguments
from cpp_semantic_diff.produce.toolchain import (
    MINIMUM_MAJOR,
    Toolchain,
    parse_release,
    path_version,
)

#: The on-disk shape of an entry *and* the recipe that keys it. Bumping this makes every
#: existing entry unreachable, which is the only safe response to a change in what is
#: stored or in what the key covers: an entry written under the old recipe cannot be known
#: to be valid under the new one.
ENTRY_FORMAT = 1


@functools.cache
def _producer_code_digest() -> str:
    """Version the *extraction code*, not just the parser it drives.

    ARCH-001 §9 names "producer version" as a key component and STATUS §4 spells out why:
    a model built by a different producer is a different model. `Toolchain.id` covers only
    half of that. libclang supplies the AST; `extract.py` decides what becomes a node, what
    a signature reads as, and which spellings are stable -- and `ir.py` fixes the shape all
    of it is stored in. Change either and the same source under the same libclang yields a
    different partial, so an entry written before the change is not valid after it.

    This is not hypothetical. Extraction was corrected today to stop leaking absolute
    worktree paths into `name`/`qualified_name`, to strip lazily-evaluated exception
    specifications out of `signature`, and to tag struct templates as structs. Every entry
    written before those fixes is confidently wrong and byte-identical in its inputs, which
    is exactly the stale hit §9 calls the worst failure this system can have.

    Hashing the source rather than a hand-maintained constant is deliberate. A constant is
    a step someone must remember at the moment they are thinking about something else, and
    forgetting it fails toward serving stale entries. A source digest fails the other way:
    reformatting a comment invalidates the cache and costs a reparse. Given the two failure
    directions, only one of them is allowed to be silent.
    """
    digest = hashlib.blake2b(digest_size=16)
    for module in (ir, extract_module):
        source = Path(module.__file__ or "")
        # A module with no readable source is not evidence that nothing changed. Refuse to
        # claim a version rather than key on a digest that silently covers less than it
        # appears to -- callers treat this as "cannot key soundly", the same as a missing
        # include closure.
        digest.update(source.read_bytes())
    return digest.hexdigest()


#: Entries kept per TU identity. Not 1: a review compares two revisions, and most TUs are
#: identical at both, but the ones the diff touched have a distinct entry per side, and
#: alternating between base and head must not evict. Not unbounded: fmt's partials measure
#: 310 kB compressed each, so an unbounded cache grows by that much per TU per edit.
GENERATIONS = 4

#: Stand-in for the checkout root in a cache key. Everything under it is repository
#: content whose identity is its repo-relative path, not where the worktree was put.
ROOT_TOKEN = "${ROOT}"

SCAN_DEPS_NAME = "clang-scan-deps"
SCAN_DEPS_TIMEOUT_SECONDS = 300

#: Last resort only, exactly as in `toolchain.py`: everything above this in
#: `candidate_scanners` *asks* the host where its LLVM tools are. These are guesses about
#: where a distribution puts them, kept because a usable LLVM can exist with neither a
#: versioned tool on PATH nor an llvm-config to ask.
SCANNER_GLOBS = (
    "/usr/lib/llvm-*/bin/clang-scan-deps",
    "/usr/lib64/llvm-*/bin/clang-scan-deps",
    "/usr/local/opt/llvm/bin/clang-scan-deps",
    "/opt/homebrew/opt/llvm/bin/clang-scan-deps",
    "/opt/homebrew/opt/llvm@*/bin/clang-scan-deps",
)


# -- finding the scanner ------------------------------------------------------------


def _run(argv: Sequence[str]) -> str:
    try:
        return subprocess.run(
            list(argv), capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def candidate_scanners(toolchain: Toolchain) -> list[Path]:
    """Every plausible `clang-scan-deps` on this host, most preferred first.

    The same problem `toolchain.py` solves for the library, with the same answer, because
    it is the same distribution decision: Ubuntu ships `clang-scan-deps-18` and no bare
    `clang-scan-deps`, so looking only for the unsuffixed name on PATH finds nothing on
    the systems this most needs to work on. Ordered by how much the host is being *asked*
    rather than guessed at -- override, PATH under both names, llvm-config's own bindir,
    the directory the loaded library came from, and only then patterns.
    """
    candidates: list[Path] = []

    if override := os.environ.get("CPPSD_SCAN_DEPS"):
        candidates.append(Path(override))

    # The toolchain's own major first: a scanner and a parser that disagree about the
    # header search compute closures for two different compilations.
    versions = [toolchain.major, *range(30, MINIMUM_MAJOR - 1, -1)]
    for name in [f"{SCAN_DEPS_NAME}-{v}" for v in versions] + [SCAN_DEPS_NAME]:
        if found := shutil.which(name):
            candidates.append(Path(found))

    for config in [f"llvm-config-{v}" for v in versions] + ["llvm-config"]:
        if not (tool := shutil.which(config)):
            continue
        if bindir := _run([tool, "--bindir"]):
            candidates.append(Path(bindir) / SCAN_DEPS_NAME)

    # Beside the library that was actually loaded: an LLVM unpacked from a tarball has
    # lib/libclang.so and bin/clang-scan-deps as siblings, and neither is on PATH.
    library = toolchain.library.resolve()
    candidates.append(library.parent.parent / "bin" / SCAN_DEPS_NAME)
    candidates.append(library.parent / SCAN_DEPS_NAME)

    for pattern in SCANNER_GLOBS:
        candidates.extend(Path(p) for p in sorted(glob(pattern), key=lambda p: -path_version(p)))

    seen: set[Path] = set()
    unique: list[Path] = []
    for path in candidates:
        if not path.is_file() or not os.access(path, os.X_OK):
            continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def scanner_release(path: Path) -> str | None:
    """The release triple a scanner reports, or None if it does not run."""
    return parse_release(_run([str(path), "--version"]))


def find_scanner(toolchain: Toolchain) -> tuple[Path | None, str]:
    """The scanner to use, or None and the reason there is none.

    A major-version mismatch is refused rather than tolerated. The scanner decides which
    headers are in the key, and one that resolves the search path differently from the
    parser can report a *smaller* closure than the parser actually reads -- which is not a
    miss, it is a hit on an entry that ignores a header. Refusing costs every review a
    full parse and says why; accepting costs correctness silently (ARCH-001 §9, §11).
    """
    candidates = candidate_scanners(toolchain)
    if not candidates:
        return None, (
            f"no {SCAN_DEPS_NAME} found (looked at $CPPSD_SCAN_DEPS, PATH including "
            f"versioned names, llvm-config --bindir, and beside {toolchain.library})"
        )
    mismatched: list[str] = []
    for path in candidates:
        release = scanner_release(path)
        if release is None:
            continue
        if release.split(".")[0] == str(toolchain.major):
            return path, ""
        mismatched.append(f"{path} is {release}")
    return None, (
        f"no {SCAN_DEPS_NAME} matching {toolchain.id}: {', '.join(mismatched) or 'none ran'}"
    )


# -- the include closure ------------------------------------------------------------


def _make_tokens(text: str) -> list[str]:
    """Split one make prerequisite list, honouring the escaping clang emits.

    Spaces in paths arrive as `\\ ` and a literal `$` as `$$`. Splitting on whitespace
    would break a path with a space in it into two paths, neither of which exists -- and a
    dependency that cannot be read is a dependency that cannot invalidate anything.
    """
    tokens: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\" and i + 1 < len(text) and text[i + 1] in " \\#":
            current.append(text[i + 1])
            i += 2
            continue
        if char == "$" and text.startswith("$$", i):
            current.append("$")
            i += 2
            continue
        if char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
            i += 1
            continue
        current.append(char)
        i += 1
    if current:
        tokens.append("".join(current))
    return tokens


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:  # pragma: no cover - a path the filesystem refuses to answer about
        return path


def parse_make_output(text: str) -> dict[Path, list[Path]]:
    """Make rules from `clang-scan-deps -format=make` → source file → its closure.

    Keyed by the **first prerequisite**, which is the TU itself. The rule's target is the
    object file, and matching that back to a compile-database entry means reproducing how
    the build system names outputs -- a mapping this tool has no business knowing.
    """
    closures: dict[Path, list[Path]] = {}
    for rule in text.replace("\\\n", " ").splitlines():
        target, separator, prerequisites = rule.partition(":")
        if not separator or not target.strip():
            continue
        deps = [Path(t) for t in _make_tokens(prerequisites)]
        if deps:
            # Keyed by the resolved path, because a compile database routinely names the
            # same file differently (`build/../src/x.cc`) and a lookup that missed on
            # spelling would silently disable the cache for that TU.
            closures[_resolved(deps[0])] = deps
    return closures


def include_closures(
    scanner: Path, database: Path, *, timeout: float = SCAN_DEPS_TIMEOUT_SECONDS
) -> dict[Path, list[Path]]:
    """Every TU's transitive include closure, in one scan of the whole database.

    One invocation rather than one per TU: the scanner shares a preprocessing cache across
    the database, which is what makes it cheap at scale (opencv's 1,292 TUs in 14.1 s; fmt
    scans in 0.1 s).

    A non-zero exit is *not* treated as total failure. The scanner reports per-TU errors on
    stderr and keeps going, and against an unbuilt tree that is expected -- 9.2 % of
    opencv's TUs include a header CMake emits during the build (ARCH-001 §3.1). Those TUs
    simply have no closure, and a TU with no closure is not cached.
    """
    try:
        proc = subprocess.run(
            [str(scanner), "-compilation-database", str(database), "-format=make"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return {}
    return parse_make_output(proc.stdout)


# -- entries ------------------------------------------------------------------------


def dumps(partial: TuExtract) -> bytes:
    """Serialize a partial model for a later process to read.

    gzip is not only for size. Its trailer is a length and a CRC over the whole stream, so
    a truncated or corrupted entry raises on read instead of decoding into a partial model
    that looks complete -- the same failure this module exists to prevent, arriving by way
    of a full disk rather than a bad key.
    """
    payload = {
        "format": ENTRY_FORMAT,
        "path": partial.path,
        "result": partial.result.model_dump(mode="json"),
        "nodes": [n.model_dump(mode="json") for n in partial.nodes.values()],
        "edges": [e.model_dump(mode="json") for e in partial.edges.values()],
        "aliases": partial.aliases,
    }
    return gzip.compress(json.dumps(payload).encode(), compresslevel=1)


def loads(blob: bytes) -> TuExtract:
    """Rebuild a partial model. Raises on anything it does not fully understand."""
    payload = json.loads(gzip.decompress(blob))
    if payload.get("format") != ENTRY_FORMAT:
        raise ValueError(f"entry format {payload.get('format')!r}, expected {ENTRY_FORMAT}")

    nodes: dict[NodeId, Node] = {}
    for raw in payload["nodes"]:
        node = Node.model_validate(raw)
        nodes[node.id] = node
    edges: dict[str, Edge] = {}
    for raw in payload["edges"]:
        edge = Edge.model_validate(raw)
        edges[edge.id] = edge

    result = TuResult.model_validate(payload["result"])
    # The record of *how the model was built* (ARCH-001 §4.4). Set here rather than
    # stored, because it is a property of this build and not of the entry.
    result.cache_hit = True
    return TuExtract(
        path=payload["path"],
        result=result,
        nodes=nodes,
        edges=edges,
        aliases=dict(payload["aliases"]),
    )


# -- the cache ----------------------------------------------------------------------


@dataclass
class CacheStats:
    """What the cache did, per TU. `TuResult.cache_hit` carries the same fact per TU;
    these are the counts the notes are written from."""

    hits: int = 0
    stale: int = 0  #: this TU was cached, under a different key -- its content changed
    cold: int = 0  #: never cached
    uncacheable: int = 0  #: no include closure, so no sound key
    stored: int = 0
    unreadable_deps: set[str] = field(default_factory=set)

    @property
    def known(self) -> int:
        """TUs the cache had *some* entry for, so the miss rate below means something."""
        return self.hits + self.stale


class TuCache:
    """A content-addressed store of partial models, and a producer of them.

    Fits the seam `build.py` left: `extracts()` yields the same `Iterable[TuExtract]` the
    parse loop does, and `assemble` cannot tell the difference. Every hit is deserialized
    fresh from disk and handed out exactly once, which is what makes it safe to give to an
    assembly that merges by mutation -- there is no shared object for a previous build to
    have poisoned.
    """

    def __init__(
        self,
        *,
        root: Path,
        toolchain: Toolchain,
        store: Path,
        closures: dict[Path, list[Path]],
        disabled: str = "",
    ) -> None:
        self.root = root.resolve()
        #: Both spellings of the worktree path. A compile database records the path it was
        #: configured with, which may differ from the resolved one by a symlink; a key that
        #: normalized only one of them would carry the revision SHA and never be shared.
        self.root_forms = sorted({str(root), str(self.root)}, key=len, reverse=True)
        self.toolchain = toolchain
        self.store = store
        self.closures = closures
        self.disabled = disabled
        self.stats = CacheStats()
        self._digests: dict[Path, str] = {}
        self._relatives: dict[Path, str] = {}

    @classmethod
    def open(
        cls,
        checkout: Checkout,
        *,
        toolchain: Toolchain,
        store: Path | None = None,
    ) -> TuCache:
        """Resolve the scanner, scan the database, and be ready to answer.

        Never raises. A cache that cannot be built is a slow build, not a failed one --
        unlike a cache that answers wrongly, which is a failed *review* that looks fine.
        """
        # `cache_root()` already decides where this tool's cache lives, honouring
        # CPPSD_CACHE_DIR and then XDG. Worktrees are its neighbours, not its parents.
        store = store or cache_root() / "tu-models" / f"v{ENTRY_FORMAT}"
        scanner, reason = find_scanner(toolchain)
        if scanner is None:
            return cls(
                root=checkout.path,
                toolchain=toolchain,
                store=store,
                closures={},
                disabled=reason,
            )
        closures = include_closures(scanner, checkout.compile_db.path)
        if not closures:
            return cls(
                root=checkout.path,
                toolchain=toolchain,
                store=store,
                closures={},
                disabled=f"{scanner} produced no include closures for {checkout.compile_db.path}",
            )
        return cls(root=checkout.path, toolchain=toolchain, store=store, closures=closures)

    # -- keys ------------------------------------------------------------------

    def digest(self, path: Path) -> str | None:
        """Content digest of one file, or None if it cannot be read.

        Memoized for the life of one build: a header included by every TU is one read, not
        one per includer, and fmt's 50 TUs share 496 distinct files. Memoizing by path is
        safe only *within* a build -- nothing is written across a build, and across builds
        the file is read again, because a cache that trusted a remembered digest would be
        the mtime design this key exists to avoid.
        """
        if (known := self._digests.get(path)) is not None:
            return known or None
        try:
            data = path.read_bytes()
        except OSError:
            self._digests[path] = ""
            return None
        digest = hashlib.blake2b(data, digest_size=16).hexdigest()
        self._digests[path] = digest
        return digest

    def _relative(self, path: Path) -> str:
        """Repository-relative where the file is under analysis, absolute otherwise.

        A repository header has to key by its repo-relative path or two worktrees of the
        same content key differently. A system header has no repo-relative path, and its
        absolute one is stable on the host the cache belongs to.

        Memoized with the digests and for the same reason: fmt's 50 TUs name 495 distinct
        files between them but 25,000 closure members in total, and `resolve()` is a
        syscall per component. Unmemoized it cost 0.9 s of a 8 s warm build.
        """
        if (known := self._relatives.get(path)) is not None:
            return known
        resolved = _resolved(path)
        try:
            relative = ROOT_TOKEN + "/" + resolved.relative_to(self.root).as_posix()
        except ValueError:
            relative = resolved.as_posix()
        self._relatives[path] = relative
        return relative

    def flags(self, entry: CompileEntry) -> list[str]:
        """The arguments the extractor will parse with, with this worktree's path removed.

        `parse_arguments` and not `entry.arguments`: a flag the extractor strips cannot be
        allowed to invalidate an entry that is still valid, and a flag it adds
        (`-resource-dir`) has to be in the key because it changes what gets parsed.

        The worktree path is normalized out because it contains the revision SHA. Left in,
        every entry would be keyed to one revision's worktree, base and head would share
        nothing, and the cache would only ever hit on a rerun of the identical review.
        """
        args = parse_arguments(entry, self.toolchain)
        normalized: list[str] = []
        for arg in args:
            for prefix in self.root_forms:
                arg = arg.replace(prefix, ROOT_TOKEN)
            normalized.append(arg)
        return normalized

    def identity(self, entry: CompileEntry) -> str:
        """What makes this *the same translation unit* across revisions and worktrees.

        Everything in the key except content. Splitting it out is what lets a miss say
        which kind of miss it is: an identity that has been seen before and no longer
        matches is a TU whose content changed, which is the invalidation ARCH-001 §9 asks
        to be reported when it goes pathological. An identity never seen is a cold TU.
        """
        digest = hashlib.blake2b(digest_size=16)
        digest.update(f"{ENTRY_FORMAT}\0".encode())
        digest.update(f"{self.toolchain.id}\0".encode())
        # The producer is the parser *and* the code that reads it (ARCH-001 §9).
        digest.update(f"{_producer_code_digest()}\0".encode())
        digest.update(f"{self._relative(entry.file)}\0".encode())
        for arg in self.flags(entry):
            digest.update(f"{arg}\0".encode())
        return digest.hexdigest()

    def key(self, entry: CompileEntry) -> str | None:
        """The full content key, or None if this TU cannot be keyed soundly.

        None is returned rather than a weaker key whenever the closure is missing or a
        file in it cannot be read. Both mean the same thing: some input to the model is
        not in the key, and an entry stored under it would be handed back after that input
        changed (ARCH-001 §9).
        """
        closure = self.closures.get(_resolved(entry.file))
        if closure is None:
            return None

        digest = hashlib.blake2b(digest_size=16)
        digest.update(self.identity(entry).encode())
        # The TU's own content, independently of the closure that should already contain
        # it -- the one input whose absence from the key would be unrecoverable.
        contents: dict[str, str] = {}
        for path in [entry.file, *closure]:
            relative = self._relative(path)
            if relative in contents:
                continue
            content = self.digest(path)
            if content is None:
                # A header the scanner named and the filesystem does not have. Inside the
                # repository this is a header whose content is part of the model and
                # cannot be hashed, so the TU is not cacheable. Outside it, the file is
                # still read by the parser through a path the scanner canonicalized away;
                # it is recorded by path and reported, because keying on a path is weaker
                # than keying on content and that should not be invisible.
                if relative.startswith(ROOT_TOKEN):
                    return None
                self.stats.unreadable_deps.add(relative)
                content = "absent"
            contents[relative] = content
        for relative in sorted(contents):
            digest.update(f"{relative}\0{contents[relative]}\0".encode())
        return digest.hexdigest()

    # -- storage ---------------------------------------------------------------

    def _directory(self, identity: str) -> Path:
        # Sharded: one directory per TU identity is thousands of directories for a large
        # project, and filesystems get slow long before they get full.
        return self.store / identity[:2] / identity

    def _path(self, identity: str, key: str) -> Path:
        return self._directory(identity) / f"{key}.json.gz"

    def load(self, identity: str, key: str) -> TuExtract | None:
        path = self._path(identity, key)
        try:
            blob = path.read_bytes()
        except OSError:
            return None
        try:
            partial = loads(blob)
        # Broad by necessity, and deliberately silent: a truncated gzip, a half-written
        # JSON document and an entry from a future format are all the same event -- this
        # entry cannot be trusted, so it is not one. Treating it as a miss reparses; the
        # alternative is reading a partial model that is missing whatever the writer had
        # not written yet.
        except Exception:
            path.unlink(missing_ok=True)
            return None
        return partial

    def save(self, identity: str, key: str, partial: TuExtract) -> None:
        """Write an entry so that no reader can ever see a half-written one.

        Written to a unique temporary name in the same directory and renamed into place:
        `os.replace` is atomic, so a concurrent reader sees either the previous entry or
        this one, and an interrupted write leaves a temporary file that no key names.
        """
        directory = self._directory(identity)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            temporary = directory / f".{key}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            temporary.write_bytes(dumps(partial))
            os.replace(temporary, self._path(identity, key))
            self.stats.stored += 1
            self._evict(directory)
        except OSError:
            # A cache that cannot be written is a slow build. Nothing about the model
            # depends on the write having happened.
            return

    def _evict(self, directory: Path) -> None:
        """Keep the newest `GENERATIONS` entries for this TU."""
        try:
            entries = sorted(directory.glob("*.json.gz"), key=lambda p: p.stat().st_mtime_ns)
        except OSError:
            return
        for stale in entries[:-GENERATIONS]:
            stale.unlink(missing_ok=True)

    # -- production ------------------------------------------------------------

    def extracts(
        self,
        entries: Iterable[CompileEntry],
        *,
        index: ci.Index | None = None,
    ) -> Iterator[TuExtract]:
        """Partial models for these TUs: cached where valid, parsed where not.

        The producer `build.py` was shaped around (ARCH-001 §9) -- an `Iterable[TuExtract]`
        that `assemble` consumes without knowing where any of them came from.
        """
        index = index or ci.Index.create()
        for entry in entries:
            key = None if self.disabled else self.key(entry)
            identity = "" if key is None else self.identity(entry)

            if key is not None:
                if (hit := self.load(identity, key)) is not None:
                    self.stats.hits += 1
                    yield hit
                    continue
                # Distinguishing "this TU changed" from "this TU was never cached" costs
                # one directory probe and is what makes the pathological case reportable.
                if self._directory(identity).is_dir():
                    self.stats.stale += 1
                else:
                    self.stats.cold += 1
            elif not self.disabled:
                self.stats.uncacheable += 1

            partial = extract(entry, root=self.root, toolchain=self.toolchain, index=index)
            # Only a successful parse is stored. A failure's cause is routinely *outside*
            # the key -- a header that does not exist is in no closure -- so an entry for
            # it could not be invalidated by the fix, and the tool would keep reporting a
            # TU as broken after it was repaired (ARCH-001 §11).
            if key is not None and partial.ok:
                self.save(identity, key, partial)
            yield partial

    def notes(self) -> list[str]:
        """What `Completeness` should say about how this model was built (ARCH-001 §4.4).

        Only the exceptional conditions. A warm build says nothing here: `cache_hit` on
        each `TuResult` already records it per TU, and notes are read as caveats.
        """
        notes: list[str] = []
        if self.disabled:
            notes.append(
                f"per-TU cache disabled: {self.disabled}. Every translation unit was "
                f"parsed from source. Install the LLVM tools package matching "
                f"{self.toolchain.id} (Debian/Ubuntu: llvm-<version>-tools, which is where "
                f"{SCAN_DEPS_NAME} lives) to restore it."
            )
            return notes

        if self.stats.uncacheable:
            notes.append(
                f"{self.stats.uncacheable} translation units have no include closure and "
                f"were parsed from source rather than cached: {SCAN_DEPS_NAME} could not "
                f"scan them, which against an unbuilt tree usually means a generated "
                f"header (ARCH-001 §3.1)"
            )

        # ARCH-001 §9 names this case in terms: touching a widely-included header
        # invalidates every TU that includes it and degrades to a full rebuild, and that
        # "must be reported as such rather than silently taking ten minutes".
        if self.stats.stale >= 5 and self.stats.stale > self.stats.hits:
            notes.append(
                f"{self.stats.stale} of {self.stats.known} cached translation units were "
                f"invalidated and reparsed, so this build cost close to a cold one at "
                f"~2.6 s per translation unit. The usual cause is a change to a "
                f"widely-included header, which invalidates every TU that includes it "
                f"(ARCH-001 §9)"
            )

        if self.stats.unreadable_deps:
            notes.append(
                f"{len(self.stats.unreadable_deps)} files in the include closure could not "
                f"be read and are keyed by path rather than by content; all are outside "
                f"the repository (e.g. {sorted(self.stats.unreadable_deps)[0]})"
            )
        return notes
