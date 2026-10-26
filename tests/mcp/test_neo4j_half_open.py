"""Neo4j gone behind a forwarder that still accepts connections (audit 11, item 3).

A local forwarder (socat) keeps accepting once the Neo4j behind it is gone, so
the driver's refused-connection fast path never fires: a pooled connection's
query waited for the server's 120 s receive timeout (forever without that
hint), where the README promises an MCP call fails within a few seconds.
A stand-in Bolt server plays both halves, never the real Neo4j.
"""

import threading
import time
from dataclasses import dataclass
from pathlib import Path

import anyio
import pytest
from mcp.client import Client
from neo4j.exceptions import ServiceUnavailable

from devgraph.config.settings import Settings
from devgraph.graph import engine as engine_module
from devgraph.graph.engine import GraphEngine
from devgraph.mcp import server as mcp_server
from tests.fake_bolt import FakeBolt

#: The most a call may take, with headroom for a loaded CI runner.
BOUND_S = 10.0


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


@pytest.fixture
def bolt():
    server = FakeBolt()
    yield server
    server.close()


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


def test_a_call_on_a_pooled_connection_fails_fast_once_the_forwarder_stops_answering(bolt, tmp_path, monkeypatch):
    settings = Settings(registry_db_path=tmp_path / "r.sqlite3", neo4j_uri=bolt.uri)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
    record = _Repo("zz-half-open", tmp_path)
    # A pooled connection idle past the liveness threshold is checked before use.
    monkeypatch.setattr(engine_module, "LIVENESS_CHECK_S", 0.3)
    engine = mcp_server.mcp_engine(settings)
    engine.probe_connectivity()  # the server's start-up check leaves a pooled connection
    time.sleep(0.5)
    bolt.freeze()
    server = mcp_server.build_server(engine, _Registry([record]), session_repo=record, session_source="env")

    async def scenario():
        async with Client(server, mode="auto") as client:
            return await client.call_tool("find_callers", {"target_name": "save"})

    started = time.monotonic()
    try:
        result = _run_bounded(lambda: anyio.run(scenario))
    finally:
        engine.close(timeout=0.1)
    elapsed = time.monotonic() - started
    assert elapsed < BOUND_S, f"find_callers took {elapsed:.1f} s behind a forwarder that stopped answering"
    assert result.is_error is True
    (content,) = result.content
    assert f"Neo4j isn't reachable at {bolt.uri}" in content.text


def test_a_read_that_never_gets_an_answer_is_abandoned_after_its_timeout_and_a_margin(bolt, monkeypatch):
    # Logged in, then silence: the transaction timeout is the server's to
    # enforce, so only the client's own deadline ends the wait.
    # It never answered the transaction's BEGIN, so it is reported unreachable.
    monkeypatch.setattr(engine_module, "READ_DEADLINE_MARGIN_S", 0.5)
    engine = GraphEngine(bolt.uri, "neo4j", "x", fail_fast=True, connection_timeout_s=2.0)
    started = time.monotonic()
    try:
        with pytest.raises(ServiceUnavailable, match=r"did not answer within 1\.5 s"):
            _run_bounded(lambda: engine.run_read_cypher("RETURN 1", {}, timeout_s=1, max_rows=1))
    finally:
        engine.close(timeout=0.1)
    assert time.monotonic() - started < 1.5 + 3.0


def test_a_pooled_connection_used_again_soon_after_the_freeze_is_given_up_at_the_deadline(bolt, monkeypatch):
    # Within the liveness threshold the pooled connection is used unchecked;
    # its BEGIN is never answered, so the deadline reports Neo4j unreachable.
    monkeypatch.setattr(engine_module, "READ_DEADLINE_MARGIN_S", 0.5)
    engine = GraphEngine(bolt.uri, "neo4j", "x", fail_fast=True, connection_timeout_s=2.0)
    try:
        engine.probe_connectivity()
        bolt.freeze()
        with pytest.raises(ServiceUnavailable, match=r"did not answer within 1\.5 s"):
            _run_bounded(lambda: engine.run_read_cypher("RETURN 1", {}, timeout_s=1, max_rows=1))
    finally:
        engine.close(timeout=0.1)
