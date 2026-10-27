"""search_component and the dashboard search against a live Neo4j: name matches in
test code (fixtures named `engine`, `registry`) come after the production code
they share a word with, unless the query asks for test code; they are ranked
lower, never left out. Fixture data is fictional."""

import uuid

import pytest

from devgraph.dashboard import queries
from devgraph.graph.engine import GraphEngine
from devgraph.mcp import tools

REPO = f"zz-search-tests-last-{uuid.uuid4().hex[:8]}"
FIXTURES = 12


@pytest.fixture
def seeded():
    engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    engine.init_schema()
    nodes = [  # more same-named test fixtures than a page holds, each an exact name match
        {"label": "Function", "repo_id": REPO, "name": name, "properties": {"file": f"tests/t{i:02d}/test_x.py"}}
        for name in ("engine", "registry")
        for i in range(FIXTURES)
    ]
    nodes += [
        {"label": "Class", "repo_id": REPO, "name": "GraphEngine", "properties": {"file": "shop/graph/engine.py"}},
        {"label": "Class", "repo_id": REPO, "name": "RepoRegistry", "properties": {"file": "shop/registry/store.py"}},
        {"label": "Function", "repo_id": REPO, "name": "fake_engine", "properties": {"file": "tests/conftest.py"}},
        {"label": "Function", "repo_id": REPO, "name": "make_fake_engine", "properties": {"file": "shop/demo.py"}},
        # Matches `engine` only by its description: after every name match.
        {"label": "Function", "repo_id": REPO, "name": "boot", "properties": {"file": "shop/boot.py",
                                                                             "description": "Starts the engine"}},
    ]
    engine.upsert_nodes(nodes)
    try:
        yield engine
    finally:
        engine.delete_repository(REPO)
        engine.close()


def _names(rows):
    return [(r["name"], r["file"]) for r in rows]


def test_search_ranks_production_before_tests(seeded):
    for query, wanted in (("engine", ("GraphEngine", "shop/graph/engine.py")),
                          ("registry", ("RepoRegistry", "shop/registry/store.py"))):
        body = tools.search_component(seeded, REPO, query, max_results=5)
        assert _names(body["results"])[0] == wanted, body["results"]


def test_test_matches_are_down_ranked_not_dropped(seeded):
    body = tools.search_component(seeded, REPO, "engine", max_results=50)
    names = _names(body["results"])
    production = names[:2]
    assert production == [("GraphEngine", "shop/graph/engine.py"), ("make_fake_engine", "shop/demo.py")]
    fixtures = [n for n in names if n[1].startswith("tests/")]
    assert len(fixtures) == FIXTURES + 1  # every `engine` fixture and `fake_engine`, after the production names
    assert names[-1] == ("boot", "shop/boot.py")  # description-only match last
    assert body["count"] == FIXTURES + 4 and body["truncated"] is False


def test_a_query_naming_test_code_ranks_it_as_any_other(seeded):
    body = tools.search_component(seeded, REPO, "fake_engine", max_results=5)
    assert _names(body["results"])[0] == ("fake_engine", "tests/conftest.py")


def test_the_dashboard_search_ranks_production_before_tests(seeded):
    rows = queries.search_components(seeded, REPO, "engine", 5)
    assert (rows[0]["name"], rows[0]["file"]) == ("GraphEngine", "shop/graph/engine.py")
    assert queries.search_components(seeded, REPO, "registry", 5)[0]["name"] == "RepoRegistry"
