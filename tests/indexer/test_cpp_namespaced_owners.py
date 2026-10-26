"""A C++ class in a namespace owns all its members under one owner (audit 11
follow-up, index format 24).

An in-class method of `namespace shop { class Ledger }` was owned by
`Ledger`, an out-of-class `int shop::Ledger::total()` by `shop.Ledger` (or
by `Ledger` again when the file happened to declare `namespace shop`), so a
namespaced class's methods split across two owners: `owner="shop.Ledger"`
or `shop::Ledger::sum` found only some of them. A class at a namespace's
top level is now owned by the namespace, so every member is `shop.Ledger`
however its definition is spelt, and one class name in two namespaces of a
file is two nodes. Fixture code is fictional.
"""

import textwrap
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.cpp.extractor import extract_cpp_file
from devgraph.indexer.dispatch import full_scan, index_paths
from devgraph.mcp import tools
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff


def _owners(source, path="ledger.hpp"):
    result = extract_cpp_file(textwrap.dedent(source), path, "r")
    return sorted((n.label, n.name, n.properties.get("owner")) for n in result.nodes if n.label != "Module")


def test_every_member_of_a_namespaced_class_has_one_owner():
    assert _owners("""\
        namespace shop {
        class Ledger {
        public:
            Ledger();
            int sum() const { return 1; }
            int total() const;
            int avg() const;
            int med() const;
        };
        int Ledger::avg() const { return 2; }
        }
        int shop::Ledger::total() const { return 3; }
        namespace shop { Ledger::Ledger() {} }
        """) == [
        ("Class", "Ledger", "shop"),
        ("Function", "Ledger", "shop.Ledger"),
        ("Function", "avg", "shop.Ledger"),
        ("Function", "sum", "shop.Ledger"),
        ("Function", "total", "shop.Ledger"),
    ]


def test_an_out_of_class_definition_in_a_file_without_the_namespace_block():
    assert _owners('#include "ledger.hpp"\nint shop::Ledger::total() const { return 3; }\n', "ledger.cpp") == [
        ("Function", "total", "shop.Ledger"),
    ]


def test_nested_namespaces_and_classes():
    assert _owners("""\
        namespace a { namespace b {
        class Outer {
            class Inner { void run() {} };
            void go();
        };
        void b::Outer::go() {}
        } }
        namespace a::b { void Outer::Inner::stop() {} }
        void a::b::Outer::Inner::halt() {}
        class Plain { void m() {} };
        """) == [
        ("Class", "Inner", "a.b.Outer"),
        ("Class", "Outer", "a.b"),
        ("Class", "Plain", ""),
        ("Function", "go", "a.b.Outer"),
        ("Function", "halt", "a.b.Outer.Inner"),
        ("Function", "m", "Plain"),
        ("Function", "run", "a.b.Outer.Inner"),
        ("Function", "stop", "a.b.Outer.Inner"),
    ]


def test_one_class_name_in_two_namespaces_of_a_file_is_two_classes():
    assert _owners("""\
        namespace a { class Item { int id() { return 1; } }; }
        namespace b { class Item { int id() { return 2; } }; }
        """) == [
        ("Class", "Item", "a"), ("Class", "Item", "b"),
        ("Function", "id", "a.Item"), ("Function", "id", "b.Item"),
    ]


def test_a_namespaced_class_with_a_base_from_another_file_is_open():
    result = extract_cpp_file(
        '#include "base.hpp"\nnamespace shop { class Sub : public Base { void own() {} }; }\n', "sub.hpp", "r"
    )
    (module,) = [n for n in result.nodes if n.label == "Module"]
    assert module.properties["heir_open"] == ["shop.Sub"]


def test_a_contains_edge_names_the_namespaced_owner():
    result = extract_cpp_file("namespace shop { class Ledger { int sum() { return 1; } }; }\n", "ledger.hpp", "r")
    contains = sorted(
        (r.from_label, r.from_name, r.from_owner, r.to_name, r.to_owner)
        for r in result.relationships if r.rel_type == "CONTAINS"
    )
    assert contains == [("Class", "Ledger", "shop", "sum", "shop.Ledger"), ("Module", "ledger.hpp", None, "Ledger", "shop")]


