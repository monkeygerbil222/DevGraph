"""Repository-configured programs must not run when DevGraph reads a repo.

Each test builds a throwaway repository whose config/attributes name a marker
script (it only touches a file under tmp), checks with plain `git` that the
setup really does run the marker, then runs DevGraph's own function against
the repo and asserts the marker did not run.

Call sites that need a graph (history resync, impact analysis) use the local
Neo4j test instance and skip without it.
"""

from __future__ import annotations

import logging
import subprocess
import uuid
from pathlib import Path

import pytest

from devgraph import git_safe
from devgraph.dashboard.git_info import get_git_log, get_git_status
from devgraph.indexer.git_history.extractor import GitHistoryExtractor, sync_git_history
from devgraph.mcp.tools import _resolve_gh_repo, impact_analysis_for_diff
from devgraph.registry.store import RepoRegistry

V1 = "def f():\n    return 1\n"
V2 = "def f():\n    return 2\n"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _git_ok(cwd: Path, *args: str) -> None:
    result = _git(cwd, *args)
    assert result.returncode == 0, result.stderr


def _commit_history(path: Path) -> None:
    _git_ok(path, "init", "-q")
    _git_ok(path, "config", "user.email", "test@example.com")
    _git_ok(path, "config", "user.name", "Test Author")
    (path / "a.py").write_text(V1)
    _git_ok(path, "add", "a.py")
    _git_ok(path, "commit", "-qm", "first")
    (path / "a.py").write_text(V2)
    _git_ok(path, "commit", "-qam", "second")


def _make_repo(path: Path) -> Path:
    path.mkdir()
    _commit_history(path)
    return path


def _make_partial_clone(tmp_path: Path) -> Path:
    """A blob:none clone with HEAD checked out but the older a.py blob absent."""
    src = tmp_path / "src"
    src.mkdir()
    _commit_history(src)
    _git_ok(src, "config", "uploadpack.allowFilter", "true")
    clone = tmp_path / "clone"
    _git_ok(tmp_path, "clone", "-q", "--filter=blob:none", f"file://{src}", str(clone))
    return clone


class Marker:
    """A script that only touches `<tmp>/fired-<name>` when run."""

    def __init__(self, tmp_path: Path, name: str) -> None:
        self.flag = tmp_path / f"fired-{name}"
        self.script = tmp_path / f"marker-{name}.sh"
        self.script.write_text(f"#!/bin/sh\ntouch '{self.flag}'\nexit 1\n")
        self.script.chmod(0o755)

    def fired(self) -> bool:
        return self.flag.exists()

    def assert_live(self, repo: Path, *git_args: str) -> None:
        """Plain git really runs the marker for this setup; then reset."""
        _git(repo, *git_args)
        assert self.fired(), f"setup check: plain `git {' '.join(git_args)}` should run the marker"
        self.flag.unlink()


@pytest.fixture
def graph_engine():
    from devgraph.graph.engine import GraphEngine

    engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    engine.init_schema()
    yield engine
    engine.close()


@pytest.fixture
def registry(tmp_path):
    reg = RepoRegistry(tmp_path / "registry.db")
    yield reg
    reg.close()


@pytest.fixture
def graph_repo(request, registry):
    """Register a repo and index a.py so history resync blames it."""
    from devgraph.indexer.python.extractor import index_file

    engine = request.getfixturevalue("graph_engine")
    repo_ids: list[str] = []

    def register(path: Path) -> str:
        repo_id = f"_hgit_{uuid.uuid4().hex[:12]}"
        repo_ids.append(repo_id)
        registry.add_repo(path, repo_id=repo_id)
        index_file(engine, repo_id, path / "a.py", repo_root=path)
        return repo_id

    yield engine, register
    for repo_id in repo_ids:
        engine.delete_repository(repo_id)


