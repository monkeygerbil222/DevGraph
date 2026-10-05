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
import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from git import GitCommandError, Repo

from devgraph import git_safe
from devgraph.dashboard.git_info import get_git_log, get_git_status
from devgraph.indexer.git_history.blame import compute_function_recency
from devgraph.indexer.git_history.extractor import GitHistoryExtractor, sync_git_history
from devgraph.mcp import tools
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


def _blame_through_open_repo(clone: Path) -> None:
    """History resync skips blame in a partial clone, so blame directly."""
    repo = git_safe.open_repo(clone)
    try:
        compute_function_recency(repo, "a.py")
    except GitCommandError:
        pass  # the older blob is missing and may not be fetched
    finally:
        repo.close()


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

    _blame_through_open_repo(clone)
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

    _blame_through_open_repo(clone)
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

    _blame_through_open_repo(clone)
    assert not marker.fired()


def test_inherited_index_file_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(git_safe, "_env_scrubbed", False)
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
        ("https://github.com/owner/widget", "owner/widget"),
        ("git@github.com:owner/widget.git", "owner/widget"),
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


def _attributes_in_tree(repo: Path, line: str) -> None:
    (repo / ".gitattributes").write_text(line)


def _attributes_in_info(repo: Path, line: str) -> None:
    info = repo / ".git" / "info"
    info.mkdir(exist_ok=True)
    (info / "attributes").write_text(line)


def _attributes_file(repo: Path, line: str) -> None:
    attributes = repo.parent / "attributes"
    attributes.write_text(line)
    _git_ok(repo, "config", "core.attributesFile", str(attributes))


@pytest.mark.parametrize(
    "assign", [_attributes_in_tree, _attributes_in_info, _attributes_file], ids=["gitattributes", "info", "attributesFile"]
)
@pytest.mark.parametrize("key", ["clean", "process"])
def test_repo_filter_driver_not_run(key, assign, tmp_path):
    repo = _make_repo(tmp_path / "repo")
    marker = Marker(tmp_path, "filter")
    _git_ok(repo, "config", f"filter.x.{key}", str(marker.script))
    assign(repo, "*.py filter=x\n")
    (repo / "a.py").write_text(V1)  # stat-dirty tracked file, so status filters it
    marker.assert_live(repo, "status")
    (repo / "a.py").write_text(V2 + "\n")

    assert "branch" in get_git_status(repo)
    assert not marker.fired()


