"""Adapter contract (HLD §7.2).

Adapters do retrieval + normalization ONLY. They return the verbatim artifacts
plus a ClauseTree; the pipeline owns hashing, storage, diffing, and events.
Never re-generate or "clean up" source text (HLD principle 1) — clause text is
extracted verbatim from the official artifact.
"""
from dataclasses import dataclass, field
from datetime import date
from typing import Iterator, Protocol, runtime_checkable


@dataclass(frozen=True)
class SourceMeta:
    family_key: str
    family_name: str
    source_key: str
    name: str
    kind: str  # law|regulation|standard|guidance|form|agreement
    issuer: str
    jurisdiction: str
    license: str  # open|restricted
    canonical_url: str
    adapter: str
    license_ref: str = ""
    scope_charter: dict = field(default_factory=dict)


@dataclass
class ClauseNode:
    """One node of the ClauseTree: {ref, path, ordering, text, children[]}."""

    ref: str
    path: str
    ordering: int
    text: str
    children: list["ClauseNode"] = field(default_factory=list)

    def walk(self) -> Iterator["ClauseNode"]:
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass(frozen=True)
class Artifact:
    """A verbatim original as retrieved (stored immutably in the datalake)."""

    name: str
    content: bytes
    content_type: str = "application/octet-stream"


@dataclass
class FetchResult:
    version_label: str
    artifacts: list[Artifact]
    clause_tree: list[ClauseNode]
    effective_date: date | None = None


@dataclass(frozen=True)
class EffectRecord:
    """One official-citator record: an instrument affecting the root source."""

    affecting_key: str          # e.g. uksi/2019/1511
    affecting_name: str
    affecting_url: str
    relation: str               # amends|corrects|...
    kind: str = "regulation"    # sources.kind of the affecting instrument


@runtime_checkable
class Adapter(Protocol):
    key: str

    def meta(self) -> SourceMeta: ...

    def fetch(self, since_version: str | None = None) -> FetchResult | None:
        """Return the current version, or None if since_version is still current."""
        ...


class CitatorAdapter(Adapter, Protocol):
    """Adapters for sources with an official effects/relations feed."""

    def family_effects(self) -> list[EffectRecord]: ...


def flatten(tree: list[ClauseNode]) -> list[ClauseNode]:
    out: list[ClauseNode] = []
    for node in tree:
        out.extend(node.walk())
    return out