def _run_site(site: str, path: Path, request) -> None:
    if site == "git_log":
        assert len(get_git_log(path, limit=10)) == 2
    elif site == "git_status":
        get_git_status(path)
    elif site == "history_extract":
        assert len(GitHistoryExtractor("_hgit_extract", path).extract_new_commits().commits) == 2
    elif site == "history_resync":
        engine, register = request.getfixturevalue("graph_repo")
        registry = request.getfixturevalue("registry")
        sync_git_history(engine, registry, register(path))
    elif site == "impact_diff":
        engine, register = request.getfixturevalue("graph_repo")
        registry = request.getfixturevalue("registry")
        outcome = impact_analysis_for_diff(engine, registry, register(path), "HEAD~1", "HEAD")
        assert outcome["changed_files"] == ["a.py"], outcome
    else:
        raise AssertionError(site)


SITES = ["git_log", "git_status", "history_extract", "history_resync", "impact_diff"]


@pytest.mark.parametrize("site", SITES)
def test_fsmonitor_not_run(site, tmp_path, request):
    repo = _make_repo(tmp_path / "repo")
    marker = Marker(tmp_path, "fsmonitor")
    _git_ok(repo, "config", "core.fsmonitor", str(marker.script))
    marker.assert_live(repo, "status")

    _run_site(site, repo, request)
    assert not marker.fired()


@pytest.mark.parametrize("site", SITES)
def test_hooks_path_hook_not_run(site, tmp_path, request):
    repo = _make_repo(tmp_path / "repo")
    marker = Marker(tmp_path, "hook")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for hook in ("post-index-change", "post-checkout", "pre-commit", "reference-transaction"):
        (hooks / hook).symlink_to(marker.script)
    _git_ok(repo, "config", "core.hooksPath", str(hooks))
    (repo / "a.py").touch()  # stat change so status rewrites the index
    marker.assert_live(repo, "status")
    (repo / "a.py").write_text(V2)  # stat change again for DevGraph's run

    _run_site(site, repo, request)
    assert not marker.fired()


@pytest.mark.parametrize("site", SITES)
def test_include_path_cannot_reenable_fsmonitor(site, tmp_path, request):
    repo = _make_repo(tmp_path / "repo")
    marker = Marker(tmp_path, "include")
    (repo / "tracked.cfg").write_text(f"[core]\n\tfsmonitor = {marker.script}\n")
    _git_ok(repo, "config", "include.path", "../tracked.cfg")
    marker.assert_live(repo, "status")

    _run_site(site, repo, request)
    assert not marker.fired()


def test_blame_textconv_not_run(tmp_path, request):
    repo = _make_repo(tmp_path / "repo")
    marker = Marker(tmp_path, "textconv")
    (repo / ".gitattributes").write_text("*.py diff=conv\n")
    _git_ok(repo, "config", "diff.conv.textconv", str(marker.script))
    marker.assert_live(repo, "blame", "HEAD", "--", "a.py")

    _run_site("history_resync", repo, request)
    assert not marker.fired()


def _ext_remote(clone: Path, marker: Marker) -> None:
    _git_ok(clone, "config", "remote.origin.url", f"ext::{marker.script} %S")
    _git_ok(clone, "config", "protocol.ext.allow", "always")


def _uploadpack_remote(clone: Path, marker: Marker) -> None:
    _git_ok(clone, "config", "remote.origin.uploadpack", str(marker.script))
    _git_ok(clone, "config", "protocol.file.allow", "always")


@pytest.mark.parametrize("configure", [_ext_remote, _uploadpack_remote], ids=["ext", "uploadpack"])
def test_partial_clone_lazy_fetch_not_run(configure, tmp_path, request):
    clone = _make_partial_clone(tmp_path)
    marker = Marker(tmp_path, "lazyfetch")
    configure(clone, marker)
    marker.assert_live(clone, "blame", "HEAD", "--", "a.py")

    _run_site("history_resync", clone, request)
    assert not marker.fired()


@pytest.mark.parametrize("configure", [_ext_remote, _uploadpack_remote], ids=["ext", "uploadpack"])
def test_lazy_fetch_blocked_by_config_alone(configure, tmp_path, request, monkeypatch):
    """On git without GIT_NO_LAZY_FETCH, the `-c` protocol pins still refuse
    the fetch even though the repo allows the protocol itself."""
    monkeypatch.delitem(git_safe.GIT_ENV, "GIT_NO_LAZY_FETCH", raising=False)
    monkeypatch.delitem(git_safe.GIT_ENV, "GIT_ALLOW_PROTOCOL", raising=False)
    clone = _make_partial_clone(tmp_path)
    marker = Marker(tmp_path, "lazyfetch")
    configure(clone, marker)
    marker.assert_live(clone, "blame", "HEAD", "--", "a.py")

    _run_site("history_resync", clone, request)
    assert not marker.fired()


