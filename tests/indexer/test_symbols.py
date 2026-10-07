"""`symbols`: in-memory symbol extraction and the C3 identity and diff rules."""

from devgraph.indexer.dispatch import _CODE_ROUTES
from devgraph.indexer.symbols import EXTRACTORS, decode_source, diff_symbols, extract_symbols, language_for


def keys(entries):
    return {(e["kind"], e["container"], e["name"]) for e in entries}


def test_every_code_route_has_an_extractor():
    assert set(EXTRACTORS) == set(_CODE_ROUTES.values())


def test_language_for_routes_by_suffix():
    assert language_for("web/util.ts") == "js"
    assert language_for("src/a.py") == "py"
    assert language_for("geo.hpp") == "cpp"
    assert language_for("README.md") is None
    assert language_for("notes.c") is None
    assert language_for("Makefile") is None


def test_decode_source_normalises_crlf_only():
    assert decode_source(b"a\r\nb\rc\n\xff") == "a\nb\rc\n\ufffd"


def test_body_slicing_ignores_form_feeds():
    old = "def a():\n    return 1\n\f\ndef b():\n    return 2\n"
    new = "def a():\n    return 10\n\f\ndef b():\n    return 2\n"
    b = next(s for s in extract_symbols("m.py", new) if s.name == "b")
    assert (b.start_line, b.end_line) == (4, 5)
    assert b.body == "def b():\n    return 2"
    added, removed, changed = diff_symbols(extract_symbols("m.py", old), extract_symbols("m.py", new))
    assert added == [] and removed == []
    assert keys(changed) == {("Function", None, "a")}


def test_container_is_not_self():
    got = {(s.kind, s.name): s.container for s in extract_symbols("m.py", "class A: pass\n")}
    assert got == {("Class", "A"): None}
    got = {(s.kind, s.name): s.container for s in extract_symbols("m.py", "class A: \n  def f(self): pass\n")}
    assert got == {("Class", "A"): None, ("Function", "f"): "A"}
    # A one-line method sharing the class's exact lines.
    src = "public class A { void f() {} }\n"
    got = {(s.kind, s.name): s.container for s in extract_symbols("A.java", src)}
    assert got == {("Class", "A"): None, ("Function", "f"): "A"}


def test_innermost_container_and_ordinals():
    src = "class A:\n    class B:\n        def f(self): pass\n        def f(self): pass\n    def f(self): pass\n"
    got = {(s.kind, s.container, s.name, s.ordinal) for s in extract_symbols("m.py", src)}
    assert got == {
        ("Class", None, "A", 0),
        ("Class", "A", "B", 0),
        ("Function", "B", "f", 0),
        ("Function", "B", "f", 1),
        ("Function", "A", "f", 0),
    }


def test_changed_entry_carries_both_sides_lines():
    old = "def f():\n    return 1\n"
    new = "\n" * 5 + "def f():\n    return 2\n"
    added, removed, changed = diff_symbols(extract_symbols("m.py", old), extract_symbols("m.py", new))
    assert added == removed == []
    assert changed == [
        {
            "kind": "Function",
            "name": "f",
            "container": None,
            "start_line": 6,
            "end_line": 7,
            "old_start_line": 1,
            "old_end_line": 2,
        }
    ]


def test_diff_lists_are_sorted_by_line_kind_name():
    new = "def z():\n    pass\nclass Y:\n    pass\ndef a():\n    pass\n"
    added, removed, changed = diff_symbols([], extract_symbols("m.py", new))
    assert [(e["start_line"], e["name"]) for e in added] == [(1, "z"), (3, "Y"), (5, "a")]
    assert removed == changed == []
    assert set(added[0]) == {"kind", "name", "container", "start_line", "end_line"}
