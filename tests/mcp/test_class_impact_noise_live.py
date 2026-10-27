"""A class's impact counts by-name callers apart, against a live Neo4j
(audit 11 review F3): a call on a value nothing types links every member
of its name, so a class's by-name callers are mostly noise. A by-name call
of a dunder member (`super().__init__()`) is no edge into the class, a
dependent reached only by by-name calls is `name_only`, counted in
`name_only_dependents` for information; `risk_level` counts every direct
dependent, as a function's does (review round 2). A function's
impact is unchanged. Fixture code is fictional."""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan
from devgraph.mcp import tools


@pytest.fixture(scope="module")
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield test_engine
    test_engine.close()


STORE = """\
    class Store:
        def __init__(self):
            self.rows = []

        def save(self, row):
            self.rows.append(row)
    """

USERS = """\
    from store import Store


    def typed(s: Store):
        s.save(1)


    def loose_a(x):
        x.save(1)


    def loose_b(x):
        x.save(2)


    def loose_c(x):
        x.save(3)


    def loose_d(x):
        x.save(4)


    class Cache:
        def __init__(self):
            super().__init__()
    """


@pytest.fixture(scope="module")
def repo(engine, tmp_path_factory):
    root = tmp_path_factory.mktemp("class-noise")
    repo_id = f"zz-class-noise-{uuid.uuid4().hex[:8]}"
    (root / "store.py").write_text(textwrap.dedent(STORE))
    (root / "users.py").write_text(textwrap.dedent(USERS))
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root)
    yield repo_id
    engine.delete_repository(repo_id)


def test_a_classs_by_name_callers_are_counted_apart(engine, repo):
    result = tools.impact_analysis(engine, repo, "Store")
    rows = {row["name"]: row.get("name_only", False) for row in result["direct_dependents"]["results"]}
    # Cache.__init__'s super().__init__() is no edge into Store.__init__.
    assert rows == {"typed": False, "loose_a": True, "loose_b": True, "loose_c": True, "loose_d": True}
    assert result["name_only_dependents"] == 4
    # Five direct dependents, by-name ones included (the dunder call not): medium.
    assert result["risk_level"] == "medium"
    callers = tools.find_callers(engine, repo, "Store")
    assert sorted(row["name"] for row in callers["results"]) == ["loose_a", "loose_b", "loose_c", "loose_d", "typed"]


def test_a_functions_impact_counts_every_caller(engine, repo):
    result = tools.impact_analysis(engine, repo, "save")
    assert result["direct_dependents"]["count"] == 5
    assert all("name_only" not in row for row in result["direct_dependents"]["results"])
    assert "name_only_dependents" not in result
    assert result["risk_level"] == "medium"
    # Cache's super().__init__() reaches no base of the repository (its base
    # is `object`), so it links no `__init__` at all (audit 12).
    init = tools.find_callers(engine, repo, "__init__", owner="Store")
    assert init["results"] == []
