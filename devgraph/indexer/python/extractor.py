"""Tree-sitter-based Python source-code extractor.

Parses a Python file using Tree-sitter's grammar-based parser and extracts:
  - Module (the file itself)
  - Classes with their base classes (inheritance)
  - Functions (at module level and within classes)
  - Imports (from/import statements)
  - Decorators (applied to classes/functions)

Tree-sitter over stdlib `ast` per the Implementation Plan: grammar-based,
incremental-parse friendly, and the only option that generalizes to
non-Python languages later.

Returns a structured result (list of dataclasses) describing nodes and
relationships that can be upserted into the graph via
GraphEngine.upsert_node/upsert_relationship. All nodes are keyed on
(repo_id, name) for idempotent incremental indexing.
"""

from __future__ import annotations

import logging
from pathlib import Path

import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser

from devgraph.indexer.common import ExtractionResult, GraphNode, GraphRelationship, own_edges
from devgraph.indexer.python.resolve import Bindings, absolute_module, relative_module

logger = logging.getLogger(__name__)

_PY_LANGUAGE = Language(tspython.language())


def _make_parser() -> Parser:
    return Parser(_PY_LANGUAGE)


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8")


def _dotted_name(node: Node, source: bytes) -> str:
    """Render an identifier/attribute node (e.g. `module.attr`) as dotted text."""
    if node.type in ("identifier", "dotted_name"):
        return _text(node, source)
    if node.type == "attribute":
        obj = node.child_by_field_name("object")
        attr = node.child_by_field_name("attribute")
        if obj is not None and attr is not None:
            return f"{_dotted_name(obj, source)}.{_text(attr, source)}"
    return _text(node, source)


def _extract_decorator_names(decorator_nodes: list[Node], source: bytes) -> list[str]:
    """Extract decorator names from a list of `decorator` nodes."""
    names = []
    for deco in decorator_nodes:
        # A `decorator` node wraps one child: identifier, attribute, or call.
        target = deco.named_children[0] if deco.named_children else None
        if target is None:
            continue
        if target.type == "call":
            func = target.child_by_field_name("function")
            if func is not None:
                names.append(_dotted_name(func, source))
        elif target.type in ("identifier", "attribute"):
            names.append(_dotted_name(target, source))
    return names


def _extract_call_targets(body: Node, source: bytes) -> list[str]:
    """Walk a function/method body for call expressions and return the
    callee's simple name (the identifier a Function node would be keyed on).

    - `foo()` -> 'foo'
    - `self.foo()` / `obj.foo()` -> 'foo' (the attribute name — this is a
      structural choice, not a type-resolved one: Tree-sitter has no type
      info, so 'self.foo()' and a free-standing 'foo()' both target the
      Function node named 'foo'. This intentionally over-links same-named
      methods across classes rather than under-linking everything, matching
      how find_callers/impact_analysis are meant to be used (a name-based,
      not fully type-resolved, call graph).
    - Nested/chained calls (`foo()()`, `a.b.c()`) resolve to the innermost
      call's callee name only.
    - Does not descend into nested function/class definitions — those are
      walked separately by the caller so calls are attributed to the
      correct enclosing scope, not hoisted to the outer function.
    """
    targets: list[str] = []

    def walk(node: Node) -> None:
        if node.type in ("function_definition", "class_definition"):
            return  # nested scope — attributed separately by the caller
        if node.type == "call":
            func = node.child_by_field_name("function")
            if func is not None:
                name = _callee_simple_name(func, source)
                if name:
                    targets.append(name)
        for child in node.children:
            walk(child)

    walk(body)
    return targets


def _callee_simple_name(func_node: Node, source: bytes) -> str | None:
    """Resolve a call expression's `function` field to a simple callee name."""
    if func_node.type == "identifier":
        return _text(func_node, source)
    if func_node.type == "attribute":
        attr = func_node.child_by_field_name("attribute")
        if attr is not None:
            return _text(attr, source)
    if func_node.type == "call":
        # Chained/immediately-invoked call, e.g. `get_handler()()` — resolve
        # to the outer call's own callee by recursing on its function field.
        inner_func = func_node.child_by_field_name("function")
        if inner_func is not None:
            return _callee_simple_name(inner_func, source)
    return None


def _extract_docstring(body: Node, source: bytes) -> str | None:
    """Extract a class/function/module's docstring, if its body's first
    statement is Python's docstring convention: an expression_statement
    wrapping a bare string node.
    """
    if not body.named_children:
        return None
    first = body.named_children[0]
    if first.type != "expression_statement" or not first.named_children:
        return None
    string_node = first.named_children[0]
    if string_node.type != "string":
        return None
    raw = _text(string_node, source)
    return _clean_docstring(raw)


