"""CLI exit codes and error output when Neo4j is down, refuses the password, or
the registry is unreadable: a non-zero exit, a clean message, a way forward."""

import logging
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from neo4j.exceptions import AuthError, ServiceUnavailable
from typer.testing import CliRunner

from devgraph.cli import main as cli_main
from devgraph.config.settings import Settings
from devgraph.graph import engine as engine_module
from devgraph.registry.store import RepoRegistry


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVGRAPH_REGISTRY_DB_PATH", str(tmp_path / "home" / "registry.sqlite3"))
    value = Settings(
        registry_db_path=tmp_path / "home" / "registry.sqlite3",
        neo4j_uri="bolt://127.0.0.1:9",
        dashboard_port=9,
    )
    with patch.object(cli_main, "get_settings", return_value=value):
        yield value


@pytest.fixture
def repo(tmp_path, settings):
    path = tmp_path / "proj"
    path.mkdir()
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    registry = RepoRegistry(settings.registry_db_path)
    try:
        return registry.add_repo(path).repo_id
    finally:
        registry.close()


def _registered(settings):
    registry = RepoRegistry(settings.registry_db_path)
    try:
        return [r.repo_id for r in registry.list_repos()]
    finally:
        registry.close()


def _engine_raising(exc):
    engine = MagicMock()
    engine.verify_connectivity.side_effect = exc
    engine.read_applied_schema.side_effect = exc
    engine.upsert_repository.side_effect = exc
    return engine


def _line(output, phrase):
    (line,) = [line for line in output.splitlines() if phrase in line]
    return line


def test_status_with_neo4j_down_exits_non_zero_with_one_clean_line(settings, monkeypatch, caplog):
    monkeypatch.setattr(engine_module, "_BASE_DELAY_S", 0.001)
    caplog.set_level(logging.WARNING)
    result = CliRunner().invoke(cli_main.app, ["status"], terminal_width=400)
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    line = _line(result.output, "Not reachable")
    assert "127.0.0.1:9" in line
    assert all(r.exc_info is None for r in caplog.records if "transient Neo4j error" in r.getMessage())


def test_a_retry_warning_counts_tries_and_carries_no_traceback(monkeypatch, caplog):
    monkeypatch.setattr(engine_module, "_BASE_DELAY_S", 0.001)
    caplog.set_level(logging.WARNING)

    def refuse():
        raise ServiceUnavailable("Couldn't connect to 127.0.0.1:9")

    with pytest.raises(ServiceUnavailable):
        engine_module._retry_transient(refuse)
    retries = [r for r in caplog.records if "transient Neo4j error" in r.getMessage()]
    assert all(r.exc_info is None for r in retries)
    assert [r.getMessage() for r in retries] == [
        f"transient Neo4j error on try {n} of 4, retrying in {d}s: Couldn't connect to 127.0.0.1:9"
        for n, d in ((1, "0.0"), (2, "0.0"), (3, "0.0"))
    ]


def test_a_wrong_password_names_the_settings_file(settings):
    engine = _engine_raising(AuthError("The client is unauthorized due to authentication failure."))
    with patch.object(cli_main, "GraphEngine", return_value=engine):
        result = CliRunner().invoke(cli_main.app, ["status"], terminal_width=400)
    assert result.exit_code == 1
    line = _line(result.output, "Not reachable")
    assert "DEVGRAPH_NEO4J_PASSWORD" in line
    assert str(settings.registry_db_path.parent / ".env") in line


def test_add_exits_non_zero_when_its_scan_fails(settings, tmp_path):
    path = tmp_path / "fresh"
    path.mkdir()
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    engine = _engine_raising(ServiceUnavailable("Couldn't connect to 127.0.0.1:9"))
    with patch.object(cli_main, "GraphEngine", return_value=engine):
        result = CliRunner().invoke(cli_main.app, ["add", str(path)], terminal_width=400)
    assert result.exit_code == 1
    assert "Registered but initial scan failed" in result.output
    assert "devgraph rescan fresh" in result.output
    assert _registered(settings) == ["fresh"]


def test_remove_with_neo4j_down_keeps_the_repo_and_suggests_keep_graph(settings, repo):
    engine = _engine_raising(ServiceUnavailable("Couldn't connect to 127.0.0.1:9"))
    with patch.object(cli_main, "GraphEngine", return_value=engine):
        result = CliRunner().invoke(cli_main.app, ["remove", repo], terminal_width=400)
    assert result.exit_code == 1
    assert "--keep-graph" in result.output
    assert "Traceback" not in result.output
    assert _registered(settings) == [repo]


def test_remove_keep_graph_unregisters_without_touching_the_graph(settings, repo):
    with patch.object(cli_main, "GraphEngine", side_effect=AssertionError("graph touched")):
        result = CliRunner().invoke(cli_main.app, ["remove", repo, "--keep-graph"], terminal_width=400)
    assert result.exit_code == 0, result.output
    assert "graph data kept" in result.output
    assert _registered(settings) == []


def test_a_corrupt_registry_error_names_its_path(settings):
    settings.registry_db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.registry_db_path.write_bytes(b"this is not a sqlite database" * 200)
    result = CliRunner().invoke(cli_main.app, ["list"], terminal_width=400)
    assert result.exit_code == 1
    assert str(settings.registry_db_path) in result.output
