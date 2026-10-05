"""Constants the sandbox shares with the rest of DevGraph have one source."""

import inspect

from devgraph import git_safe, paths
from devgraph.cli import main as cli_main
from devgraph.config import project_schema
from devgraph.sandbox import consent, reader, selection
from devgraph.sandbox.digest import SHORT_DIGEST_LENGTH


def test_schema_file_name_and_read_cap_are_the_config_ones():
    assert not hasattr(reader, "SCHEMA_FILE")
    assert reader.SCHEMA_FILENAME is project_schema.SCHEMA_FILENAME
    assert reader.MAX_CONFIG_BYTES is paths.MAX_CONFIG_BYTES
    assert "cap=MAX_CONFIG_BYTES" in inspect.getsource(reader.read_schema_file)


def test_git_minimum_is_the_lazy_fetch_guard_minimum():
    assert selection.GIT_MIN_VERSION is git_safe.LAZY_FETCH_GUARD_MIN_VERSION


def test_provider_script_path_has_one_helper():
    assert reader.provider_script_path("runbook_links") == ".devgraph/providers/runbook_links.py"
    for module in (consent, cli_main):  # reader holds the helper
        assert ".devgraph/providers/{" not in inspect.getsource(module), module.__name__


def test_revoke_digest_prefix_uses_the_short_digest_length():
    source = inspect.getsource(cli_main.scripts_revoke)
    assert "{12,64}" not in source and "SHORT_DIGEST_LENGTH" in source
    assert SHORT_DIGEST_LENGTH == 12
