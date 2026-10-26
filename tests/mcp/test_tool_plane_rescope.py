"""A session whose repository is registered after it started (audit 11, item 8).

`run_stdio` decided once, at start-up, whether the session had a repository: a
session opened in a folder registered afterwards ran no reload poller and
advertised no tool-list changes, so its project and global tools never
appeared until the client restarted. Stub engine and registry."""

import asyncio
import json
import textwrap
from dataclasses import dataclass
from pathlib import Path

import anyio
import pytest

from devgraph.config import global_tools
from devgraph.config.project_tools import TOOLS_FILENAME
from devgraph.config.settings import Settings
from devgraph.mcp import server as mcp_server
from devgraph.mcp.tool_plane import session_rescoper
from devgraph.mcp.tool_reload import poll_tool_reloads
from tests.mcp.test_tool_reload import _run_stdio_handshake

pytestmark = pytest.mark.usefixtures("trusted_project_tools")

PROJECT = """
    version: 1
    tools:
      - name: list_files
        description: List files.
        cypher: |
          MATCH (f:File {repo_id: $repo_id}) RETURN f.path AS path
"""

GLOBAL = json.dumps({"version": 1, "tools": [
    {"name": "gamma", "description": "Global gamma.", "cypher": "MATCH (n {repo_id: $repo_id}) RETURN n.name AS name"}
]})


@dataclass
class Repo:
    repo_id: str
    path: Path
    active: bool = True


class Registry:
    """Registers like RepoRegistry: each change moves the change marker."""

    def __init__(self):
        self.repos, self.marker, self.reads = [], None, 0

    def register(self, repo):
        self.repos.append(repo)
        self.marker = f"changed {len(self.repos)}"

    def list_repos(self, active_only=False):
        self.reads += 1
        return list(self.repos)

    def get(self, repo_id):
        return next((r for r in self.repos if r.repo_id == repo_id), None)

    def last_changed_at(self):
        return self.marker


class Engine:
    def run_read_cypher(self, query, parameters, *, timeout_s, max_rows):
        return [], False


@pytest.fixture
def session(tmp_path, monkeypatch):
    """An unscoped session started in `demo`, a folder not registered yet."""
    repo_dir = tmp_path / "demo"
    repo_dir.mkdir()
    (repo_dir / TOOLS_FILENAME).write_text(textwrap.dedent(PROJECT))
    store = tmp_path / "store" / global_tools.GLOBAL_TOOLS_FILENAME
    store.parent.mkdir()
    store.write_text(GLOBAL)
    monkeypatch.setattr(global_tools, "_default_path", lambda: store)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: Settings(registry_db_path=tmp_path / "r.sqlite3"))
    registry = Registry()
    server = mcp_server.build_server(
        Engine(), registry, session_repo=None, session_source="none",
        rescope=session_rescoper(registry, {}, repo_dir),
    )
    return server, registry, Repo("demo", repo_dir)


def _tools(server):
    return {t.name for t in asyncio.run(server.list_tools())}


def _status(server):
    return json.loads(asyncio.run(server.read_resource("devgraph://project-tools"))[0].content)


def test_registering_the_folder_serves_its_project_and_global_tools(session):
    server, registry, repo = session
    plane = server.devgraph_tool_plane
    assert plane.reload_if_changed() is False
    assert not {"list_files", "gamma"} & _tools(server)
    registry.register(repo)
    assert plane.reload_if_changed() is True
    assert {"list_files", "gamma"} <= _tools(server)
    status = _status(server)
    assert status["scope"] == {"repo_id": "demo", "source": "cwd"}
    assert status["served"] == ["list_files", "gamma"]
    assert not any("only in sessions scoped" in n for n in status["notices"])
    assert plane.reload_if_changed() is False


def test_the_registry_is_read_again_only_when_its_change_marker_moves(session):
    server, registry, _ = session
    plane = server.devgraph_tool_plane
    plane.reload_if_changed()
    reads = registry.reads
    for _ in range(3):
        plane.reload_if_changed()
    assert registry.reads == reads
    registry.marker = "changed elsewhere"  # another folder registered
    plane.reload_if_changed()
    assert registry.reads == reads + 1


def test_a_rescopable_session_advertises_tool_list_changes(session, monkeypatch):
    server, _, _ = session
    capabilities = anyio.run(_run_stdio_handshake, server, monkeypatch)
    assert capabilities.tools.list_changed is True


def test_the_poller_notifies_once_the_folder_is_registered(session):
    server, registry, repo = session
    notified = []

    class Notifier:
        async def notify(self):
            notified.append(1)

    async def scenario():
        with anyio.CancelScope() as scope:
            ticks = 0

            async def sleep(_):
                nonlocal ticks
                ticks += 1
                if ticks == 2:
                    registry.register(repo)
                if ticks > 3:
                    scope.cancel()
                await anyio.sleep(0)

            await poll_tool_reloads(server.devgraph_tool_plane, Notifier(), sleep=sleep)

    anyio.run(scenario)
    assert notified == [1]