@pytest.mark.parametrize("dropped", ["GIT_NO_LAZY_FETCH", "GIT_ALLOW_PROTOCOL"])
@pytest.mark.parametrize("configure", [_ext_remote, _uploadpack_remote], ids=["ext", "uploadpack"])
def test_lazy_fetch_blocked_by_one_env_var_alone(configure, dropped, tmp_path, request, monkeypatch):
    """Without the `-c` protocol pins, either lazy-fetch variable on its own
    still refuses the fetch."""
    overrides = tuple(o for o in git_safe.CONFIG_OVERRIDES if not o.startswith("protocol."))
    monkeypatch.setattr(git_safe, "CONFIG_OVERRIDES", overrides)
    monkeypatch.delitem(git_safe.GIT_ENV, dropped, raising=False)
    clone = _make_partial_clone(tmp_path)
    marker = Marker(tmp_path, "lazyfetch")
    configure(clone, marker)
    marker.assert_live(clone, "blame", "HEAD", "--", "a.py")

    _run_site("history_resync", clone, request)
    assert not marker.fired()


def test_inherited_index_file_is_ignored(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "repo")
    other = _make_repo(tmp_path / "other")
    (other / "only-in-other.txt").write_text("x")
    _git_ok(other, "add", "only-in-other.txt")
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))

    assert get_git_status(repo)["uncommitted"] == []


def test_old_git_logs_lazy_fetch_warning(monkeypatch, caplog):
    monkeypatch.setattr(git_safe.Git, "version_info", property(lambda self: (2, 39, 5)))
    git_safe._check_git_version.cache_clear()
    try:
        with caplog.at_level(logging.WARNING, logger="devgraph.git_safe"):
            git_safe._check_git_version()
    finally:
        git_safe._check_git_version.cache_clear()
    assert "GIT_NO_LAZY_FETCH" in caplog.text


def test_normal_repo_behaviour_unchanged(tmp_path, graph_repo, registry):
    repo = _make_repo(tmp_path / "repo")
    (repo / "a.py").write_text(V2 + "x = 1\n")
    (repo / "new.txt").write_text("n")

    log = get_git_log(repo, limit=10)
    assert [c["title"] for c in log] == ["second", "first"]
    assert log[0]["parents"] == [log[1]["hash"]]

    status = get_git_status(repo)
    assert sorted((e["path"], e["state"]) for e in status["uncommitted"]) == [
        ("a.py", "modified"),
        ("new.txt", "untracked"),
    ]

    extracted = GitHistoryExtractor("_hgit_normal", repo).extract_new_commits()
    assert [c.properties["message"] for c in extracted.commits] == ["first", "second"]
    assert {r.target_name for r in extracted.relationships} == {"a.py"}

    engine, register = graph_repo
    repo_id = register(repo)
    assert sync_git_history(engine, registry, repo_id)["mode"] == "initial"
    rows = engine.run_cypher(
        "MATCH (n:Function {repo_id: $repo_id, name: 'f'}) RETURN n.last_modified_at AS at",
        {"repo_id": repo_id},
    )
    assert rows and rows[0]["at"] == log[0]["date"]

    assert impact_analysis_for_diff(engine, registry, repo_id, "HEAD~1", "HEAD")["changed_files"] == ["a.py"]


@pytest.mark.parametrize(
    "url, expected",
    [
        ("git@github.com:octo-org/sample.repo.git", "octo-org/sample.repo"),
        ("https://github.com/octo-org/sample", "octo-org/sample"),
        ("https://github.com/octo-org/sample/../../other", None),
        ("https://github.com/octo org/sample", None),
        ("https://github.com/-R/sample", None),
        ("https://github.com/octo-org/sample;touch x", None),
    ],
)
def test_gh_repo_slug_is_sanitised(url, expected, tmp_path):
    repo = _make_repo(tmp_path / "repo")
    _git_ok(repo, "remote", "add", "origin", url)
    assert _resolve_gh_repo(str(repo)) == expected
