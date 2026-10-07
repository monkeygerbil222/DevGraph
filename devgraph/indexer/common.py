"""Shared graph-write dataclasses used by every per-language source extractor.

Split out of `devgraph/indexer/python/extractor.py` so the JS/TS, C#, C++,
Java, Rust, and Go extractors (Implementation Plan #8) share one definition
instead of each redeclaring the same three classes.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GraphNode:
    """A node to be upserted into the graph."""

    label: str
    repo_id: str
    name: str
    properties: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "repo_id": self.repo_id,
            "name": self.name,
            "properties": self.properties,
        }


@dataclass
class GraphRelationship:
    """A relationship to be upserted into the graph.

    from_file/to_file are an opt-in exactness constraint: when an endpoint's
    file is known for certain at extraction time (e.g. CONTAINS, where both
    ends are always the file currently being parsed), setting it makes the
    MATCH in engine.py require that node's `file` property too, instead of
    matching by name alone. Leave it None for endpoints whose file genuinely
    isn't knowable from syntax alone (e.g. a CALLS target, which could be
    defined anywhere in the repo) -- that keeps today's bare-name matching,
    ambiguity and all, since guessing wrong there would silently drop edges
    rather than just being imprecise.

    The source end is pinned for every edge out of one of the parsed file's
    own file-scoped nodes (see `own_edges`), so a `main` in one file never
    writes another file's calls. A source that isn't one of those (a Module,
    whose name is already its path, or a type defined in another file, such
    as a Rust `impl Trait for Foo`) stays bare.

    `origin` is the file that wrote the edge. The engine keeps every writer
    in the edge's sorted `origins` list, and a re-index of that file removes
    it from the edges it no longer writes, deleting an edge whose last writer
    is gone.
    """

    from_label: str
    from_name: str
    rel_type: str
    to_label: str
    to_name: str
    repo_id: str
    properties: dict | None = None
    from_file: str | None = None
    to_file: str | None = None
    origin: str | None = None

    def to_dict(self) -> dict:
        return {
            "from_label": self.from_label,
            "from_name": self.from_name,
            "rel_type": self.rel_type,
            "to_label": self.to_label,
            "to_name": self.to_name,
            "repo_id": self.repo_id,
            "properties": self.properties,
            "from_file": self.from_file,
            "to_file": self.to_file,
            "origin": self.origin,
        }


@dataclass
class ExtractionResult:
    """Result of parsing a source file: nodes and relationships."""

    nodes: list[GraphNode] = field(default_factory=list)
    relationships: list[GraphRelationship] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "relationships": [r.to_dict() for r in self.relationships],
        }


def own_edges(result: ExtractionResult, file_path: str) -> ExtractionResult:
    """Mark `result` as `file_path`'s own edges, in place, and return it.

    Pins the source end (`from_file`) of every relationship whose source
    `(label, name)` is one of the result's nodes with `file == file_path`,
    and stamps `origin = file_path` on every relationship. An already-set
    `from_file` is left as it is.
    """
    owned = {
        (node.label, node.name) for node in result.nodes if node.properties.get("file") == file_path
    }
    for rel in result.relationships:
        rel.origin = file_path
        if rel.from_file is None and (rel.from_label, rel.from_name) in owned:
            rel.from_file = file_path
    return result
