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
    STRUCTURAL = "structural"  # tier 2: body hash
    POSITIONAL = "positional"  # tier 3: (parent, kind, ordinal)
    SIMILARITY = "similarity"  # tier 4: weighted similarity above threshold


class Location(BaseModel):
    """Provenance only. Never identity -- an entity that moved is the same entity."""

    model_config = ConfigDict(frozen=True)

    file: str = Field(description="Repository-relative, forward slashes.")
    line: int
    end_line: int | None = None
    column: int | None = None

    @field_validator("file")
    @classmethod
    def _normalize_separators(cls, v: str) -> str:
        # Windows is the primary development platform; paths must not vary by host.
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
    """

    model_config = ConfigDict(extra="forbid")

    tus_total: int = 0
    tus: list[TuResult] = Field(default_factory=list)
    build_available: bool = False
    producers: list[str] = Field(default_factory=list)
    producer_version: str = ""
    notes: list[str] = Field(default_factory=list)

    @property
    def parsed(self) -> int:
        return sum(1 for t in self.tus if t.status is TuStatus.PARSED)

    @property
    def failed(self) -> int:
        return sum(1 for t in self.tus if t.status is TuStatus.FAILED)

    @property
    def is_complete(self) -> bool:
        return self.tus_total > 0 and self.parsed == self.tus_total

    def summary(self) -> str:
        if self.tus_total == 0:
            return "no translation units analyzed"
        if self.is_complete:
            return f"{self.parsed}/{self.tus_total} TUs parsed"
        pct = 100.0 * self.failed / self.tus_total
        return f"{self.parsed}/{self.tus_total} TUs parsed, {self.failed} failed ({pct:.1f}%)"


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
            for key in sorted(n.attrs):
                h.update(f"{key}={n.attrs[key]!r}".encode())
        for edge_id in sorted(e.id for e in self.edges):
            h.update(edge_id.encode())
        return h.hexdigest()


AnyId = Annotated[NodeId | EdgeId, Field(description="Subject of an event.")]
