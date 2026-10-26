"""First-run CLI wording (audit 11, item 7): `update --force` says what it does,
and `mcp add` without Claude Code points to `devgraph client-config`."""

from unittest.mock import patch

from typer.testing import CliRunner

from devgraph.cli import main as cli_main


def test_update_force_help_does_not_promise_a_stash():
    result = CliRunner().invoke(cli_main.app, ["update", "--help"], terminal_width=400)
    assert result.exit_code == 0
    text = " ".join(result.output.split())
    assert "stash first" not in text
    assert "Nothing is stashed" in text


def test_mcp_add_without_claude_points_to_client_config():
    with patch.object(cli_main.shutil, "which", return_value=None):
        result = CliRunner().invoke(cli_main.app, ["mcp", "add"], terminal_width=400)
    assert result.exit_code == 1
    text = " ".join(result.output.split())
    assert "'claude' not found on PATH" in text
    assert "devgraph client-config" in text
