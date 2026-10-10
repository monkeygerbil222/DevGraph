"""Built-in tools are bounded: impact traversal depth, and every query read-only
with a timeout that reaches the client as a ToolError."""

import uuid
from dataclasses import dataclass
from pathlib import Path

import anyio
import pytest
from mcp.client import Client
from neo4j.exceptions import ClientError

from devgraph.config.settings import Settings
from devgraph.graph.engine import GraphEngine
from devgraph.mcp import server as mcp_server
from devgraph.mcp import tools
from devgraph.mcp.tools import IMPACT_MAX_DEPTH, impact_analysis


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    test_engine.init_schema()
    yield test_engine
    test_engine.close()


@pytest.fixture
def chain(engine):
    """f0 <- f1 <- ... <- f7: f<k> reaches f0 over k CALLS edges."""
    repo_id = f"zz-impact-depth-{uuid.uuid4().hex[:8]}"
    names = [f"f{k}" for k in range(8)]
    for name in names:
        engine.upsert_node("Function", repo_id, name)
    for caller, callee in zip(names[1:], names):
        engine.upsert_relationship("Function", caller, "CALLS", "Function", callee, repo_id)
    yield repo_id
    engine.delete_repository(repo_id)


def test_impact_transitive_dependents_stop_at_the_max_depth(engine, chain):
    assert IMPACT_MAX_DEPTH == 4
    result = impact_analysis(engine, chain, "f0")
    assert [d["name"] for d in result["direct_dependents"]["results"]] == ["f1"]
    transitive = sorted(d["name"] for d in result["transitive_dependents"]["results"])
    assert transitive == ["f2", "f3", "f4"]  # f5 is 5 hops away


def test_impact_queries_carry_no_unbounded_path():
    import inspect

    source = inspect.getsource(tools)
    assert "*2..]" not in source and "*2..}" not in source


def test_builtin_queries_run_read_only_with_a_timeout():
    seen = {}

    class Engine:
        def run_read_cypher(self, query, params, *, timeout_s, max_rows):
            seen.update(timeout_s=timeout_s, max_rows=max_rows)
            return [], False

        def run_cypher(self, query, params=None):
            raise AssertionError("built-in tools must not use the unbounded run_cypher")

    impact_analysis(Engine(), "demo", "f0")
    assert seen["timeout_s"] == tools.BUILTIN_TIMEOUT_S


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


class _SlowEngine:
    def run_read_cypher(self, query, params, *, timeout_s, max_rows):
        raise ClientError._hydrate_neo4j(code="Neo.ClientError.Transaction.TransactionTimedOut", message="secret")

    def run_cypher(self, query, params=None):
        raise AssertionError("built-in tools must not use the unbounded run_cypher")


def test_a_timeout_reaches_the_client_as_a_tool_error(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "get_settings", lambda: Settings(registry_db_path=tmp_path / "r.sqlite3"))
    (tmp_path / "demo").mkdir()
    record = _Repo("demo", tmp_path / "demo")
    server = mcp_server.build_server(_SlowEngine(), _Registry([record]), session_repo=record, session_source="env")

    async def scenario():
        async with Client(server, mode="auto") as client:
            return await client.call_tool("impact_analysis", {"component_name": "f0"})

    result = anyio.run(scenario)
    assert result.is_error is True
    (content,) = result.content
    assert content.text == (
        f"Error executing tool impact_analysis: query timed out after {tools.BUILTIN_TIMEOUT_S} s; narrow the request"
    )
