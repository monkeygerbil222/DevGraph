"""find_related_prs and issue_history_for never reach the network: with the
repository's PR or issue source off they say how to turn it on."""

import subprocess
from pathlib import Path

import pytest

from devgraph.mcp import tools
from devgraph.mcp.tools import _resolve_gh_repo, find_related_prs, issue_history_for
from devgraph.registry.store import RepoRecord


class _Registry:
    def __init__(self, record):
        self.record = record

    def get(self, repo_id):
        return self.record if repo_id == self.record.repo_id else None


class _Graph:
    def __init__(self):
        self.queries = []

    def run_read_cypher(self, query, params, *, timeout_s, max_rows):
        self.queries.append(query)
        return [], False


@pytest.fixture
def no_subprocess(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError(f"a subprocess was started: {args}")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


@pytest.mark.parametrize(
    ("tool", "command"),
    [(find_related_prs, "devgraph pr-source widget enable"), (issue_history_for, "devgraph issue-source widget enable")],
)
def test_a_source_that_is_off_returns_a_notice_and_runs_nothing(tmp_path, no_subprocess, tool, command):
    registry = _Registry(RepoRecord("widget", tmp_path, True, True, None))
    graph = _Graph()
    result = tool(graph, "widget", "widget/core.py", registry=registry)
    assert result["count"] == 0 and result["results"] == [] and result["truncated"] is False
    assert command in result["notice"]
    assert graph.queries == []


@pytest.mark.parametrize(
    ("tool", "flag"), [(find_related_prs, "pr_source_enabled"), (issue_history_for, "issue_source_enabled")]
)
def test_a_source_that_is_on_reads_the_graph(tmp_path, no_subprocess, tool, flag):
    registry = _Registry(RepoRecord("widget", tmp_path, True, True, None, **{flag: True}))
    graph = _Graph()
    result = tool(graph, "widget", "widget/core.py", registry=registry)
    assert "notice" not in result
    assert len(graph.queries) == 1


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("git@github.com:acme/toolkit.git", "acme/toolkit"),
        ("https://github.com/acme/widget", "acme/widget"),
        ("https://github.com/acme/widget.git", "acme/widget"),
        ("https://gitlab.example.com/acme/widget.git", None),
    ],
)
def test_resolve_gh_repo_strips_only_a_git_suffix(tmp_path: Path, url, slug):
    subprocess.check_call(["git", "init", "-q", str(tmp_path)])
    subprocess.check_call(["git", "-C", str(tmp_path), "remote", "add", "origin", url])
    assert _resolve_gh_repo(str(tmp_path)) == slug


def test_no_gh_fallback_is_left():
    assert not hasattr(tools, "_gh_pr_list") and not hasattr(tools, "_gh_issue_list")
