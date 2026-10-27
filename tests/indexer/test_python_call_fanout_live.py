"""Name-only and package calls never end where the code can't reach (audit
12 item 1), against a live Neo4j: a production file's name-only call never
links a test file's function (a test's call into production still does),
and no other file's call links a function nested in another function, by
name or through a package pin. Live indexing equals a fresh scan when the
test fake or the nested function comes, goes, or stops being nested."""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan, index_paths, remove_paths
from devgraph.mcp import tools
from devgraph.test_paths import is_test_path
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
    repo = f"zz-fanout-{uuid.uuid4().hex[:8]}"
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


def _targets(engine, repo_id, caller, callee):
    rows = engine.run_cypher(
        "MATCH (a {repo_id: $r, name: $a})-[x:CALLS]->(b:Function {name: $b}) "
        "RETURN b.file AS f, b.owner AS o, x.confidence AS c",
        {"r": repo_id, "a": caller, "b": callee},
    )
    return sorted((row["f"], row["o"], row["c"]) for row in rows)


def _assert_fresh(engine, repo_id, root):
    expected = fresh_snapshot(engine, repo_id, root)
    actual = graph_snapshot(engine, repo_id)
    if actual != expected:
        pytest.fail("graph does not equal a fresh full_scan:\n" + snapshot_diff(expected, actual))


API = """\
    from fastapi import FastAPI

    app = FastAPI()


    @app.get("/items")
    def list_items(driver):
        return driver.session()
    """
DB = "class Driver:\n    def session(self):\n        return 1\n"
FAKE = "class FakeDriver:\n    def session(self):\n        return 2\n"
TEST = "def test_items(driver):\n    driver.session()\n"


def test_production_bare_call_never_reaches_test_file(engine, repo_id, tmp_path):
    _write(tmp_path, "app/api.py", API)
    _write(tmp_path, "app/db.py", DB)
    _write(tmp_path, "tests/fakes.py", FAKE)
    _write(tmp_path, "tests/test_api.py", TEST)
    _scan(engine, repo_id, tmp_path)
    assert _targets(engine, repo_id, "list_items", "session") == [("app/db.py", "Driver", "name")]
    # A test's call into production code, and into another test file, stays.
    assert _targets(engine, repo_id, "test_items", "session") == [
        ("app/db.py", "Driver", "name"), ("tests/fakes.py", "FakeDriver", "name"),
    ]
    flow = tools.trace_request_flow(engine, repo_id, "GET /items")
    names = {(c["name"], c.get("file")) for c in flow["components"]}
    assert ("session", "app/db.py") in names
    assert not [c for c in flow["components"] if is_test_path(c.get("file"))]


def test_a_test_fake_added_after_the_caller_stays_unlinked(engine, repo_id, tmp_path):
    _write(tmp_path, "app/api.py", API)
    _write(tmp_path, "app/db.py", DB)
    _write(tmp_path, "tests/test_api.py", TEST)
    _scan(engine, repo_id, tmp_path)
    fake = _write(tmp_path, "tests/fakes.py", FAKE)
    index_paths(engine, repo_id, tmp_path, {fake})
    assert _targets(engine, repo_id, "list_items", "session") == [("app/db.py", "Driver", "name")]
    assert ("tests/fakes.py", "FakeDriver", "name") in _targets(engine, repo_id, "test_items", "session")
    _assert_fresh(engine, repo_id, tmp_path)
    fake.unlink()
    remove_paths(engine, repo_id, tmp_path, {fake})
    _assert_fresh(engine, repo_id, tmp_path)


NESTED = """\
    def outer():
        def run():
            return 1
        return run


    def same_file(x):
        return x.run()
    """
OTHER = "class Job:\n    def run(self):\n        return 2\n"
CALLER = "def go(x):\n    return x.run()\n"


def test_bare_call_never_reaches_another_files_nested_function(engine, repo_id, tmp_path):
    _write(tmp_path, "app/a.py", NESTED)
    _write(tmp_path, "app/job.py", OTHER)
    _write(tmp_path, "app/b.py", CALLER)
    _scan(engine, repo_id, tmp_path)
    assert _targets(engine, repo_id, "go", "run") == [("app/job.py", "Job", "name")]
    # Its own file's call may still name it.
    assert ("app/a.py", "outer", "name") in _targets(engine, repo_id, "same_file", "run")


def test_a_nested_function_added_or_flipped_live_equals_fresh(engine, repo_id, tmp_path):
    _write(tmp_path, "app/job.py", OTHER)
    _write(tmp_path, "app/b.py", CALLER)
    _scan(engine, repo_id, tmp_path)
    nested = _write(tmp_path, "app/a.py", NESTED)
    index_paths(engine, repo_id, tmp_path, {nested})
    assert _targets(engine, repo_id, "go", "run") == [("app/job.py", "Job", "name")]
    _assert_fresh(engine, repo_id, tmp_path)
    # `outer` becomes a class: its `run` (same node, owner `outer`) is a
    # method another file's call can reach.
    nested.write_text("class outer:\n    def run(self):\n        return 1\n")
    index_paths(engine, repo_id, tmp_path, {nested})
    assert ("app/a.py", "outer", "name") in _targets(engine, repo_id, "go", "run")
    _assert_fresh(engine, repo_id, tmp_path)
    # And back to a function: the edge goes.
    nested.write_text(textwrap.dedent(NESTED))
    index_paths(engine, repo_id, tmp_path, {nested})
    assert _targets(engine, repo_id, "go", "run") == [("app/job.py", "Job", "name")]
    _assert_fresh(engine, repo_id, tmp_path)


def test_package_pin_skips_nested_function(engine, repo_id, tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/tools.py", "def find_callers():\n    return 1\n")
    _write(tmp_path, "main.py", "from pkg import tools\n\n\ndef ask():\n    return tools.find_callers()\n")
    _scan(engine, repo_id, tmp_path)
    server = _write(tmp_path, "pkg/server.py", """\
        def build_server():
            def find_callers():
                return 2
            return find_callers
        """)
    index_paths(engine, repo_id, tmp_path, {server})
    assert _targets(engine, repo_id, "ask", "find_callers") == [("pkg/tools.py", "", "resolved")]
    _assert_fresh(engine, repo_id, tmp_path)
