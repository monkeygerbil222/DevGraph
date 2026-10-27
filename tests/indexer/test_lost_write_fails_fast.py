"""A write that lost its Neo4j connection is retried for seconds, not 30 s
(audit 11 follow-up).

The driver retries a managed transaction for up to 30 s after any retryable
error, a lost connection included, so a batch losing Neo4j waited about 31 s
before it aborted with `Neo4jLost`. The engine now runs that retry itself:
a lost connection (`ServiceUnavailable`) is retried for `CONNECTION_RETRY_S`,
while a transient error (a deadlock, a lock timeout) or an expired session (a
cluster's leader switch) keeps the whole `TRANSACTION_RETRY_S` window.

The live test loses the connection for real through a forwarder of its own
on a free port; the shared one and the Neo4j container are never touched.
"""

import subprocess
import time

import pytest
from neo4j.exceptions import ClientError, ServiceUnavailable, SessionExpired, TransientError

from devgraph.graph import engine as engine_module
from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer import dispatch
from devgraph.indexer.dispatch import Neo4jLost, full_scan
from tests.indexer.test_neo4j_lost_relink import AUTH, NEO4J, _Forwarder
from tests.watcher.live_helpers import live_repo_id, sweep_stale_live_repos


class _FlakySession:
    """A driver session whose transactions fail with `errors(n)` (n counting
    calls from 1) until it returns None; each failing call takes `slow_s`."""

    def __init__(self, errors, slow_s=0.0):
        self.errors, self.slow_s, self.calls = errors, slow_s, 0

    def _call(self, fn):
        self.calls += 1
        error = self.errors(self.calls)
        if error is not None:
            time.sleep(self.slow_s)
            raise error
        return fn(None)

    def execute_write(self, fn, *args, **kwargs):
        return self._call(fn)

    def execute_read(self, fn, *args, **kwargs):
        return self._call(fn)


@pytest.fixture
def short_windows(monkeypatch):
    monkeypatch.setattr(engine_module, "TRANSACTION_RETRY_S", 3.0)
    monkeypatch.setattr(engine_module, "CONNECTION_RETRY_S", 0.6)
    monkeypatch.setattr(engine_module, "_TRANSACTION_RETRY_DELAY_S", 0.1)


def _session(errors, slow_s=0.0, retry=True):
    inner = _FlakySession(errors, slow_s)
    return engine_module._IndexWaitingSession(inner, retry=retry), inner


@pytest.mark.parametrize("method", ["execute_write", "execute_read"])
def test_a_lost_connection_is_retried_only_briefly(short_windows, method):
    session, inner = _session(lambda n: ServiceUnavailable("connection lost"))
    started = time.monotonic()
    with pytest.raises(ServiceUnavailable):
        getattr(session, method)(lambda tx: "done")
    assert time.monotonic() - started < 2.0
    assert inner.calls >= 2  # retried, if briefly


@pytest.mark.parametrize(
    "error",
    [TransientError("deadlock"), SessionExpired("leader switched")],
    ids=["deadlock", "leader-switch"],
)
def test_a_transient_error_keeps_the_whole_retry_window(short_windows, error):
    # Failing for about 1.5 s, past the connection window, then answering.
    session, inner = _session(lambda n: error if n <= 3 else None, slow_s=0.4)
    assert session.execute_write(lambda tx: "done") == "done"
    assert inner.calls == 4


def test_a_transient_error_past_the_window_is_raised(short_windows):
    session, _ = _session(lambda n: TransientError("deadlock"), slow_s=0.5)
    started = time.monotonic()
    with pytest.raises(TransientError):
        session.execute_write(lambda tx: "done")
    assert 3.0 <= time.monotonic() - started < 6.0


def test_an_error_that_is_not_retryable_is_raised_at_once(short_windows):
    session, inner = _session(lambda n: ClientError("syntax"))
    with pytest.raises(ClientError):
        session.execute_write(lambda tx: "done")
    assert inner.calls == 1


def test_a_fail_fast_engine_runs_one_try(short_windows):
    session, inner = _session(lambda n: TransientError("deadlock"), retry=False)
    with pytest.raises(TransientError):
        session.execute_write(lambda tx: "done")
    assert inner.calls == 1


def test_the_driver_retries_nothing_itself():
    """The engine's retry replaces the driver's, which would add its 30 s."""
    for options in ({}, {"fail_fast": True, "connection_timeout_s": 2.0}):
        engine = GraphEngine("bolt://127.0.0.1:9", "neo4j", "x", **options)
        try:
            with engine._driver._driver.session() as session:
                assert session._config.max_transaction_retry_time < 0
        finally:
            engine.close(timeout=0.1)


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri=f"bolt://{NEO4J[0]}:{NEO4J[1]}", user=AUTH[0], password=AUTH[1])
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield test_engine
    test_engine.close()


@pytest.fixture
def repo_id(engine):
    sweep_stale_live_repos(engine, "zz-lostfast")
    repo = live_repo_id("zz-lostfast")
    yield repo
    engine.delete_repository(repo)


def test_a_batch_losing_neo4j_aborts_within_seconds(engine, repo_id, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
    (root / "a.py").write_text("def other():\n    return 1\n")
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root)
    forwarder = _Forwarder()
    # The agent's engine: no option set, so the indexing defaults.
    agent = GraphEngine(uri=f"bolt://127.0.0.1:{forwarder.port}", user=AUTH[0], password=AUTH[1])
    try:
        real = agent.replace_file_nodes

        def lose_then_write(*args, **kwargs):  # lost as the batch's first write starts
            forwarder.drop()
            return real(*args, **kwargs)

        agent.replace_file_nodes = lose_then_write
        (root / "a.py").write_text("def other():\n    return 2\n")
        started = time.monotonic()
        with pytest.raises(Neo4jLost):
            dispatch.index_paths(agent, repo_id, root, {root / "a.py"})
        elapsed = time.monotonic() - started
        # The driver's own retry took about 31 s here.
        assert elapsed < 15, f"aborted after {elapsed:.1f} s"
    finally:
        agent.close(timeout=0.5)
        forwarder.close()
