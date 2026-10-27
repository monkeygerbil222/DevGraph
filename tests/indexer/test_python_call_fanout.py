"""Python name-only calls that can't reach their target (audit 12 item 1):
`super().m()` on a base outside the repository, a dunder or constructor
call on an untyped receiver, and a function nested in another function
(local to it, recorded as `nested` and by a match key) link no other file."""

import textwrap

from devgraph.graph.schema import LOCAL_KEY
from devgraph.indexer.python.extractor import extract_python_file


def calls(source: str, file_path: str = "app/main.py") -> set[tuple]:
    """(caller, callee, to_file, confidence) per CALLS row."""
    result = extract_python_file(textwrap.dedent(source), file_path, "repo")
    return {
        (r.from_name, r.to_name, r.to_file, r.properties["confidence"])
        for r in result.relationships
        if r.rel_type == "CALLS"
    }


def test_python_super_on_external_base_links_nothing():
    source = """\
        class Boom(Exception):
            def __init__(self, msg):
                super().__init__(msg)
                super().with_traceback(None)
        """
    assert calls(source) == set()


def test_super_outside_a_class_links_nothing():
    assert calls("def f():\n    super().run()\n") == set()


def test_super_on_an_in_file_or_imported_base_still_resolves():
    source = """\
        from lib.base import Base


        class Local:
            def run(self):
                pass


        class A(Local):
            def run(self):
                super().run()


        class B(Base):
            def run(self):
                super().run()
        """
    found = calls(source)
    assert ("run", "run", "app/main.py", "resolved") in found
    assert ("run", "run", "lib/base.py", "resolved") in found
    assert not any(conf == "name" for *_rest, conf in found)


def test_dunder_and_constructor_calls_on_an_untyped_receiver_link_nothing():
    source = """\
        def f(x, mod):
            x.__enter__()
            x.__init__()
            mod.Engine()
            x.run()
        """
    assert calls(source) == {("f", "run", None, "name")}


def _functions(source: str) -> dict[tuple[str, str], dict]:
    result = extract_python_file(textwrap.dedent(source), "app/main.py", "repo")
    return {(n.properties["owner"], n.name): n.properties for n in result.nodes if n.label == "Function"}


def test_a_function_defined_in_a_function_is_nested():
    found = _functions("""\
        def outer():
            def inner():
                pass

            class Local:
                def method(self):
                    pass
            return inner


        class Top:
            def method(self):
                def helper():
                    pass
        """)
    assert found[("outer", "inner")]["nested"] is True
    assert found[("outer.Local", "method")]["nested"] is True
    assert found[("Top.method", "helper")]["nested"] is True
    for key in [("", "outer"), ("Top", "method")]:
        assert "nested" not in found[key]
        assert LOCAL_KEY not in (found[key].get("mkeys") or [])
    assert LOCAL_KEY in found[("outer", "inner")]["mkeys"]