def test_required_repo_filter_does_not_break_status(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    marker = Marker(tmp_path, "filter")
    _git_ok(repo, "config", "filter.x.clean", str(marker.script))
    _git_ok(repo, "config", "filter.x.required", "true")
    _attributes_in_tree(repo, "*.py filter=x\n")
    (repo / "a.py").write_text(V1)

    assert ("a.py", "modified") in {(e["path"], e["state"]) for e in get_git_status(repo)["uncommitted"]}
    assert not marker.fired()


def test_submodule_filter_driver_not_run(tmp_path):
    sub = _make_repo(tmp_path / "sub")
    repo = _make_repo(tmp_path / "repo")
    _git_ok(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "sub")
    _git_ok(repo, "commit", "-qm", "add submodule")
    marker = Marker(tmp_path, "subfilter")
    _git_ok(repo / "sub", "config", "filter.x.clean", str(marker.script))  # .git/modules/sub/config
    (repo / ".git" / "modules" / "sub" / "info" / "attributes").write_text("*.py filter=x\n")
    (repo / "sub" / "a.py").write_text(V1)
    marker.assert_live(repo, "status")
    (repo / "sub" / "a.py").write_text(V2 + "\n")

    get_git_status(repo)
    assert not marker.fired()


def test_slow_command_times_out(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "repo")
    slow = tmp_path / "slow.sh"
    slow.write_text("#!/bin/sh\nexec sleep 20\n")
    slow.chmod(0o755)
    _git_ok(repo, "config", "core.fsmonitor", str(slow))
    overrides = tuple(o for o in git_safe.CONFIG_OVERRIDES if not o.startswith("core.fsmonitor"))
    monkeypatch.setattr(git_safe, "CONFIG_OVERRIDES", overrides)
    monkeypatch.setattr(git_safe, "GIT_TIMEOUT_SECONDS", 1.0)

    started = time.monotonic()
    with pytest.raises(GitCommandError, match="did not complete in 1 secs"):
        get_git_status(repo)
    assert time.monotonic() - started < 10


def test_global_excludes_file_is_honoured(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "ignore").write_text("*.log\n")
    (home / ".gitconfig").write_text(f"[core]\n\texcludesFile = {home / 'ignore'}\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    repo = _make_repo(tmp_path / "repo")
    (repo / "debug.log").write_text("x")
    (repo / "new.txt").write_text("n")

    assert get_git_status(repo)["uncommitted"] == [{"path": "new.txt", "state": "untracked"}]


def _make_renaming_partial_clone(tmp_path: Path) -> Path:
    """A blob:none clone whose last commit renames a file and edits it; the
    pre-rename blob is absent, so rename detection would need a fetch."""
    src = tmp_path / "src"
    src.mkdir()
    _commit_history(src)
    body = "".join(f"line {i}\n" for i in range(40))
    (src / "notes.txt").write_text(body)
    _git_ok(src, "add", "notes.txt")
    _git_ok(src, "commit", "-qm", "add notes")
    _git_ok(src, "mv", "notes.txt", "renamed.txt")
    (src / "renamed.txt").write_text(body + "edited\n")
    _git_ok(src, "commit", "-qam", "rename notes")
    _git_ok(src, "config", "uploadpack.allowFilter", "true")
    clone = tmp_path / "clone"
    _git_ok(tmp_path, "clone", "-q", "--filter=blob:none", f"file://{src}", str(clone))
    no_fetch = subprocess.run(
        ["git", "diff", "--name-only", "-M", "HEAD~1", "HEAD"],
        cwd=str(clone),
        capture_output=True,
        env={**os.environ, "GIT_NO_LAZY_FETCH": "1"},
    )
    assert no_fetch.returncode != 0, "setup check: rename detection should need a missing blob"
    return clone


def test_partial_clone_history_resync_and_impact_diff(tmp_path, graph_repo, registry, caplog):
    clone = _make_renaming_partial_clone(tmp_path)

    extracted = GitHistoryExtractor("_hgit_partial", clone).extract_new_commits()
    last = extracted.commits[-1].sha
    assert {r.target_name for r in extracted.relationships if r.source_name == last} == {"notes.txt", "renamed.txt"}

    engine, register = graph_repo
    repo_id = register(clone)
    with caplog.at_level(logging.INFO, logger="devgraph.indexer.git_history.extractor"):
        assert sync_git_history(engine, registry, repo_id)["commits_indexed"] == 4
    assert caplog.text.count("partial clone") == 1
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    outcome = impact_analysis_for_diff(engine, registry, repo_id, "HEAD~1", "HEAD")
    assert "error" not in outcome
    assert sorted(outcome["changed_files"]) == ["notes.txt", "renamed.txt"]


def test_impact_diff_opens_repo_through_open_repo(tmp_path, graph_repo, registry, monkeypatch):
    repo = _make_repo(tmp_path / "repo")
    engine, register = graph_repo
    repo_id = register(repo)

    opened: list[type] = []
    real_init = Repo.__init__

    def spy_init(self, *args, **kwargs):
        opened.append(type(self))
        real_init(self, *args, **kwargs)

    via_open_repo: list[str] = []
    real_open_repo = tools.open_repo

    def spy_open_repo(path):
        via_open_repo.append(str(path))
        return real_open_repo(path)

    monkeypatch.setattr(Repo, "__init__", spy_init)
    monkeypatch.setattr(tools, "open_repo", spy_open_repo)

    assert impact_analysis_for_diff(engine, registry, repo_id, "HEAD~1", "HEAD")["changed_files"] == ["a.py"]
    assert via_open_repo == [str(repo)]
    assert opened == [git_safe.HardenedRepo]
