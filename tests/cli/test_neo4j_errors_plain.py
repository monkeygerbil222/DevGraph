"""Neo4j down or refusing the login is one plain line naming what to fix, in
every command that reads the graph (audit 11, item 7). `rescan`, `stats`,
`export`, `insights`, `self-test` and `prune` printed the driver's raw error
(`{neo4j_code: Neo.ClientError.Security.Unauthorized} {message: ...}
{gql_status: 50N42} ...`), as `status`, `doctor` and `register` no longer do.
A stub engine raises the errors: no login is ever tried against the real
Neo4j, so its rate limit is never tripped."""

import subprocess
from unittest.mock import patch

import pytest
from neo4j.exceptions import AuthError, Neo4jError, ServiceUnavailable
from typer.testing import CliRunner

from devgraph.cli import main as cli_main
from devgraph.config.settings import Settings
from devgraph.registry.store import RepoRegistry

URI = "bolt://127.0.0.1:9"
RAW = "{neo4j_code: Neo.ClientError.Security.Unauthorized} {gql_status: 50N42}"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVGRAPH_REGISTRY_DB_PATH", str(tmp_path / "home" / "registry.sqlite3"))
    value = Settings(registry_db_path=tmp_path / "home" / "registry.sqlite3", neo4j_uri=URI)
    with patch.object(cli_main, "get_settings", return_value=value):
        yield value


@pytest.fixture
def repo(tmp_path, settings):
    path = tmp_path / "proj"
    path.mkdir()
    (path / "a.py").write_text("def f():\n    pass\n")
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    registry = RepoRegistry(settings.registry_db_path)
    try:
        return registry.add_repo(path).repo_id
    finally:
        registry.close()


class _Failing:
    """An engine whose every graph call raises `error`."""

    def __init__(self, error):
        self.error = error

    def __call__(self, *args, **kwargs):
        return self

    def close(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise self.error

        return fail


ERRORS = {
    "wrong password": (AuthError(RAW), "refused the username or password"),
    "rate limited": (
        Neo4jError._hydrate_neo4j(code="Neo.ClientError.Security.AuthenticationRateLimit", message="too many attempts"),
        "refused the login (too many attempts)",
    ),
    "down": (ServiceUnavailable(f"Couldn't connect to 127.0.0.1:9 {RAW}"), f"nothing answers at {URI}"),
}


@pytest.mark.parametrize("error", ERRORS, ids=list(ERRORS))
@pytest.mark.parametrize("command", ["rescan", "stats", "stats-all", "export", "insights", "self-test", "prune"])
def test_a_neo4j_failure_is_one_plain_line(command, error, settings, repo):
    exc, phrase = ERRORS[error]
    args = {
        "stats-all": ["stats"], "prune": ["prune", "--dry-run"], "self-test": ["self-test"],
    }.get(command, [command, repo])
    with patch.object(cli_main, "GraphEngine", _Failing(exc)):
        result = CliRunner().invoke(cli_main.app, args, terminal_width=400)
    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    assert "neo4j_code" not in result.output and "Couldn't connect" not in result.output
    (line,) = [line for line in result.output.splitlines() if "[X]" in line]
    assert phrase in line
    assert "DEVGRAPH_NEO4J" in line
