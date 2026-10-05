"""The static-scan worker (spec §4.2, §5.3). Standard library only.

`scan.py` sends this file's source as `python -I -S -c <source>`, so it never
imports DevGraph. It reads one JSON request on stdin (`text`, `allowed_modules`,
`denied_names`, `feature_version`) and writes one JSON object on stdout:
`{"findings": [...], "literal_spans": [[start, end], ...]}`, or
`{"error": "<exception class>", "line": <int or null>}`.

The script is data here: it is parsed with `ast` and split with `tokenize`,
never compiled to a code object, imported or run.

Literal spans follow the contract in `devgraph/sandbox/display.py`: half-open
code point offsets of STRING, FSTRING_MIDDLE (and TSTRING_MIDDLE) and COMMENT
tokens, with line starts taken only from "\\n". A token whose range cannot be
confirmed against the text is left out, which the display treats as code.
"""

import ast
import io
import json
import sys
import tokenize

_MIDDLE_TYPES = {
    getattr(tokenize, name)
    for name in ("FSTRING_MIDDLE", "TSTRING_MIDDLE")
    if hasattr(tokenize, name)
}


def _dunder(name):
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def _module_allowed(module, allowed):
    return any(module == a or module.startswith(a + ".") for a in allowed)


def _is_derive(node):
    if not isinstance(node, ast.FunctionDef) or node.name != "derive":
        return False
    args = node.args
    params = args.posonlyargs + args.args
    return (
        len(params) == 1
        and params[0].arg == "ctx"
        and not (args.vararg or args.kwarg or args.kwonlyargs or args.defaults)
    )


def findings(tree, allowed, denied):
    found = []

    def add(rule, node, message):
        found.append(
            {"rule": rule, "line": getattr(node, "lineno", 0), "message": message}
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pathlib" or not _module_allowed(alias.name, allowed):
                    add("import", node, f"import of {ascii(alias.name)} is not allowed")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                add("import", node, "relative imports are not allowed")
                continue
            for alias in node.names:
                if module == "pathlib":
                    ok = alias.name.startswith("Pure")
                else:
                    ok = _module_allowed(module, allowed) or (
                        alias.name != "*"
                        and _module_allowed(f"{module}.{alias.name}", allowed)
                    )
                if not ok:
                    add(
                        "import",
                        node,
                        f"import of {ascii(module + '.' + alias.name)} is not allowed",
                    )
        elif isinstance(node, ast.Name):
            if node.id in denied:
                add("denied_name", node, f"use of {ascii(node.id)} is not allowed")
            if _dunder(node.id):
                add("dunder", node, f"dunder name {ascii(node.id)} is not allowed")
        elif isinstance(node, ast.Attribute):
            if _dunder(node.attr):
                add(
                    "dunder",
                    node,
                    f"dunder attribute {ascii(node.attr)} is not allowed",
                )
        elif isinstance(node, ast.While):
            if isinstance(node.test, ast.Constant) and node.test.value is True:
                add("while_true", node, "'while True:' is not allowed")
    if not any(_is_derive(node) for node in tree.body):
        add("no_derive", tree, "no top-level 'def derive(ctx):'")
    found.sort(key=lambda f: (f["line"], f["rule"], f["message"]))
    return found


def _middle_end(text, start, value):
    """Where an f-string middle token ends in `text`: each `{` or `}` in its value
    may stand for a doubled `{{` or `}}` in the source. None if it does not match."""
    i = start
    for char in value:
        if text[i : i + 1] != char:
            return None
        i += 2 if char in "{}" and text[i + 1 : i + 2] == char else 1
    return i


def literal_spans(text):
    line_starts = [0] + [i + 1 for i, char in enumerate(text) if char == "\n"]

    def offset(position):
        row, col = position
        return line_starts[row - 1] + col if 0 < row <= len(line_starts) else None

    spans = []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type in (tokenize.STRING, tokenize.COMMENT):
            start, end = offset(token.start), offset(token.end)
            if start is None or end is None or text[start:end] != token.string:
                continue
        elif token.type in _MIDDLE_TYPES:
            start = offset(token.start)
            end = None if start is None else _middle_end(text, start, token.string)
            if end is None:
                continue
        else:
            continue
        if end > start and (not spans or start >= spans[-1][1]):
            spans.append([start, end])
    return spans


def scan(request):
    text = request["text"]
    tree = ast.parse(text, feature_version=tuple(request["feature_version"]))
    return {
        "findings": findings(
            tree, tuple(request["allowed_modules"]), frozenset(request["denied_names"])
        ),
        "literal_spans": literal_spans(text),
    }


def main():
    try:
        result = scan(json.loads(sys.stdin.buffer.read().decode("utf-8")))
    except BaseException as exc:  # every failure is reported, then rejected by the host
        line = getattr(exc, "lineno", None) if isinstance(exc, SyntaxError) else None
        result = {
            "error": type(exc).__name__,
            "line": line if isinstance(line, int) else None,
        }
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
