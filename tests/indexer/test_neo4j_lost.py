"""Losing Neo4j mid-index fails the batch or scan instead of skipping each
file as broken (audit 11 item 3): nothing is stamped, the agent retries, and
the catch-up after it leaves the graph equal to a fresh scan."""

import subprocess
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from neo4j.exceptions import ServiceUnavailable, SessionExpired

from devgraph.agent.sync import FAILURE_RETRY_DELAY_S, RepoSync
from devgraph.cli.main import _neo4j_problem
from devgraph.graph.engine import EngineClosed, GraphEngine, provision_repository_schema
from devgraph.indexer import dispatch
from devgraph.indexer.dispatch import Neo4jLost, catch_up, full_scan, index_outdated, index_paths
from devgraph.registry.store import RepoRegistry
from tests.helpers import stub
from tests.watcher.live_helpers import (
    fresh_snapshot,
    graph_snapshot,
    live_repo_id,
    snapshot_diff,
    sweep_stale_live_repos,
)


class _QuietEngine:
    def route_mount_state(self, repo_id, names=None):
        return {}

    def list_file_nodes(self, repo_id, files):
        return set()

    def read_applied_schema(self, repo_id):
        return None

    def update_skipped_files(self, repo_id, add=None, drop=(), replace=False):
        pass


def _files(tmp_path, n=3):
    paths = set()
    for i in range(n):
        (tmp_path / f"m{i}.py").write_text(f"x{i} = 1\n")
        paths.add(tmp_path / f"m{i}.py")
    return paths


@pytest.mark.parametrize("error", [ServiceUnavailable("connection lost"), SessionExpired("session expired")])
def test_a_lost_connection_aborts_the_batch_at_the_first_file(tmp_path, monkeypatch, error):
    paths = _files(tmp_path)
    tried = []

    def lost(*args):
        tried.append(args[4])
        raise error

    stub(monkeypatch, dispatch, "_index_single_path", lost)
    with pytest.raises(Neo4jLost) as raised:
        index_paths(_QuietEngine(), "_unit_lost", tmp_path, paths)
    assert tried == ["m0.py"]
    assert raised.value.__cause__ is error
    assert "m0.py" in str(raised.value) and "Neo4j stopped answering" in str(raised.value)
    # A caller reporting an unreachable Neo4j (the CLI) reports this one too.
    assert _neo4j_problem(raised.value, SimpleNamespace(neo4j_uri="bolt://db.example:7687")).startswith(
        "nothing answers at bolt://db.example:7687"
    )


def test_a_file_s_own_error_still_skips_only_that_file(tmp_path, monkeypatch):
    paths = _files(tmp_path)
    tried = []

    def broken(*args):
        tried.append(args[4])
        raise ValueError("unparseable")

    stub(monkeypatch, dispatch, "_index_single_path", broken)
    index_paths(_QuietEngine(), "_unit_lost", tmp_path, paths)
    assert tried == ["m0.py", "m1.py", "m2.py"]


def test_engine_closed_is_raised_as_itself(tmp_path, monkeypatch):
    paths = _files(tmp_path, 1)

    def closed(*args):
        raise EngineClosed()

    stub(monkeypatch, dispatch, "_index_single_path", closed)
    with pytest.raises(EngineClosed) as raised:
        index_paths(_QuietEngine(), "_unit_lost", tmp_path, paths)
    assert not isinstance(raised.value, Neo4jLost)


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield test_engine
    test_engine.close()


@pytest.fixture
def repo_id(engine):
    sweep_stale_live_repos(engine, "zz-lost")
    repo = live_repo_id("zz-lost")
    yield repo
    engine.delete_repository(repo)


def _repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
    for i in range(6):
        nxt = (i + 1) % 6
        (root / f"m{i}.py").write_text(f"from m{nxt} import f{nxt}\ndef f{i}():\n    return f{nxt}()\n")
    return root


def _lose(monkeypatch, repo_id, files):
    """Make the write of each of `files` in `repo_id` fail as a dropped connection does."""
    real = GraphEngine.replace_file_nodes

    def flaky(self, repo, file_name, *args, **kwargs):
        if repo == repo_id and file_name in files:
            raise ServiceUnavailable("simulated: connection to Neo4j lost")
        return real(self, repo, file_name, *args, **kwargs)

    stub(monkeypatch, GraphEngine, "replace_file_nodes", flaky)


def _assert_fresh(engine, repo_id, root):
    expected = fresh_snapshot(engine, repo_id, root)
    actual = graph_snapshot(engine, repo_id)
    assert actual == expected, snapshot_diff(expected, actual)


def test_a_full_scan_losing_neo4j_fails_unstamped_and_the_next_catch_up_heals_it(
    engine, repo_id, tmp_path, monkeypatch
):
    root = _repo(tmp_path)
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    with monkeypatch.context() as patched:
        _lose(patched, repo_id, {"m2.py", "m3.py"})
        with pytest.raises(Neo4jLost):
            full_scan(engine, repo_id, root)
    assert index_outdated(engine, repo_id)

    catch_up(engine, repo_id, root, datetime.now(timezone.utc))
    assert not index_outdated(engine, repo_id)
    _assert_fresh(engine, repo_id, root)


def test_a_live_batch_losing_neo4j_is_retried_and_converges(engine, repo_id, tmp_path, monkeypatch):
    root = _repo(tmp_path)
    registry = RepoRegistry(tmp_path / "registry.sqlite3")
    try:
        registry.add_repo(root, repo_id=repo_id)
        provision_repository_schema(engine, root)
        engine.upsert_repository(repo_id, repo_id, str(root))
        full_scan(engine, repo_id, root)
        before = datetime.now(timezone.utc) - timedelta(seconds=1)
        registry.mark_indexed(repo_id, at=before)
        time.sleep(0.05)
        requested = []
        sync = RepoSync(engine, registry, lambda event: None, lambda *args: requested.append(args))

        (root / "m1.py").write_text("from m2 import f2\ndef renamed():\n    return f2()\n")
        with monkeypatch.context() as patched:
            _lose(patched, repo_id, {"m1.py"})
            sync.on_changes(repo_id, {root / "m1.py"}, set())
        assert registry.get(repo_id).last_indexed == before.isoformat()  # not stamped past the lost batch
        assert [(rid, delay) for rid, _since, delay in requested] == [(repo_id, FAILURE_RETRY_DELAY_S)]

        assert sync.on_catch_up(repo_id, requested[0][1], "retry")
        _assert_fresh(engine, repo_id, root)
    finally:
        registry.close()

