"""A .cpp defining a method of a class its header declares makes no file-less
copy of the class (audit 11 item 5, C++): the header's Class is the only one,
so `search_component` lists no phantom class in the .cpp, and the graph stays
equal to a fresh scan as the .cpp changes."""

import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan, index_paths
from devgraph.mcp.tools import search_component
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff

SHAPES = """\
namespace geo {
class Circle {
public:
    double area() const { return 3.0; }
};
}
"""
MAIN = """\
#include "shapes.hpp"
double geo::Circle::extra() const { return area(); }
int main() { geo::Circle c; return (int)c.extra(); }
"""


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield test_engine
    test_engine.close()


def test_one_class_node_from_the_header_and_live_equals_fresh(engine, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "shapes.hpp").write_text(SHAPES)
    (root / "main.cpp").write_text(MAIN)
    repo_id = f"zz-cppstub-{uuid.uuid4().hex[:8]}"
    try:
        provision_repository_schema(engine, root)
        engine.upsert_repository(repo_id, repo_id, str(root))
        full_scan(engine, repo_id, root)
        classes = engine.run_cypher(
            "MATCH (c:Class {repo_id: $r, name: 'Circle'}) RETURN c.file AS f", {"r": repo_id}
        )
        assert [row["f"] for row in classes] == ["shapes.hpp"]
        found = search_component(engine, repo_id, "Circle")
        assert [(row["labels"], row["file"]) for row in found["results"] if row["name"] == "Circle"] == [
            (["Class"], "shapes.hpp")
        ]

        (root / "main.cpp").write_text(MAIN + "double geo::Circle::more() const { return extra(); }\n")
        index_paths(engine, repo_id, root, {root / "main.cpp"})
        expected = fresh_snapshot(engine, repo_id, root)
        actual = graph_snapshot(engine, repo_id)
        assert actual == expected, snapshot_diff(expected, actual)
        assert engine.run_cypher(
            "MATCH (c:Class {repo_id: $r}) WHERE c.file IS NULL RETURN count(c) AS n", {"r": repo_id}
        ) == [{"n": 0}]
    finally:
        engine.delete_repository(repo_id)