def _clean_docstring(raw: str) -> str:
    """Strip a string literal's quote characters and common leading indentation."""
    text = raw.strip()
    for prefix in ('"""', "'''"):
        if text.startswith(prefix) and text.endswith(prefix) and len(text) >= 2 * len(prefix):
            text = text[len(prefix) : -len(prefix)]
            break
    else:
        for prefix in ('"', "'"):
            if text.startswith(prefix) and text.endswith(prefix) and len(text) >= 2:
                text = text[1:-1]
                break

    lines = text.split("\n")
    # Dedent using the minimum indentation of non-blank lines after the first
    # (PEP 257: the first line typically starts right after the opening quote).
    non_first = [l for l in lines[1:] if l.strip()]
    if non_first:
        indent = min(len(l) - len(l.lstrip()) for l in non_first)
        lines = [lines[0]] + [l[indent:] if len(l) >= indent else l for l in lines[1:]]
    return "\n".join(lines).strip()


def _docstring_summary(full_text: str, max_chars: int = 120) -> str:
    """Compute a PEP-257 summary line: first line up to the first blank line
    or terminating period, with a hard fallback truncation for docstrings
    that don't follow that convention.
    """
    first_para = full_text.split("\n\n", 1)[0].strip()
    first_line = first_para.split("\n", 1)[0].strip()

    period_idx = first_line.find(". ")
    if period_idx != -1:
        first_line = first_line[: period_idx + 1]

    if len(first_line) > max_chars:
        first_line = first_line[: max_chars - 3].rstrip() + "..."
    return first_line


def _extract_base_class_names(superclasses_node: Node | None, source: bytes) -> list[str]:
    """Extract base class names from an `argument_list` node under class_definition."""
    if superclasses_node is None:
        return []
    names = []
    for child in superclasses_node.named_children:
        if child.type in ("identifier", "attribute"):
            names.append(_dotted_name(child, source))
        elif child.type == "keyword_argument":
            # e.g. `class Foo(metaclass=Meta):` — not a real base class.
            continue
    return names


def _extract_imports(root: Node, source: bytes, file_path: str) -> Bindings:
    """Every import statement in the file, function-local ones included, as
    its bindings and IMPORTS targets (see resolve.py).

    `file_path` is the importing file's repo-relative path with forward
    slashes ('services/api/main.py', or 'main.py' at the repo root): absolute
    imports are tried under each of its ancestor directories and relative
    ones against its own directory. Each import targets every candidate
    file, `p.py` and `p/__init__.py`; only the ones that exist as Modules
    ever become edges. `from P import n` also targets the submodule `P.n`.
    """
    bindings = Bindings()
    current_dir = file_path.rsplit("/", 1)[0] if "/" in file_path else ""

    def walk(node: Node) -> None:
        if node.type == "import_statement":
            # import X [as Y] [, Z [as W]]
            for child in node.named_children:
                if child.type == "dotted_name":
                    bindings.add_import(_text(child, source), None, file_path)
                elif child.type == "aliased_import":
                    name_node = child.child_by_field_name("name")
                    alias_node = child.child_by_field_name("alias")
                    if name_node is not None:
                        alias = _text(alias_node, source) if alias_node else None
                        bindings.add_import(_text(name_node, source), alias, file_path)
        elif node.type == "import_from_statement":
            # from X import Y [as Z][, ...] | from . import Y | from X import *
            module_node = node.child_by_field_name("module_name")
            module_name = _text(module_node, source) if module_node else ""
            # tree_sitter's Python bindings hand back a fresh Node wrapper
            # object on every accessor call, so `child is module_node`
            # never matches even for the same underlying tree node —
            # compare byte spans instead to keep the module-name
            # dotted_name out of the imported names.
            module_span = (module_node.start_byte, module_node.end_byte) if module_node else None

            names: list[tuple[str, str | None]] = []
            star = False
            for child in node.named_children:
                if child.type == "dotted_name" and (child.start_byte, child.end_byte) != module_span:
                    names.append((_text(child, source), None))
                elif child.type == "aliased_import":
                    name_node = child.child_by_field_name("name")
                    alias_node = child.child_by_field_name("alias")
                    if name_node is not None:
                        names.append((_text(name_node, source), _text(alias_node, source) if alias_node else None))
                elif child.type == "wildcard_import":
                    star = True

            if module_name.startswith("."):
                bindings.add_from(relative_module(module_name, current_dir), names, star)
            elif module_name:
                bindings.add_from(absolute_module(module_name, file_path), names, star)

        for child in node.children:
            walk(child)

    walk(root)
    return bindings