# ── live ─────────────────────────────────────────────────────────────────

LEDGER_HPP = """\
    namespace shop {
    class Ledger {
    public:
        Ledger();
        int sum() const { return 1; }
        int total() const;
    };
    }
    int shop::Ledger::total() const { return sum(); }
    """

LEDGER_CPP = """\
    #include "ledger.hpp"
    namespace shop { Ledger::Ledger() {} }
    """

APP_CPP = """\
    #include "ledger.hpp"
    int report() { shop::Ledger l; return l.sum() + l.total(); }
    """


@pytest.fixture(scope="module")
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield test_engine
    test_engine.close()


@dataclass
class _Repo:
    repo_id: str
    path: Path
    active: bool = True


class _Registry:
    def __init__(self, repos):
        self.repos = repos

    def get(self, repo_id):
        return next((r for r in self.repos if r.repo_id == repo_id), None)


@pytest.fixture
def repo(engine, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    for rel, text in {"ledger.hpp": LEDGER_HPP, "ledger.cpp": LEDGER_CPP, "app.cpp": APP_CPP}.items():
        (root / rel).write_text(textwrap.dedent(text))
    repo_id = f"zz-cppns-{uuid.uuid4().hex[:8]}"
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root)
    yield repo_id, root
    engine.delete_repository(repo_id)


def test_live_a_namespaced_class_answers_by_its_one_owner(engine, repo):
    repo_id, root = repo
    owners = engine.run_cypher(
        "MATCH (f:Function {repo_id: $r}) WHERE f.name IN ['Ledger', 'sum', 'total'] "
        "RETURN f.name AS name, f.owner AS owner ORDER BY name",
        {"r": repo_id},
    )
    assert [(r["name"], r["owner"]) for r in owners] == [
        ("Ledger", "shop.Ledger"), ("sum", "shop.Ledger"), ("total", "shop.Ledger"),
    ]
    contained = engine.run_cypher(
        "MATCH (c:Class {repo_id: $r, name: 'Ledger'})-[:CONTAINS]->(f:Function) RETURN f.name AS name ORDER BY name",
        {"r": repo_id},
    )
    assert [r["name"] for r in contained] == ["sum", "total"]  # the .cpp's constructor: the known gap

    described = tools.describe_node(engine, repo_id, "Ledger")
    assert described["status"] == "found", described
    assert described["node"]["file"] == "ledger.hpp"
    assert [c["file"] for c in described["constructors"]] == ["ledger.cpp"]
    for qualified in ("shop::Ledger::sum", "shop::Ledger::total"):
        assert tools.describe_node(engine, repo_id, qualified)["status"] == "found", qualified

    callers = tools.find_callers(engine, repo_id, "total", owner="shop.Ledger")
    assert sorted(row["name"] for row in callers["results"]) == ["report"]

    impact = tools.impact_analysis(engine, repo_id, "Ledger", file="ledger.hpp")
    direct = {row["name"]: sorted(row.get("via") or []) for row in impact["direct_dependents"]["results"]}
    assert direct.get("report") == ["shop.Ledger.sum", "shop.Ledger.total"], impact


def test_live_an_edit_equals_a_fresh_scan(engine, repo):
    repo_id, root = repo
    (root / "ledger.hpp").write_text(textwrap.dedent(LEDGER_HPP).replace(
        "int total() const;", "int total() const;\n    int avg() const { return total(); }"
    ))
    (root / "ledger.cpp").write_text(textwrap.dedent(LEDGER_CPP) + "int shop::Ledger::med() const { return 2; }\n")
    (root / "app.cpp").write_text(textwrap.dedent(APP_CPP).replace("l.total()", "l.avg()"))
    index_paths(engine, repo_id, root, {root / "ledger.hpp", root / "ledger.cpp", root / "app.cpp"})
    expected = fresh_snapshot(engine, repo_id, root)
    actual = graph_snapshot(engine, repo_id)
    assert actual == expected, snapshot_diff(expected, actual)
    callers = tools.find_callers(engine, repo_id, "avg", owner="shop.Ledger")
    assert sorted(row["name"] for row in callers["results"]) == ["report"]
