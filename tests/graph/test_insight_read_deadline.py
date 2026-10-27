"""Insight reads get the per-try read deadline (audit 11 follow-up).

`read_insights_summary` and `load_insight_graph` ran as plain autocommit
statements, outside the deadline every other MCP read gets (`_within`). A
Neo4j that answered the login and then went silent (gone behind a forwarder
that still accepts) hung them: the MCP insight tools, and the agent's
insights scheduler, whose engine has no connection timeout at all.

Now each try is given up once it has gone its deadline without an answer:
`Neo4jNotAnswering` when Neo4j never accepted the transaction,
`QueryNotAnswered` when it did (slow, not gone). The load may run as long as
rows keep coming, since it is legitimately long on a large graph. A stand-in
Bolt server plays Neo4j, never the real one.
"""

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import anyio
import pytest
from mcp.client import Client
from neo4j.exceptions import ServiceUnavailable

from devgraph.analytics.insights import COMMUNITY_RELATIONSHIPS, InsightsScheduler
from devgraph.config.settings import Settings
from devgraph.graph import engine as engine_module
from devgraph.graph.engine import GraphEngine, Neo4jNotAnswering, QueryNotAnswered
from devgraph.mcp import server as mcp_server
from tests.fake_bolt import FakeBolt


def _run_bounded(fn, seconds=20.0):
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
    monkeypatch.setattr(engine_module, "INSIGHT_SUMMARY_TIMEOUT_S", 1.0)
    monkeypatch.setattr(engine_module, "INSIGHT_LOAD_QUIET_S", 1.5)


@contextmanager
def _engine(query=None, **options):
    """An engine on a stand-in Bolt server; by default the agent's (no option set)."""
    bolt = FakeBolt(query)
    engine = GraphEngine(bolt.uri, "neo4j", "x", **options)
    try:
        yield engine
    finally:
        engine.close(timeout=0.1)
        bolt.close()


MCP = {"fail_fast": True, "connection_timeout_s": 2.0}
AGENT: dict = {}


@pytest.mark.parametrize("options", [MCP, AGENT], ids=["mcp", "agent"])
def test_the_summary_read_gives_up_on_a_silent_neo4j(options):
    with _engine(**options) as engine:
        started = time.monotonic()
        with pytest.raises(Neo4jNotAnswering):
            _run_bounded(lambda: engine.read_insights_summary("r"))
        assert time.monotonic() - started < 6


@pytest.mark.parametrize("options", [MCP, AGENT], ids=["mcp", "agent"])
def test_the_load_gives_up_on_a_silent_neo4j(options):
    with _engine(**options) as engine:
        with pytest.raises(Neo4jNotAnswering):
            _run_bounded(lambda: engine.load_insight_graph("r", COMMUNITY_RELATIONSHIPS))


def test_a_slow_summary_read_is_not_reported_unreachable():
    with _engine(lambda n: ("slow", 4.0), **MCP) as engine:
        with pytest.raises(QueryNotAnswered, match=r"didn't answer within 1\.5 s") as caught:
            _run_bounded(lambda: engine.read_insights_summary("r"))
    assert not isinstance(caught.value, ServiceUnavailable)


def test_a_slow_load_is_not_reported_unreachable():
    with _engine(lambda n: ("slow", 4.0)) as engine:
        with pytest.raises(QueryNotAnswered, match=r"didn't answer within 1\.5 s"):
            _run_bounded(lambda: engine.load_insight_graph("r", COMMUNITY_RELATIONSHIPS))


class _Record:
    def __init__(self, data):
        self._data = data

    def data(self):
        return dict(self._data)


class _StreamingTx:
    """Each query's rows arrive `gap_s` apart: a long load that keeps answering."""

    def __init__(self, gap_s):
        self.gap_s = gap_s

    def run(self, query, **params):
        if "ids" in params:
            rows = [{"id": i, "name": i, "labels": ["Function"], "file": "f.py", "owner": None, "community": None}
                    for i in params["ids"]]
        else:
            rows = [{"source": "a", "target": "b", "type": "CALLS"}, {"source": "b", "target": "c", "type": "CALLS"}]
        for row in rows:
            time.sleep(self.gap_s)
            yield _Record(row)


class _StreamingDriver:
    def __init__(self, gap_s):
        self.gap_s = gap_s

    @contextmanager
    def session(self, **kwargs):
        yield self

    def execute_read(self, work, *args, **kwargs):
        return work(_StreamingTx(self.gap_s), *args, **kwargs)


def test_a_long_load_that_keeps_answering_is_not_cut_off():
    # Five rows 0.8 s apart: 4 s in all, well past the 1.5 s quiet limit.
    engine = GraphEngine("bolt://127.0.0.1:9", "neo4j", "x", fail_fast=True, connection_timeout_s=2.0)
    engine._driver = _StreamingDriver(gap_s=0.8)
    started = time.monotonic()
    nodes, edges = _run_bounded(lambda: engine.load_insight_graph("r", COMMUNITY_RELATIONSHIPS))
    assert time.monotonic() - started > 3
    assert len(edges) == 2 and sorted(n["id"] for n in nodes) == ["a", "b", "c"]


@dataclass
class _Repo:
    repo_id: str
    path: Path
    last_indexed: str | None = "2026-10-01T10:00:00+00:00"
    active: bool = True


class _Registry:
    def __init__(self, repos):
        self.repos = repos

    def list_repos(self, active_only=False):
        return list(self.repos)

    def get(self, repo_id):
        return next((r for r in self.repos if r.repo_id == repo_id), None)


def test_the_insights_scheduler_moves_on_from_a_silent_neo4j(tmp_path):
    with _engine() as engine:
        scheduler = InsightsScheduler(engine, _Registry([_Repo("zz-silent", tmp_path)]))
        assert _run_bounded(scheduler.run_once) == []


def test_an_mcp_insight_tool_on_a_slow_neo4j_does_not_say_to_start_it(tmp_path, monkeypatch):
    bolt = FakeBolt(lambda n: ("slow", 4.0))
    settings = Settings(registry_db_path=tmp_path / "r.sqlite3", neo4j_uri=bolt.uri)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
    record = _Repo("zz-slow-insights", tmp_path)
    engine = mcp_server.mcp_engine(settings)
    server = mcp_server.build_server(engine, _Registry([record]), session_repo=record, session_source="env")

    async def scenario():
        async with Client(server, mode="auto") as client:
            return await client.call_tool("find_communities", {"repo_id": "zz-slow-insights"})

    try:
        result = _run_bounded(lambda: anyio.run(scenario))
    finally:
        engine.close(timeout=0.1)
        bolt.close()
    assert result.is_error is True
    (content,) = result.content
    assert "accepted the query but didn't answer" in content.text
    assert "isn't reachable" not in content.text


class _RecordingDriver(_StreamingDriver):
    def execute_read(self, work, *args, **kwargs):
        self.timeout = getattr(work, "timeout", None)
        return super().execute_read(work, *args, **kwargs)


def test_the_load_has_a_generous_server_limit():
    """An abandoned load (its client gave up) must not stream on the server forever."""
    engine = GraphEngine("bolt://127.0.0.1:9", "neo4j", "x", fail_fast=True, connection_timeout_s=2.0)
    engine._driver = _RecordingDriver(gap_s=0.0)
    engine.load_insight_graph("r", COMMUNITY_RELATIONSHIPS)
    assert engine._driver.timeout == engine_module.INSIGHT_LOAD_TIMEOUT_S >= 600
