"""A dashboard port another process holds: the agent says so instead of failing
silently, the tray and `devgraph status` show it, and `devgraph dashboard`
never opens a browser on a port DevGraph doesn't serve."""

import http.server
import logging
import socket
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from devgraph.cli import main as cli_main
from devgraph.config.settings import Settings
from devgraph.dashboard.serving import DashboardFailure
from devgraph.dashboard.url import probe_dashboard
from tests.graph.test_engine_close import fake_engine


@pytest.fixture
def held_port():
    """A port another program holds, answering HTTP like something that isn't DevGraph."""
    server = http.server.HTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    server.RequestHandlerClass.log_message = lambda *a, **k: None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _headless(monkeypatch, port):
    engine, _driver = fake_engine(monkeypatch)
    settings = MagicMock(
        health_check_interval_s=60, dashboard_enabled=True, dashboard_host="127.0.0.1", dashboard_port=port
    )
    with patch("devgraph.agent.headless.get_settings", return_value=settings), \
         patch("devgraph.agent.headless.RepoRegistry"), \
         patch("devgraph.agent.headless.GraphEngine", return_value=engine), \
         patch("devgraph.agent.headless.WatcherManager"):
        from devgraph.agent.headless import HeadlessAgent

        return HeadlessAgent()


def test_a_held_port_is_a_clear_warning_not_a_silent_exit(monkeypatch, held_port, caplog):
    agent = _headless(monkeypatch, held_port)
    caplog.set_level(logging.WARNING)
    agent._run_dashboard()  # returns; a SystemExit would escape the test
    (record,) = [r for r in caplog.records if "dashboard" in r.getMessage()]
    message = record.getMessage()
    assert f"port {held_port}" in message and "DEVGRAPH_DASHBOARD_PORT" in message
    assert record.exc_info is None
    assert agent._dashboard_problem.detail == message


def test_the_dashboard_serves_and_identifies_itself(monkeypatch):
    port = _free_port()
    agent = _headless(monkeypatch, port)
    thread = threading.Thread(target=agent._run_dashboard, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 10
        while probe_dashboard(url) != "devgraph" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert probe_dashboard(url) == "devgraph"
        assert agent._dashboard_problem is None
    finally:
        agent._dashboard_server.should_exit = True
        thread.join(5)


def test_probe_tells_another_program_from_nothing(held_port):
    assert probe_dashboard(f"http://127.0.0.1:{held_port}") == "other"
    assert probe_dashboard(f"http://127.0.0.1:{_free_port()}") == "none"


def test_the_tray_tooltip_names_the_held_port():
    with patch("devgraph.agent.tray.get_settings", return_value=MagicMock()), \
         patch("devgraph.agent.tray.RepoRegistry"), \
         patch("devgraph.agent.tray.GraphEngine"), \
         patch("devgraph.agent.tray.WatcherManager"):
        from devgraph.agent.tray import TrayApp

        app = TrayApp()
    assert app._status_text() == "DevGraph (ok)"
    app._dashboard_problem = DashboardFailure("dashboard port 8765 in use", "dashboard port 8765 on 127.0.0.1 is ...")
    assert app._status_text() == "DevGraph (ok; dashboard port 8765 in use)"


def _settings(tmp_path, port):
    return Settings(registry_db_path=tmp_path / "registry.db", dashboard_host="127.0.0.1", dashboard_port=port)


def test_devgraph_dashboard_refuses_a_port_another_program_holds(tmp_path, held_port):
    with patch.object(cli_main, "get_settings", return_value=_settings(tmp_path, held_port)), \
         patch.object(cli_main, "_tray_liveness_text", return_value="running"), \
         patch.object(cli_main.webbrowser, "open") as browser:
        result = CliRunner().invoke(cli_main.app, ["dashboard"])
    assert result.exit_code == 1
    browser.assert_not_called()
    assert f"{held_port}" in result.stdout and "DEVGRAPH_DASHBOARD_PORT" in result.stdout


def test_devgraph_status_names_a_held_dashboard_port(tmp_path, held_port):
    with patch.object(cli_main, "get_settings", return_value=_settings(tmp_path, held_port)), \
         patch.object(cli_main, "GraphEngine") as engine_cls:
        engine_cls.return_value.verify_connectivity.return_value = None
        engine_cls.return_value.index_format.return_value = None
        result = CliRunner().invoke(cli_main.app, ["status"])
    assert "Dashboard" in result.stdout
    assert f"port {held_port} is held by another program" in result.stdout
