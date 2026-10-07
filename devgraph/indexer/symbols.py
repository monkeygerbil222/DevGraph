"""Code symbols (functions and classes) of one file's text, extracted in memory, and their diff.

Every code route `index_paths` uses runs here on a string: nothing is read from
disk and nothing is written. See
docs/superpowers/specs/2026-10-08-compare-branches-design.md (C3, C6).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from devgraph.indexer.common import ExtractionResult
from devgraph.indexer.cpp.extractor import extract_cpp_file
from devgraph.indexer.csharp.extractor import extract_csharp_file
from devgraph.indexer.dispatch import _CODE_ROUTES
from devgraph.indexer.go.extractor import extract_go_file
from devgraph.indexer.java.extractor import extract_java_file
from devgraph.indexer.jsts.extractor import extract_js_file
from devgraph.indexer.kotlin.extractor import extract_kotlin_file
from devgraph.indexer.python.extractor import extract_python_file
from devgraph.indexer.rust.extractor import extract_rust_file

# One entry per `_CODE_ROUTES` value; a test fails if a route has none.
EXTRACTORS: dict[str, Callable[[str, str], ExtractionResult]] = {
    "py": lambda source, path: extract_python_file(source, path, ""),
    "js": lambda source, path: extract_js_file(source, path, ""),
    "cs": lambda source, path: extract_csharp_file(source, path, ""),
    "cpp": lambda source, path: extract_cpp_file(source, path, ""),
    "java": lambda source, path: extract_java_file(source, path, ""),
    "rs": lambda source, path: extract_rust_file(source, path, ""),
    "kt": lambda source, path: extract_kotlin_file(source, path, ""),
    # The module path only shapes IMPORTS targets, which are dropped here.
    "go": lambda source, path: extract_go_file(source, path, "", module_path=None),
}

_SYMBOL_LABELS = ("Function", "Class")


@dataclass
class Symbol:
    kind: str  # "Function" or "Class"
    name: str
    container: str | None  # the innermost enclosing class's name
    ordinal: int  # position among symbols with the same (kind, container, name)
    start_line: int
    end_line: int
    body: str

    @property
    def key(self) -> tuple[str, str | None, str, int]:
        return (self.kind, self.container, self.name, self.ordinal)


def language_for(path: str) -> str | None:
    """The code route of a repo-relative path, by its suffix, or `None`."""
    return _CODE_ROUTES.get(PurePosixPath(path).suffix)


def decode_source(data: bytes) -> str:
    """Blob bytes as the indexer would see the file: UTF-8 with replacement, CRLF as LF."""
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n")


def extract_symbols(path: str, text: str) -> list[Symbol]:
    """The functions and classes of `text` (already normalised), in source order.

    Raises whatever the language's extractor raises; `path` must have a code route."""
    result = EXTRACTORS[language_for(path)](text, path)
    nodes = [
        n
        for n in result.nodes
        if n.label in _SYMBOL_LABELS
        and n.properties.get("start_line") is not None
        and n.properties.get("end_line") is not None
    ]
    nodes.sort(key=lambda n: (n.properties["start_line"], n.properties["end_line"]))
    lines = text.split("\n")  # tree-sitter's rows; splitlines() would also break on \f, \x85, ...
    classes = [n for n in nodes if n.label == "Class"]
    seen: dict[tuple[str, str | None, str], int] = {}
    symbols = []
    for node in nodes:
        start, end = node.properties["start_line"], node.properties["end_line"]
        enclosing = [
            c
            for c in classes
            if c is not node and c.properties["start_line"] <= start and end <= c.properties["end_line"]
        ]
        container = None
        if enclosing:
            inner = min(enclosing, key=lambda c: (-c.properties["start_line"], c.properties["end_line"]))
            container = inner.name
        group = (node.label, container, node.name)
        ordinal = seen.get(group, 0)
        seen[group] = ordinal + 1
        symbols.append(Symbol(node.label, node.name, container, ordinal, start, end, "\n".join(lines[start - 1 : end])))
    return symbols


def _entry(symbol: Symbol) -> dict:
    return {
        "kind": symbol.kind,
        "name": symbol.name,
        "container": symbol.container,
        "start_line": symbol.start_line,
        "end_line": symbol.end_line,
    }


def _order(entry: dict) -> tuple:
    return (entry["start_line"], entry["kind"], entry["name"])


def diff_symbols(old: list[Symbol], new: list[Symbol]) -> tuple[list[dict], list[dict], list[dict]]:
    """(added, removed, changed) entries, paired by (kind, container, name, ordinal).

    `added` and `changed` carry head-side lines, `removed` base-side ones; a `changed`
    entry adds `old_start_line`/`old_end_line`. A symbol with the same body on both
    sides is not reported, wherever it moved. Each list is sorted by (line, kind, name)."""
    before = {s.key: s for s in old}
    after = {s.key: s for s in new}
    added = [_entry(s) for key, s in after.items() if key not in before]
    removed = [_entry(s) for key, s in before.items() if key not in after]
    changed = [
        {**_entry(s), "old_start_line": before[key].start_line, "old_end_line": before[key].end_line}
        for key, s in after.items()
        if key in before and before[key].body != s.body
    ]
    return sorted(added, key=_order), sorted(removed, key=_order), sorted(changed, key=_order)
