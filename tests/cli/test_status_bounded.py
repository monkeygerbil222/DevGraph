"""`devgraph status` and `doctor` with Neo4j gone behind a forwarder that still
accepts connections (audit 11, item 3): they hung (status for over 200 s, with
the driver's 30 s connection timeout retried); now their check is one try
bounded by `neo4j_probe_timeout_s`. A silent listener stands in, never the real
Neo4j."""

import socket
import threading
import time
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from devgraph.cli import main as cli_main
from devgraph.config.settings import Settings
from devgraph.graph import engine as engine_module
from devgraph.graph.engine import GraphEngine
from devgraph.mcp import server as mcp_server

#: The real check: without a local Neo4j (the Windows CI job), tests/conftest.py
#: stubs it to fail at once, which would pass these tests without running it.
_REAL_VERIFY = GraphEngine.verify_connectivity


@pytest.fixture
def silent_uri():
    """Accepts connections and never answers the Bolt handshake."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(50)
    held = []

    def accept():
        while True:
            try:
                held.append(listener.accept()[0])
            except OSError:
                return

    threading.Thread(target=accept, daemon=True).start()
    yield f"bolt://127.0.0.1:{listener.getsockname()[1]}"
    listener.close()
    for conn in held:
        conn.close()


def _invoke_bounded(args, seconds=60.0):
    """The CLI run's result, failing the test (not hanging the run) past `seconds`."""
    outcome = {}
    worker = threading.Thread(
        target=lambda: outcome.setdefault("result", CliRunner().invoke(cli_main.app, args, terminal_width=400)),
        daemon=True,
    )
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), f"devgraph {' '.join(args)} still running after {seconds:.0f} s"
    return outcome["result"]


@pytest.mark.parametrize("command", ["status", "doctor"])
def test_the_connection_check_gives_up_within_the_probe_timeout(command, silent_uri, tmp_path, monkeypatch):
    monkeypatch.setattr(GraphEngine, "verify_connectivity", _REAL_VERIFY)
    monkeypatch.setenv("DEVGRAPH_REGISTRY_DB_PATH", str(tmp_path / "home" / "registry.sqlite3"))
    settings = Settings(
        registry_db_path=tmp_path / "home" / "registry.sqlite3",
        neo4j_uri=silent_uri,
        neo4j_probe_timeout_s=1.0,
        dashboard_port=9,
    )
    with patch.object(cli_main, "get_settings", return_value=settings):
        started = time.monotonic()
        result = _invoke_bounded([command])
    elapsed = time.monotonic() - started
    assert elapsed < 10.0, f"{command} took {elapsed:.1f} s"
    assert elapsed >= 0.9, "the real connection check did not run (it waits out the 1 s probe timeout)"
    assert result.exit_code == 1
    (line,) = [line for line in result.output.splitlines() if "Not reachable" in line]
    assert f"nothing answers at {silent_uri}" in line


def test_the_mcp_connection_timeout_is_a_setting(monkeypatch):
    seen = {}

    def driver(uri, **options):
        seen.update(options)
        return object()

    monkeypatch.setattr(engine_module.GraphDatabase, "driver", driver)
    mcp_server.mcp_engine(Settings(mcp_connection_timeout_s=3.5))
    assert seen["connection_timeout"] == seen["connection_acquisition_timeout"] == 3.5
    # Only a connection idle this long is checked before use: one RESET per idle spell, not per query.
    assert seen["liveness_check_timeout"] == 10.0


def test_the_default_limits():
    defaults = Settings()
    assert defaults.mcp_connection_timeout_s == 5.0
    assert defaults.neo4j_probe_timeout_s == 5.0
