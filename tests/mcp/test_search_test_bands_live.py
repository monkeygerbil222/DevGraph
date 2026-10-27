"""search_component against a live Neo4j: test code is ranked lower only within
its band of name matches, so a test helper named exactly as the query still
beats production names that only contain one of its words; and a CamelCase
query naming test code (`FakeEngine`) ranks it as any other. Fixture data is
fictional."""

import uuid

import pytest

from devgraph.graph.engine import GraphEngine
from devgraph.mcp import tools

REPO = f"zz-search-bands-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def seeded():
    engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    engine.init_schema()

    def fn(name, file, label="Function"):
        return {"label": label, "repo_id": REPO, "name": name, "properties": {"file": file}}

    engine.upsert_nodes([
        fn("make_repo", "tests/conftest.py"),
        fn("RepoRegistry", "shop/registry/store.py", "Class"),
        fn("remove_repo", "shop/registry/store.py"),
        *[fn(f"make_widget_{i}", "shop/widgets.py") for i in range(6)],  # start with the term `make`
        fn("engine", "tests/graph/test_engine.py"),
        fn("GraphEngine", "shop/graph/engine.py", "Class"),
        fn("FakeEngine", "tests/fakes.py", "Class"),
        fn("FakeEngineFactory", "shop/demo.py", "Class"),
    ])
    try:
        yield engine
    finally:
        engine.delete_repository(REPO)
        engine.close()


def _first(engine, query, n=3):
    return [r["name"] for r in tools.search_component(engine, REPO, query, max_results=n)["results"]]


def test_an_exact_test_helper_beats_production_term_matches(seeded):
    assert _first(seeded, "make_repo")[0] == "make_repo"


def test_whole_query_production_matches_still_beat_a_test_fixture(seeded):
    assert _first(seeded, "engine", 5)[:3] == ["FakeEngineFactory", "GraphEngine", "engine"]


def test_a_camel_case_query_naming_test_code_ranks_it_as_any_other(seeded):
    assert _first(seeded, "FakeEngine")[0] == "FakeEngine"


def test_test_words_are_whole_words():
    for query in ("FakeEngine", "MockRegistry", "StubClock", "TestSearch", "test_search", "conftest", "HTTPTest"):
        assert tools._targets_tests(query), query
    for query in ("testimonials", "latest", "registry", "attestation"):
        assert not tools._targets_tests(query), query
