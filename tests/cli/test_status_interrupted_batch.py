"""`devgraph status` and doctor's "Graph index" name a repository whose last
update was cut short (audit 11 follow-up).

A batch that lost Neo4j, or whose process was killed, leaves its
`batch_open` marker on the Repository node, and the agent's next catch-up
rescans that repository in full (`dispatch._unfinished_batches`). `status`
and doctor read only the index format, so they called it up to date. They
now apply catch-up's own rule: a marker of an ended process on this host is
`rescan pending (interrupted batch)`; a running process's, or another
host's, is not.
"""

import socket
import subprocess
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from devgraph.cli import main as cli_main
from devgraph.graph.engine import GraphEngine
from devgraph.indexer.dispatch import INDEX_FORMAT
from devgraph.registry.store import RepoRegistry


def _ended_token() -> str:
    ended = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    return f"{int(ended.stdout)}@{socket.gethostname()}/0123abcd#3"


def _running_token() -> str:
    import os

    return f"{os.getppid()}@{socket.gethostname()}/4567cdef#1"


class _MarkedEngine:
    """A reachable Neo4j where every index is current, with `markers` per repository."""

    markers: dict[str, list[str]] = {}

    def __init__(self, *args, **kwargs):
        pass

    def verify_connectivity(self):
        pass

    def probe_connectivity(self):
        pass

    def init_schema(self):
        pass

    def close(self, timeout=None):
        pass

    def index_format(self, repo_id):
        return INDEX_FORMAT

    def open_batches(self, repo_id):
        return list(self.markers.get(repo_id, []))

    def graph_repo_ids(self):
        return set(self.markers)

    def run_cypher(self, query, params=None):
        return []


def _settings(tmp_path):
    return SimpleNamespace(
        registry_db_path=tmp_path / "registry.sqlite3",
        neo4j_uri="bolt://127.0.0.1:7687", neo4j_user="neo4j", neo4j_password="devgraph-local-dev",
        health_check_interval_s=30, neo4j_probe_timeout_s=5.0, dashboard_port=7690,
    )


def _registered(tmp_path, markers):
    registry = RepoRegistry(tmp_path / "registry.sqlite3")
    for repo_id in markers:
        root = tmp_path / repo_id
        (root / ".git").mkdir(parents=True)
        registry.add_repo(root, repo_id=repo_id)
        registry.mark_indexed(repo_id)
    registry.close()
    return type("Engine", (_MarkedEngine,), {"markers": markers})


def _markers():
    return {
        "zz-cut": [_ended_token()],
        "zz-running": [_running_token()],
        "zz-elsewhere": ["4242@another-host.example/89abcdef#2"],
        "zz-clean": [],
    }


def _flat(text):
    return " ".join(text.split())


def test_status_names_an_interrupted_batch(tmp_path):
    engine = _registered(tmp_path, _markers())
    with patch.object(cli_main, "get_settings", return_value=_settings(tmp_path)), \
         patch.object(cli_main, "GraphEngine", engine):
        result = CliRunner().invoke(cli_main.app, ["status"])
    section = _flat(result.stdout.split("Graph Index", 1)[1])
    assert "zz-cut: rescan pending (interrupted batch)" in section
    for repo_id in ("zz-running", "zz-elsewhere", "zz-clean"):
        assert f"{repo_id}:" not in section


def test_doctor_names_an_interrupted_batch(tmp_path):
    engine = _registered(tmp_path, _markers())
    with patch.object(cli_main, "get_settings", return_value=_settings(tmp_path)), \
         patch.object(cli_main, "GraphEngine", engine), \
         patch.object(cli_main, "resolve_podman", return_value=None), \
         patch.object(cli_main, "_schema_drift_findings", return_value=[]), \
         patch.object(cli_main, "_docs_source_findings", return_value=[]), \
         patch.object(cli_main, "_stale_schema_objects", return_value=[]), \
         patch("devgraph.indexer.schema_constraints.constraint_drift", return_value=[]):
        result = CliRunner().invoke(cli_main.app, ["doctor"])
    out = _flat(result.stdout)
    section = out.split("Graph index", 1)[1].split("Live Watcher", 1)[0]
    assert "zz-cut: rescan pending (interrupted batch)" in section
    for repo_id in ("zz-running", "zz-elsewhere", "zz-clean"):
        assert f"{repo_id}:" not in section
    assert "All checks passed" not in out


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield test_engine
    test_engine.close()


def test_a_marker_left_in_the_graph_is_read_as_catch_up_reads_it(engine):
    repo_id = f"zz-interrupted-{uuid.uuid4().hex[:8]}"
    try:
        engine.upsert_repository(repo_id, repo_id, "/tmp/zz-interrupted")
        engine.set_index_format(repo_id, INDEX_FORMAT)
        repo = SimpleNamespace(repo_id=repo_id, active=True, last_indexed="2026-10-01T00:00:00+00:00")
        assert cli_main._pending_rescans(engine, [repo]) == []
        engine.open_batch(repo_id, _ended_token())
        engine.close_batch(repo_id, [])  # writes the pending marker
        assert cli_main._pending_rescans(engine, [repo]) == [repo]
        assert cli_main._index_state(engine, repo_id) == cli_main.INTERRUPTED_BATCH
    finally:
        engine.delete_repository(repo_id)
