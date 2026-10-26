"""A live Neo4j that is slow, or still building an index, is not "unreachable".

The fail-fast read deadline (`READ_DEADLINE_MARGIN_S` past the query's
timeout) first covered the whole read: the wait for an index still being
built (up to `INDEX_WAIT_S`) and the retry after a failure once connected. So
a Neo4j that answered was reported as "isn't reachable ... podman start".
Each try now gets its own deadline, an index wait extends it, and a query
Neo4j accepted (it answered the transaction's BEGIN) that then gets no answer
is a `QueryNotAnswered`, which is no `ServiceUnavailable`. A stand-in Bolt
server plays Neo4j.
"""

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import anyio
import pytest
from mcp.client import Client
from neo4j.exceptions import ServiceUnavailable, TransientError

from devgraph.config.settings import Settings
from devgraph.graph import engine as engine_module
from devgraph.graph.engine import GraphEngine, IndexesNotReady, QueryNotAnswered
from devgraph.mcp import server as mcp_server
from devgraph.mcp import tools
from tests.fake_bolt import FakeBolt


def _run_bounded(fn, seconds=60.0):
    """`fn()`'s result, failing the test (not hanging the run) when it takes over `seconds`."""
    outcome = {}

    def target():
        try:
            outcome["value"] = fn()
        except BaseException as exc:  # handed to the test thread
            outcome["error"] = exc

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), f"still waiting after {seconds:.0f} s"
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


@pytest.fixture(autouse=True)
def short_limits(monkeypatch):
    monkeypatch.setattr(engine_module, "READ_DEADLINE_MARGIN_S", 0.5)
    monkeypatch.setattr(engine_module, "_INDEX_POLL_S", 0.2)


@contextmanager
def _engine(query):
    bolt = FakeBolt(query)
    engine = GraphEngine(bolt.uri, "neo4j", "x", fail_fast=True, connection_timeout_s=2.0)
    try:
        yield engine, bolt
    finally:
        engine.close(timeout=0.1)
        bolt.close()


def _read(engine, timeout_s=1):
    return _run_bounded(lambda: engine.run_read_cypher("RETURN 1 AS x", {}, timeout_s=timeout_s, max_rows=5))


def test_a_read_waiting_for_an_index_is_not_cut_off_by_the_deadline():
    # About 3 s of "index still populating", twice the 1.5 s deadline.
    with _engine(lambda n: "hint" if n <= 12 else "rows") as (engine, bolt):
        started = time.monotonic()
        assert _read(engine) == ([{"x": 1}], False)
        assert time.monotonic() - started > 1.5
        assert bolt.runs == 13


def test_an_index_never_built_keeps_its_own_error(monkeypatch):
    monkeypatch.setattr(engine_module, "INDEX_WAIT_S", 2.5)
    with _engine(lambda n: "hint") as (engine, _):
        with pytest.raises(IndexesNotReady):
            _read(engine)
    assert tools.neo4j_unavailable(IndexesNotReady("building"), "bolt://x") is None


def test_a_slow_but_live_read_is_not_reported_unreachable():
    with _engine(lambda n: ("slow", 3.0)) as (engine, _):
        with pytest.raises(QueryNotAnswered, match=r"accepted the query but didn't answer within 1\.5 s") as caught:
            _read(engine)
    assert not isinstance(caught.value, ServiceUnavailable)
    assert tools.neo4j_unavailable(caught.value, "bolt://x") is None


class _Tx:
    def __init__(self, failure):
        self.failure = failure

    def run(self, query, parameters):
        raise self.failure


class _SlowThenAnswering:
    """A driver whose first read fails once connected after `slow_s`; the next answers."""

    def __init__(self, slow_s):
        self.slow_s, self.attempts = slow_s, 0

    @contextmanager
    def session(self, **kwargs):
        yield self

    def execute_read(self, work):
        self.attempts += 1
        if self.attempts == 1:
            time.sleep(self.slow_s)
            return work(_Tx(TransientError("deadlock")))
        return [], False


def test_the_retry_after_a_failure_once_connected_gets_its_own_deadline():
    engine = GraphEngine("bolt://127.0.0.1:9", "neo4j", "x", fail_fast=True, connection_timeout_s=2.0)
    driver = _SlowThenAnswering(slow_s=1.2)
    engine._driver = driver
    assert _read(engine) == ([], False)
    assert driver.attempts == 2


@dataclass
class _Repo:
    repo_id: str
    path: Path
    active: bool = True


class _Registry:
    def __init__(self, repos):
        self.repos = repos

    def list_repos(self, active_only=False):
        return list(self.repos)

    def get(self, repo_id):
        return next((r for r in self.repos if r.repo_id == repo_id), None)


def test_an_mcp_call_on_a_slow_neo4j_does_not_say_to_start_it(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "BUILTIN_TIMEOUT_S", 1)
    bolt = FakeBolt(lambda n: ("slow", 3.0))
    settings = Settings(registry_db_path=tmp_path / "r.sqlite3", neo4j_uri=bolt.uri)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
    record = _Repo("zz-slow", tmp_path)
    engine = mcp_server.mcp_engine(settings)
    server = mcp_server.build_server(engine, _Registry([record]), session_repo=record, session_source="env")

    async def scenario():
        async with Client(server, mode="auto") as client:
            return await client.call_tool("find_callers", {"target_name": "save"})

    try:
        result = _run_bounded(lambda: anyio.run(scenario))
    finally:
        engine.close(timeout=0.1)
        bolt.close()
    assert result.is_error is True
    (content,) = result.content
    assert "accepted the query but didn't answer" in content.text
    assert "isn't reachable" not in content.text and "podman start" not in content.text


def test_a_project_tool_on_a_slow_neo4j_says_so():
    from types import SimpleNamespace

    from devgraph.mcp.tool_plane import _failure

    exc = QueryNotAnswered("Neo4j accepted the query but didn't answer within 35 s")
    error = _failure(SimpleNamespace(name="list_files", timeout_s=30), exc, "project", "bolt://127.0.0.1:7687")
    assert str(error) == "project tool 'list_files': Neo4j accepted the query but didn't answer within 35 s"
