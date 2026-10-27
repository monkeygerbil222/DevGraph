"""Python decorators and aliased package re-exports against a live Neo4j
(audit 12 item 5): `find_callers` and `impact_analysis` of an in-repo
decorator list the definitions it decorates, and a call through an
`__init__.py` alias links the original. Live indexing equals a fresh scan
when a decorator is added or removed and when the alias changes."""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan, index_paths, sync_resolver_config
from devgraph.mcp import tools
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield test_engine
    test_engine.close()


@pytest.fixture
def repo_id(engine):
    repo = f"zz-decorators-{uuid.uuid4().hex[:8]}"
    for each in (repo, f"{repo}_fresh"):
        engine.delete_repository(each)
    yield repo
    for each in (repo, f"{repo}_fresh"):
        engine.delete_repository(each)


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def _scan(engine, repo_id, root):
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root)


def _assert_fresh(engine, repo_id, root):
    expected = fresh_snapshot(engine, repo_id, root)
    actual = graph_snapshot(engine, repo_id)
    if actual != expected:
        pytest.fail("graph does not equal a fresh full_scan:\n" + snapshot_diff(expected, actual))


def _callers(engine, repo_id, name):
    rows = engine.run_cypher(
        "MATCH (a {repo_id: $r})-[:CALLS]->(b:Function {repo_id: $r, name: $n}) RETURN a.name AS a",
        {"r": repo_id, "n": name},
    )
    return sorted(row["a"] for row in rows)


RETRY = """\
    def retry(times):
        def wrap(fn):
            return fn
        return wrap
    """
JOBS = """\
    from app.util import retry


    @retry(3)
    def fetch():
        return 1


    class Worker:
        @retry(2)
        def run(self):
            return 2
    """


def test_impact_of_decorator_lists_decorated_functions(engine, repo_id, tmp_path):
    _write(tmp_path, "app/__init__.py", "")
    _write(tmp_path, "app/util.py", RETRY)
    _write(tmp_path, "app/jobs.py", JOBS)
    _scan(engine, repo_id, tmp_path)
    callers = tools.find_callers(engine, repo_id, "retry")
    assert sorted(row["name"] for row in callers["results"]) == ["Worker", "app/jobs.py"]
    impact = tools.impact_analysis(engine, repo_id, "retry")
    assert sorted(row["name"] for row in impact["direct_dependents"]["results"]) == ["Worker", "app/jobs.py"]


def test_a_decorator_added_or_removed_live_equals_fresh(engine, repo_id, tmp_path):
    _write(tmp_path, "app/__init__.py", "")
    _write(tmp_path, "app/util.py", RETRY)
    jobs = _write(tmp_path, "app/jobs.py", "def fetch():\n    return 1\n")
    _scan(engine, repo_id, tmp_path)
    assert _callers(engine, repo_id, "retry") == []
    jobs.write_text(textwrap.dedent(JOBS))
    index_paths(engine, repo_id, tmp_path, {jobs})
    assert _callers(engine, repo_id, "retry") == ["Worker", "app/jobs.py"]
    _assert_fresh(engine, repo_id, tmp_path)
    jobs.write_text("def fetch():\n    return 1\n")
    index_paths(engine, repo_id, tmp_path, {jobs})
    assert _callers(engine, repo_id, "retry") == []
    _assert_fresh(engine, repo_id, tmp_path)


def test_an_aliased_reexport_changed_live_equals_fresh(engine, repo_id, tmp_path):
    _write(tmp_path, "pyproject.toml", "[project]\nname = 'app'\n")
    init = _write(tmp_path, "app/__init__.py", "from .util import other as renamed\n")
    _write(tmp_path, "app/util.py", "def other():\n    return 1\n\n\ndef helper():\n    return 2\n")
    _write(tmp_path, "main.py", "from app import renamed\n\n\ndef main():\n    return renamed()\n")
    _scan(engine, repo_id, tmp_path)
    assert _callers(engine, repo_id, "other") == ["main"]
    init.write_text("from .util import helper as renamed\n")
    index_paths(engine, repo_id, tmp_path, {init})
    assert sync_resolver_config(engine, repo_id, tmp_path, {init}) > 0
    assert _callers(engine, repo_id, "other") == []
    assert _callers(engine, repo_id, "helper") == ["main"]
    _assert_fresh(engine, repo_id, tmp_path)
