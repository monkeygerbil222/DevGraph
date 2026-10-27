"""A Python decorator is called by the definition it decorates (audit 12
item 5 and its review), `via: "decorator"`, resolved like a call in the
scope Python evaluates it in but never by bare name (an untyped
`@app.get(...)` links no `get`). A name an `__init__.py` re-exports under
an alias (`from .util import other as renamed`) reaches the original, the
`__init__.py` being Python resolver configuration."""

import textwrap

from devgraph.indexer.python.extractor import extract_python_file
from devgraph.indexer.resolver_config import ResolverConfig, fingerprints, touches_config


def rows(source: str, file_path: str = "app/main.py", config=None):
    result = extract_python_file(textwrap.dedent(source), file_path, "repo", config)
    return [r for r in result.relationships if r.rel_type == "CALLS"]


def calls(source: str, file_path: str = "app/main.py", config=None) -> set[tuple]:
    """(caller label, caller, callee, to_file, confidence) per CALLS row."""
    return {
        (r.from_label, r.from_name, r.to_name, r.to_file, r.properties["confidence"])
        for r in rows(source, file_path, config)
    }


def test_decorator_usage_is_a_caller():
    source = """\
        import app.deco as deco
        from app import retry


        def plain(fn):
            return fn


        @plain
        def a():
            pass


        @deco.plain
        def b():
            pass


        @retry(3)
        def c():
            pass


        class Job:
            @plain
            def run(self):
                pass


        def outer():
            @plain
            def inner():
                pass
        """
    found = calls(source)
    assert ("Function", "a", "plain", "app/main.py", "resolved") in found
    assert ("Function", "b", "plain", "app/deco.py", "resolved") in found
    assert ("Function", "c", "retry", "app/__init__.py", "resolved") in found
    assert ("Function", "c", "retry", "app/", "package") in found
    assert ("Function", "run", "plain", "app/main.py", "resolved") in found
    assert ("Function", "inner", "plain", "app/main.py", "resolved") in found
    # No scope calls them: the definitions do.
    assert not [row for row in found if row[0] in ("Module", "Class") or row[1] == "outer"]
    decorated = [r for r in rows(source) if r.to_name in ("plain", "retry")]
    assert all(r.properties["via"] == "decorator" for r in decorated)
    (run,) = {(r.from_owner, r.properties.get("caller_class")) for r in decorated if r.from_name == "run"}
    assert run == ("Job", "Job")


def test_a_decorated_class_calls_its_decorator():
    found = rows("def register(cls):\n    return cls\n\n\n@register\nclass Plugin:\n    pass\n")
    assert [(r.from_label, r.from_name, r.to_name, r.properties["via"]) for r in found] == [
        ("Class", "Plugin", "register", "decorator"),
    ]


def test_a_decorator_the_definition_also_calls_is_one_plain_call():
    source = """\
        def plain(fn):
            return fn


        @plain
        def a():
            plain(None)
        """
    (row,) = [r for r in rows(source) if r.to_name == "plain"]
    assert row.from_name == "a" and "via" not in row.properties


def test_an_unresolved_or_builtin_decorator_links_nothing():
    source = """\
        from fastapi import FastAPI

        app = FastAPI()


        @app.get("/items")
        def items():
            pass


        class Engine:
            @property
            def ready(self):
                return True

            @ready.setter
            def ready(self, value):
                pass

            @staticmethod
            def version():
                return 1
        """
    assert not [row for row in calls(source) if row[4] == "name"]
    assert not [row for row in calls(source) if row[2] in ("get", "setter", "property", "staticmethod")]


INIT = "from .util import other as renamed, helper\nfrom app.core import make as build\n"


def _repo(tmp_path, init=INIT):
    for rel, text in {
        "pyproject.toml": "[project]\nname = 'app'\n", "app/__init__.py": init,
        "app/util.py": "def other():\n    return 1\n\n\ndef helper():\n    return 2\n",
        "app/core.py": "def make():\n    return 3\n",
    }.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def test_aliased_package_reexport_links_original(tmp_path):
    _repo(tmp_path)
    config = ResolverConfig(tmp_path)
    assert config.python_aliases() == {"app/__init__.py": [["build", "make", "app.core"], ["renamed", "other", ".util"]]}
    source = """\
        from app import renamed, build
        import app


        @renamed
        def f():
            renamed()
            build()
            app.renamed()
        """
    found = calls(source, "main.py", config)
    assert ("Function", "f", "other", "app/util.py", "resolved") in found
    assert ("Function", "f", "make", "app/core.py", "resolved") in found
    # Without the configuration, only the alias's own name.
    assert not [row for row in calls(source, "main.py") if row[2] in ("other", "make")]


def test_an_init_alias_is_python_configuration(tmp_path):
    _repo(tmp_path, "from .util import helper\n")
    before = fingerprints(tmp_path)["python"]
    assert "app/__init__.py" not in before["inputs"]
    (tmp_path / "app/__init__.py").write_text(INIT)
    assert touches_config("app/__init__.py", before["inputs"], tmp_path)
    after = fingerprints(tmp_path)["python"]
    assert "app/__init__.py" in after["inputs"] and after["fingerprint"] != before["fingerprint"]
    # A change elsewhere in it, its aliases the same, changes nothing.
    (tmp_path / "app/__init__.py").write_text("VERSION = 2\n" + INIT)
    assert fingerprints(tmp_path)["python"] == after
