"""The intermediate representation: a semantic model of the codebase at one commit.

ARCH-001 §4. The governing constraint is that every field is either **identity**,
**diffable state**, or **provenance**. A field that can be neither compared across commits
nor used to locate a change in source does not belong here.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Annotated, NewType

from pydantic import BaseModel, ConfigDict, Field, field_validator

NodeId = NewType("NodeId", str)
EdgeId = NewType("EdgeId", str)

#: Attributes are deliberately scalar. ARCH-001 §4.3 -- an attribute diff is a first-class
#: event, produced mechanically. Nested attributes would need bespoke diff logic per node
#: kind, which is how these systems rot.
type AttrValue = str | int | float | bool | None


def usr_file(node_id: str) -> str | None:
    """The file component of a file-scoped USR, or None for an external-linkage one.

    Clang writes the source file into the USR of anything with internal linkage -- statics,
    anonymous-namespace members -- because those entities are only distinguishable by where
    they live: `c:util.cpp@F@helper#` against `c:@F@helper#`. It writes the *file name*, not
    the path, so two worktrees at different absolute paths still agree; a rename that changes
    the name does not.

    This is the only place the *format* of a USR is interpreted; everywhere else it is an
    opaque key and should stay that way. Anything that needs to know about internal linkage
    asks here rather than growing its own idea of what a USR looks like.

    **It lives in `ir.py` for a cache-correctness reason, not a tidiness one.** The producer
    keys its stored partials on `_producer_code_digest`, which hashes exactly two modules --
    `ir.py` and `extract.py`. `extract.py` decides `Node.tu` by asking this function, so a
    helper that changed the answer while living anywhere else would rewrite every partial
    while leaving every cache key byte-identical: the stale hit ARCH-001 §9 calls the worst
    failure available here. `match.declared` re-exports it, which is where it used to live
    and where its callers still expect it.
    """
    if not node_id.startswith("c:"):
        return None
    rest = node_id[2:]
    # `c:@...` is external linkage; a bare `c:` is not a declaration USR at all.
    if not rest or rest.startswith("@"):
        return None
    name, sep, _ = rest.partition("@")
    return name if sep and name else None


class NodeKind(StrEnum):
    NAMESPACE = "namespace"
    CLASS = "class"
    STRUCT = "struct"
    UNION = "union"
    FUNCTION = "function"
    METHOD = "method"
    FIELD = "field"
    ENUM = "enum"
    ENUMERATOR = "enumerator"
    ALIAS = "alias"
    VARIABLE = "variable"
    TEMPLATE = "template"
    FILE = "file"
    TARGET = "target"


class EdgeKind(StrEnum):
    """Subset of the CodeAtlas C++ relation ontology (RESEARCH-ONT-003), chosen for
    diff-relevance. The full 61-relation vocabulary is the reference, not the target."""

    CONTAINS = "contains"
    DECLARES = "declares"
    DEFINES = "defines"
    INHERITS = "inherits"
    OVERRIDES = "overrides"
    CALLS = "calls"
    INSTANTIATES = "instantiates"
    HAS_A = "has-a"
    HOLDS_A = "holds-a"
    USES_IN_INTERFACE = "uses-in-interface"
    USES_IN_IMPLEMENTATION = "uses-in-implementation"
    INCLUDES = "includes"
    NEEDS_SYMBOL = "needs-symbol"
    RESOLVES_TO = "resolves-to"


class MatchTier(StrEnum):
    """Which identity tier produced a cross-commit match (ARCH-001 §5).

    Recorded on the match and propagated into the confidence of every event derived from
    it, so uncertainty is never laundered into a confident claim.
    """

    DECLARED = "declared"  # tier 1: USR
    NAME = "name"  # functions and methods left by tier 1: kind + qualified name (ADR-036)
    STRUCTURAL = "structural"  # tier 2: body hash
    POSITIONAL = "positional"  # tier 3: (parent, kind, ordinal)
    SIMILARITY = "similarity"  # tier 4: weighted similarity above threshold


class Location(BaseModel):
    """Provenance only. Never identity -- an entity that moved is the same entity."""

    model_config = ConfigDict(frozen=True)

    file: str = Field(description="Repository-relative, forward slashes.")
    line: int = Field(description="Where the entity's name is, or the site itself for an edge.")
    end_line: int | None = None
    column: int | None = None
    start_line: int | None = Field(
        default=None,
        description="Where the declaration's text begins, when that is before `line`: a "
        "`template <...>` header, an attribute, a return type on the line above the name. "
        "A change on those lines is a change to this declaration, and a span that started "
        "at the name would point nowhere for it.",
    )

    @property
    def first_line(self) -> int:
        """The first line of the span: `start_line` where it is known and earlier."""
        return min(self.line, self.start_line) if self.start_line is not None else self.line

    @property
    def sort_key(self) -> tuple[str, int, int, int, int]:
        """A total order over locations, so a set of them can be stored in one order."""
        return (self.file, self.first_line, self.line, self.end_line or 0, self.column or 0)

    @field_validator("file")
    @classmethod
    def _normalize_separators(cls, v: str) -> str:
        # A path is provenance, and provenance that varies by host is not comparable.
        # Compile databases travel between machines and clang echoes back whatever
        # separator it was given, so normalize rather than trust the input.
        return v.replace("\\", "/")


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: NodeId
    kind: NodeKind
    name: str
    qualified_name: str

    signature: str | None = Field(
        default=None,
        description="Normalized: typedefs resolved, defaults included, cv/ref qualifiers "
        "explicit. Required for tier-1 identity to distinguish overloads.",
    )
    symbol: str | None = Field(
        default=None,
        description="Mangled symbol name where the entity has linkage. The join key to "
        "abidiff/bloaty/nm/coverage (ARCH-001 §8). Populated from v0 even though the "
        "evidence layer is deferred, because retrofitting invalidates every cached model.",
    )

    attrs: dict[str, AttrValue] = Field(default_factory=dict)
    body_hash: str | None = Field(
        default=None,
        description="Normalized subtree hash, insensitive to formatting and comments. "
        "Kept separate from attrs: an attribute change has a specific known meaning, a "
        "body change only means 'look closer'.",
    )

    location: Location | None = None
    sites: tuple[Location, ...] = Field(
        default=(),
        description="Every place this entity is declared or defined -- a header declaration "
        "and the definition in a source file, each redeclaration -- sorted and deduplicated. "
        "Provenance only, like `location`, which is one of them. **The one field merged as a "
        "union** rather than taken from the observation that wins (`extract.merge_node`): a "
        "change event about the entity has to point at every line that declares it, or a "
        "reviewer cannot get from an edited declaration in a header to the event it caused "
        "(ARCH-001 §6.2). Never compared, ranked or hashed.",
    )
    tu: str | None = Field(
        default=None,
        description="Defining translation unit. Identity-relevant for internal linkage, "
        "where USRs incorporate the file path (ARCH-001 §5).",
    )

    @field_validator("attrs", mode="before")
    @classmethod
    def _attrs_must_be_scalar(cls, v: object) -> object:
        # `before` so the diagnostic names the offending key and cites the rule, rather
        # than surfacing pydantic's generic union-mismatch error.
        if isinstance(v, dict):
            for key, value in v.items():
                if not isinstance(value, str | int | float | bool | type(None)):
                    raise ValueError(
                        f"attrs[{key!r}] is {type(value).__name__}; attributes must be scalar "
                        f"so that attribute diffs stay mechanical (ARCH-001 §4.3)"
                    )
        return v


class Edge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: NodeId
    dst: NodeId
    kind: EdgeKind
    attrs: dict[str, AttrValue] = Field(default_factory=dict)

    locations: list[Location] = Field(
        default_factory=list,
        description="Every site producing this edge. A function calling another twice is "
        "one edge with two locations, not two edges -- dedup is a correctness "
        "requirement, not an optimization (ARCH-001 §5).",
    )

    @property
    def id(self) -> EdgeId:
        return EdgeId(f"{self.src}|{self.kind.value}|{self.dst}")


class TuStatus(StrEnum):
    PARSED = "parsed"
    FAILED = "failed"
    SKIPPED = "skipped"


class TuResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    status: TuStatus
    detail: str | None = None
    cache_hit: bool = False


class Completeness(BaseModel):
    """How the model was built (ARCH-001 §4.4).

    Not diagnostics. This is load-bearing input to confidence, and any output built on an
    incomplete model must say so at the top rather than in a log.

    **A model is incomplete in two unrelated ways, and conflating them is a mistake in
    either direction.**

    *Coverage* is what `is_complete` measures: sources the model set out to read and could
    not. A TU that failed to parse takes its declarations with it, so an entity that is
    genuinely present reads as absent -- ARCH-001 §11's "absence of evidence into apparent
    evidence of absence", exactly. It taints every claim on that side, including claims
    about entities in the TUs that did parse, because what is missing may be what would
    have contradicted them.

    *Boundary* is what `edges_closed` measures: relations whose endpoint no declaration in this
    repository owns, dropped by the builder because they cannot be nodes. Calls into `std::`,
    into libc, and into implicit special members nobody wrote. This is not a hole in coverage;
    it is where the modelled world ends, and with full coverage every dropped edge points
    outside it by construction. It is *expected*, it is large on any real repository
    (`produce/build.py` gives fmt's figure), and it must be stated as a number on every run
    rather than raised as an alarm on every run -- an alarm that never turns off is one nobody
    reads.

    Both are carried as counts rather than as prose, because a consumer has to be able to
    state the fact numerically. ARCH-001 §10 makes the JSON the product, and a sentence in
    `notes` is a fact only a human who reads English can extract.
    """

    model_config = ConfigDict(extra="forbid")

    tus_total: int = 0
    tus: list[TuResult] = Field(default_factory=list)

    edges_total: int = Field(
        default=0,
        description="Distinct edges the builder considered: resolved plus dropped. Distinct, "
        "not arrivals -- fmt yields 224,401 arrivals for 26,071 distinct edges, and a ratio "
        "over arrivals would measure how often headers are re-included.",
    )
    edges_dropped: int = Field(
        default=0,
        description="Distinct edges dropped because an endpoint resolves to no declared "
        "node (ARCH-001 §11). The boundary of the model, quantified -- see the class "
        "docstring on why this is not the same kind of incompleteness as a failed TU.",
    )

    build_available: bool = False
    producers: list[str] = Field(default_factory=list)
    producer_version: str = ""
    language_modes: list[str] = Field(
        default_factory=list,
        description="The distinct `-std=` values the compile database gives the C++ TUs, "
        "sorted. Empty where the build states none, which is itself worth seeing -- the TUs "
        "are then parsed at the parser's default rather than the project's. A determinant of "
        "the AST that nothing else in this record captures: raising CMAKE_CXX_STANDARD is one "
        "line of diff and changes which declarations exist, and in the delta that is "
        "indistinguishable from new code (ARCH-001 §11, §12.1).",
    )
    not_visible: list[str] = Field(
        default_factory=list,
        description="What no model from this producer can show, whatever the source: "
        "implicit special members, which libclang does not expose, and macros (ADR-029). "
        "The same on every run, and stated on every run, so a change the model cannot see "
        "reads as unseen rather than as absent (ARCH-001 §11).",
    )
    notes: list[str] = Field(
        default_factory=list,
        description="Anything the builder wants to say that has no field of its own. Not "
        "the channel for a measured quantity: a count that lives only here is unparseable "
        "by every consumer that is not a human (ARCH-001 §10).",
    )

    @property
    def parsed(self) -> int:
        return sum(1 for t in self.tus if t.status is TuStatus.PARSED)

    @property
    def failed(self) -> int:
        return sum(1 for t in self.tus if t.status is TuStatus.FAILED)

    @property
    def is_complete(self) -> bool:
        """**Source coverage, and nothing else** -- every TU attempted was parsed.

        Deliberately not widened to include `edges_closed`. Boundary residue is present on
        every real repository, so folding it in here would make `is_complete` False on
        every run and `Confidence.models_complete` False on every event, which turns the
        one flag rank (§7) uses to promote uncertain events into a constant. The residue is
        reported by its own numbers instead; the name of this one says what it counts.
        """
        return self.tus_total > 0 and self.parsed == self.tus_total

    @property
    def edges_resolved(self) -> int:
        return self.edges_total - self.edges_dropped

    @property
    def edges_closed(self) -> bool:
        """Whether every edge the builder saw landed on two declared nodes.

        True on a repository that calls nothing it does not declare -- a fixture, in
        practice. False on all of fmt, spdlog and anything that includes a standard header,
        which is why this is a *fact to state*, not a *warning to raise*.
        """
        return self.edges_dropped == 0

    @property
    def edge_residue(self) -> float:
        """Dropped share of the distinct edges, 0.0 when nothing was considered.

        A ratio rather than a count because the count alone is unreadable: one dropped count is
        a catastrophe against a small graph and the ordinary cost of doing business against a
        large one.
        """
        return (self.edges_dropped / self.edges_total) if self.edges_total else 0.0

    def summary(self) -> str:
        """One line stating both kinds of incompleteness, numerically (ARCH-001 §11)."""
        if self.tus_total == 0:
            coverage = "no translation units analyzed"
        elif self.is_complete:
            coverage = f"{self.parsed}/{self.tus_total} TUs parsed"
        else:
            pct = 100.0 * self.failed / self.tus_total
            coverage = (
                f"{self.parsed}/{self.tus_total} TUs parsed, {self.failed} failed ({pct:.1f}%)"
            )
        if self.edges_closed:
            return coverage
        return (
            f"{coverage}; {self.edges_dropped:,}/{self.edges_total:,} edges "
            f"({100.0 * self.edge_residue:.1f}%) lead outside the model"
        )


class Model(BaseModel):
    """The semantic model at one revision."""

    model_config = ConfigDict(extra="forbid")

    rev: str = Field(description="Resolved commit SHA, not a ref name.")
    nodes: dict[NodeId, Node] = Field(default_factory=dict)
    edges: list[Edge] = Field(default_factory=list)
    completeness: Completeness = Field(default_factory=Completeness)

    def add_node(self, node: Node) -> None:
        self.nodes[node.id] = node

    def edges_by_kind(self, kind: EdgeKind) -> list[Edge]:
        return [e for e in self.edges if e.kind is kind]

    def fingerprint(self) -> str:
        """Content hash over identity and diffable state -- excludes provenance.

        Two models with the same fingerprint produce an empty delta, which makes this a
        cheap correctness check on the whole pipeline.
        """
        h = hashlib.blake2b(digest_size=16)
        for node_id in sorted(self.nodes):
            n = self.nodes[node_id]
            h.update(node_id.encode())
            h.update(n.kind.value.encode())
            h.update((n.signature or "").encode())
            h.update((n.body_hash or "").encode())
            # ARCH-001 §5: the TU is identity exactly where the USR is file-scoped, and this
            # hashes it under the same condition `DeclaredTier.key` applies -- one rule about
            # when the TU counts, in two places that must not disagree. Without it the
            # docstring's claim above is false: two same-named statics in different source
            # files share a USR, so moving one between files changes identity and nothing
            # this hashes, and the fingerprints would match while the delta did not.
            if usr_file(node_id) is not None:
                h.update((n.tu or "").encode())
            for key in sorted(n.attrs):
                h.update(f"{key}={n.attrs[key]!r}".encode())
        for edge_id in sorted(e.id for e in self.edges):
            h.update(edge_id.encode())
        return h.hexdigest()


AnyId = Annotated[NodeId | EdgeId, Field(description="Subject of an event.")]