def extract_python_file(source_code: str, file_path: str, repo_id: str) -> ExtractionResult:
    """Parse a Python file and extract nodes and relationships.

    Args:
        source_code: The Python source code as a string.
        file_path: The Module node's identity — should be the file's path
            relative to the repo root, using forward slashes (e.g.
            'services/api/main.py', or just 'main.py' for a repo-root file).
            A bare filename with no directory also works but loses the
            ability to resolve multi-level relative imports and risks
            colliding with a same-named file elsewhere in the repo (two
            files both named 'main.py' in different directories would
            otherwise merge into one Module node) — index_file() computes
            the correct repo-relative form automatically.
        repo_id: Repository ID for scoping nodes.

    Returns:
        ExtractionResult containing lists of nodes and relationships. On a
        syntax error, Tree-sitter still returns a best-effort tree (it uses
        ERROR nodes rather than raising), so partial results are extracted
        and the error is logged rather than the whole file being skipped.
    """
    result = ExtractionResult()
    source_bytes = source_code.encode("utf-8")

    parser = _make_parser()
    tree = parser.parse(source_bytes)
    root = tree.root_node

    if root.has_error:
        logger.warning(f"Syntax errors while parsing {file_path}; extracting best-effort result")

    # Create a Module node for the file itself.
    module_properties: dict = {"type": "module", "source_file": file_path}
    module_docstring = _extract_docstring(root, source_bytes)
    if module_docstring:
        module_properties["description"] = _docstring_summary(module_docstring)
        module_properties["docstring_full"] = module_docstring
    module_node = GraphNode(
        label="Module",
        repo_id=repo_id,
        name=file_path,
        properties=module_properties,
    )
    result.nodes.append(module_node)

    # IMPORTS edges to every candidate file of every import (see resolve.py).
    # file_path is expected to be the repo-relative path with forward
    # slashes (e.g. 'services/api/main.py'), which the candidates hang off.
    # A package's `from . import x` names its own __init__.py, never an edge.
    bindings = _extract_imports(root, source_bytes, file_path)
    for target in sorted(bindings.targets - {file_path}):
        result.relationships.append(
            GraphRelationship(
                from_label="Module",
                from_name=file_path,
                rel_type="IMPORTS",
                to_label="Module",
                to_name=target,
                repo_id=repo_id,
            )
        )

    def _emit_call(
        caller_name: str, caller_label: str, target_name: str, caller_class: str | None = None
    ) -> None:
        # caller_class records the enclosing class of a method-body call (None
        # for module-level/free-function calls) so find_callers can optionally
        # narrow results via scope_to_class — an opt-in query-time filter,
        # not a change to which edges get emitted (see Implementation Plan #3,
        # Item 2: a same-file MRO-suppression filter turned out to require a
        # Function-node schema change to express, so this ships as query-time
        # narrowing on data recorded at extraction time instead).
        properties = {"caller_class": caller_class} if caller_class else None
        result.relationships.append(
            GraphRelationship(
                from_label=caller_label,
                from_name=caller_name,
                rel_type="CALLS",
                to_label="Function",
                to_name=target_name,
                repo_id=repo_id,
                properties=properties,
            )
        )

    def visit_block(block: Node, parent_name: str | None, parent_label: str) -> None:
        """Visit statements in a block (module body, class body, function body)."""
        for node in block.named_children:
            if node.type == "class_definition":
                _visit_class(node, parent_name, parent_label)
            elif node.type in ("function_definition",):
                _visit_function(node, parent_name, parent_label)
            elif node.type == "decorated_definition":
                # decorated_definition wraps decorator(s) + the actual definition.
                definition = node.child_by_field_name("definition")
                decorators = [c for c in node.named_children if c.type == "decorator"]
                if definition is not None and definition.type == "class_definition":
                    _visit_class(definition, parent_name, parent_label, extra_decorators=decorators)
                elif definition is not None and definition.type == "function_definition":
                    _visit_function(definition, parent_name, parent_label, extra_decorators=decorators)
            elif node.type not in ("class_definition", "function_definition", "decorated_definition"):
                # Module/class-body-level statement (not a def) — attribute
                # any call expressions in it to the enclosing scope (usually
                # the Module, for top-level script code / constant setup).
                caller_class = parent_name if parent_label == "Class" else None
                for target in _extract_call_targets(node, source_bytes):
                    _emit_call(
                        parent_name if parent_name else file_path, parent_label, target, caller_class
                    )

    def _visit_class(
        node: Node,
        parent_name: str | None,
        parent_label: str,
        extra_decorators: list[Node] | None = None,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        class_name = _text(name_node, source_bytes)

        decorator_nodes = extra_decorators or []
        decorators = _extract_decorator_names(decorator_nodes, source_bytes)

        superclasses_node = node.child_by_field_name("superclasses")
        base_classes = _extract_base_class_names(superclasses_node, source_bytes)

        class_properties: dict = {
            "type": "class",
            "decorators": decorators,
            "file": file_path,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
        }
        body_node = node.child_by_field_name("body")
        if body_node is not None:
            docstring = _extract_docstring(body_node, source_bytes)
            if docstring:
                class_properties["description"] = _docstring_summary(docstring)
                class_properties["docstring_full"] = docstring

        result.nodes.append(
            GraphNode(
                label="Class",
                repo_id=repo_id,
                name=class_name,
                properties=class_properties,
            )
        )

        result.relationships.append(
            GraphRelationship(
                from_label=parent_label,
                from_name=parent_name if parent_name else file_path,
                rel_type="CONTAINS",
                to_label="Class",
                to_name=class_name,
                repo_id=repo_id,
                from_file=file_path if parent_label == "Class" else None,
                to_file=file_path,
            )
        )

        for base_class in base_classes:
            result.relationships.append(
                GraphRelationship(
                    from_label="Class",
                    from_name=class_name,
                    rel_type="EXTENDS",
                    to_label="Class",
                    to_name=base_class,
                    repo_id=repo_id,
                )
            )

        if body_node is not None:
            visit_block(body_node, class_name, "Class")

    def _visit_function(
        node: Node,
        parent_name: str | None,
        parent_label: str,
        extra_decorators: list[Node] | None = None,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        func_name = _text(name_node, source_bytes)

        decorator_nodes = extra_decorators or []
        decorators = _extract_decorator_names(decorator_nodes, source_bytes)

        func_properties: dict = {
            "type": "function",
            "decorators": decorators,
            "file": file_path,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
        }
        body_node = node.child_by_field_name("body")
        if body_node is not None:
            docstring = _extract_docstring(body_node, source_bytes)
            if docstring:
                func_properties["description"] = _docstring_summary(docstring)
                func_properties["docstring_full"] = docstring

        result.nodes.append(
            GraphNode(
                label="Function",
                repo_id=repo_id,
                name=func_name,
                properties=func_properties,
            )
        )

        result.relationships.append(
            GraphRelationship(
                from_label=parent_label,
                from_name=parent_name if parent_name else file_path,
                rel_type="CONTAINS",
                to_label="Function",
                to_name=func_name,
                repo_id=repo_id,
                from_file=file_path if parent_label == "Class" else None,
                to_file=file_path,
            )
        )

        # Nested function definitions (rare, but Tree-sitter walks these fine),
        # and CALLS edges for every call expression made directly in this
        # function's body (not inside a nested def — _extract_call_targets
        # stops descending at nested function/class scopes so those calls
        # get attributed to the nested function itself, not hoisted here).
        if body_node is not None:
            caller_class = parent_name if parent_label == "Class" else None
            for target in _extract_call_targets(body_node, source_bytes):
                _emit_call(func_name, "Function", target, caller_class)

            for child in body_node.named_children:
                if child.type == "function_definition":
                    _visit_function(child, func_name, "Function")
                elif child.type == "decorated_definition":
                    definition = child.child_by_field_name("definition")
                    decos = [c for c in child.named_children if c.type == "decorator"]
                    if definition is not None and definition.type == "function_definition":
                        _visit_function(definition, func_name, "Function", extra_decorators=decos)

    visit_block(root, None, "Module")

    return own_edges(result, file_path)


def index_file(
    engine,
    repo_id: str,
    file_path: str | Path,
    repo_root: str | Path | None = None,
) -> None:
    """Extract Python file and upsert results into the graph.

    This is a thin wrapper that calls extract_python_file() and then
    upserts each node and relationship via the GraphEngine.

    Args:
        engine: A GraphEngine instance.
        repo_id: Repository ID for scoping.
        file_path: Path to the Python file to index.
        repo_root: The repository's root directory. When given, the Module
            node is keyed by file_path's path relative to repo_root (forward
            slashes), which is what makes multi-level relative imports
            resolve correctly and prevents same-named files in different
            directories from colliding into one Module node. When omitted
            (e.g. a caller indexing a standalone file with no repo context,
            as some tests do), falls back to the bare filename — matching
            this function's original behavior, so existing single-file
            callers keep working, just without the collision/relative-import
            benefits multi-file repos get from passing repo_root.

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    source_code = file_path.read_text(encoding="utf-8")

    if repo_root is not None:
        try:
            rel = file_path.resolve().relative_to(Path(repo_root).resolve())
            module_name = rel.as_posix()
        except ValueError:
            module_name = file_path.name  # file_path wasn't actually under repo_root
    else:
        module_name = file_path.name

    result = extract_python_file(source_code, module_name, repo_id)

    engine.upsert_nodes([node.to_dict() for node in result.nodes])
    engine.upsert_relationships([rel.to_dict() for rel in result.relationships])
