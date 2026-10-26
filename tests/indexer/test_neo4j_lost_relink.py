"""A live batch that loses Neo4j after writing its files' nodes, before it
relinked other files' edges to them, still converges (audit 11 review,
item 1): its retry could diff only against the nodes the lost batch had
already written, find nothing added, and leave `user()`'s call to the new
`helper()` out. The batch leaves a `batch_open` marker on the Repository
node, so the next catch-up, even in another process after a restart or a
kill, rescans the repository in full.

The connection is lost for real: the agent's engine talks to Neo4j through a
forwarder this test runs on a port of its own, and the forwarder drops every
connection as the batch reaches `_relink_name_refs`."""

import os
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone

import pytest

from devgraph.agent.sync import RepoSync
from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer import dispatch
from devgraph.indexer.dispatch import full_scan
from devgraph.registry.store import RepoRegistry
from tests.helpers import stub
from tests.watcher.live_helpers import (
    fresh_snapshot,
    graph_snapshot,
    live_repo_id,
    snapshot_diff,
    sweep_stale_live_repos,
)

NEO4J = ("127.0.0.1", 7687)
AUTH = ("neo4j", "devgraph-local-dev")


class _Forwarder:
    """A Bolt forwarder on a free local port; `drop()` closes every
    connection and refuses new ones until `resume()`."""

    def __init__(self) -> None:
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.dropped = False
        self.closed = False
        self.sockets: set[socket.socket] = set()
        self.lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while not self.closed:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            if self.dropped:
                client.close()
                continue
            try:
                upstream = socket.create_connection(NEO4J)
            except OSError:
                client.close()
                continue
            with self.lock:
                self.sockets |= {client, upstream}
            for a, b in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pump, args=(a, b), daemon=True).start()

    def _pump(self, source: socket.socket, sink: socket.socket) -> None:
        try:
            while data := source.recv(65536):
                sink.sendall(data)
        except OSError:
            pass
        finally:
            for each in (source, sink):
                try:
                    each.close()
                except OSError:
                    pass

    def drop(self) -> None:
        self.dropped = True
        with self.lock:
            for each in self.sockets:
                try:
                    each.shutdown(socket.SHUT_RDWR)
                    each.close()
                except OSError:
                    pass
            self.sockets.clear()

    def resume(self) -> None:
        self.dropped = False

    def close(self) -> None:
        self.closed = True
        self.drop()
        self.listener.close()


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
    sweep_stale_live_repos(engine, "zz-lostrelink")
    repo = live_repo_id("zz-lostrelink")
    yield repo
    engine.delete_repository(repo)


@pytest.mark.parametrize("restart", [False, True], ids=["retry", "after-a-restart"])
def test_a_batch_losing_neo4j_while_relinking_converges_on_its_retry(
    engine, repo_id, tmp_path, monkeypatch, restart
):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
    (root / "a.py").write_text("def other():\n    return 1\n")
    (root / "b.py").write_text("from a import helper\ndef user():\n    return helper()\n")
    forwarder = _Forwarder()
    proxied = GraphEngine(
        uri=f"bolt://127.0.0.1:{forwarder.port}", user=AUTH[0], password=AUTH[1],
        fail_fast=True, connection_timeout_s=2,
    )
    registry = RepoRegistry(tmp_path / "registry.sqlite3")
    try:
        registry.add_repo(root, repo_id=repo_id)
        provision_repository_schema(engine, root)
        engine.upsert_repository(repo_id, repo_id, str(root))
        full_scan(engine, repo_id, root)
        # Past the catch-up's margin, so the retry offers a.py alone, not b.py.
        time.sleep(dispatch.CATCH_UP_MARGIN_NS / 1e9 + 1)
        registry.mark_indexed(repo_id, at=datetime.now(timezone.utc))
        time.sleep(0.05)
        requested = []
        sync = RepoSync(proxied, registry, lambda event: None, lambda *args: requested.append(args))

        real = dispatch._relink_name_refs

        def lose_then_relink(*args, **kwargs):
            forwarder.drop()
            return real(*args, **kwargs)

        (root / "a.py").write_text("def other():\n    return 1\ndef helper():\n    return 2\n")
        with monkeypatch.context() as patched:
            stub(patched, dispatch, "_relink_name_refs", lose_then_relink)
            sync.on_changes(repo_id, {root / "a.py"}, set())
        assert forwarder.dropped and len(requested) == 1  # the batch failed and asked for its retry
        assert graph_snapshot(engine, repo_id) != fresh_snapshot(engine, repo_id, root)

        assert engine.open_batches(repo_id)  # the cut-short batch's marker stays

        forwarder.resume()
        if restart:
            # The agent killed before its retry: a new process (another
            # owner, the same pid as a restarted container's) with a new
            # RepoSync and no memory of the batch runs its start catch-up.
            monkeypatch.setattr(dispatch, "_SCAN_OWNER", f"{dispatch._SCAN_OWNER.rsplit('/', 1)[0]}/0badc0de")
            monkeypatch.setattr(dispatch, "_failed_batches", set())
            sync = RepoSync(engine, registry, lambda event: None, lambda *args: None)
            assert sync.on_catch_up(repo_id, datetime.fromisoformat(registry.get(repo_id).last_indexed), "start")
        else:
            assert sync.on_catch_up(repo_id, requested[0][1], "retry")
        expected = fresh_snapshot(engine, repo_id, root)
        actual = graph_snapshot(engine, repo_id)
        assert actual == expected, snapshot_diff(expected, actual)
        assert engine.open_batches(repo_id) == []
    finally:
        registry.close()
        proxied.close()
        forwarder.close()


def test_a_finished_batch_leaves_no_marker(engine, repo_id, tmp_path):
    """A finished batch's marker goes with the engine's next write, or as the
    engine closes, and it is never taken for unfinished meanwhile."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
    (root / "a.py").write_text("def other():\n    return 1\n")
    registry = RepoRegistry(tmp_path / "registry.sqlite3")
    agent = GraphEngine(uri=f"bolt://{NEO4J[0]}:{NEO4J[1]}", user=AUTH[0], password=AUTH[1])
    try:
        registry.add_repo(root, repo_id=repo_id)
        provision_repository_schema(engine, root)
        engine.upsert_repository(repo_id, repo_id, str(root))
        full_scan(agent, repo_id, root)
        sync = RepoSync(agent, registry, lambda event: None, lambda *args: None)
        (root / "a.py").write_text("def other():\n    return 2\n")
        (root / "b.py").write_text("def more():\n    return 3\n")
        sync.on_changes(repo_id, {root / "a.py", root / "b.py"}, set())
        (root / "b.py").unlink()
        sync.on_changes(repo_id, set(), {root / "b.py"})
        assert len(engine.open_batches(repo_id)) <= 1  # the last batch's, until the next write
        assert dispatch._unfinished_batches(agent, repo_id) == []
        agent.close()
        assert engine.open_batches(repo_id) == []
    finally:
        agent.close()
        registry.close()


class _Markers:
    def __init__(self, tokens):
        self.tokens = tokens

    def open_batches(self, repo_id):
        return list(self.tokens)


def test_which_markers_count_as_unfinished(monkeypatch):
    host, pid = socket.gethostname(), os.getpid()
    own = f"{pid}@{host}/0123abcd"
    monkeypatch.setattr(dispatch, "_SCAN_OWNER", own)
    monkeypatch.setattr(dispatch, "_failed_batches", {f"{own}#1"})
    monkeypatch.setattr(dispatch, "_holder_gone", lambda holder: holder.startswith("999999@"))
    tokens = [
        f"{own}#1",  # this process's, failed: unfinished
        f"{own}#2",  # this process's, running now or finished with its removal still to be written
        f"{pid}@{host}/deadbeef#1",  # an earlier process of this pid: unfinished
        f"999999@{host}/feedface#4",  # an ended process of this host: unfinished
        f"4242@{host}/cafecafe#1",  # a running process of this host
        f"{pid}@elsewhere.example/0badf00d#1",  # another host's: never taken for ended
    ]
    assert dispatch._unfinished_batches(_Markers(tokens), "r") == [tokens[0], tokens[2], tokens[3]]


def test_marking_a_batch_runs_no_query_of_its_own(tmp_path):
    """The markers ride on the batch's own write transactions (engine
    `_BatchMarkers`), so a save costs no extra round trip."""
    marking = GraphEngine(uri="bolt://127.0.0.1:1", user=AUTH[0], password=AUTH[1])
    sessions = []
    marking._driver.session = lambda **kwargs: sessions.append(kwargs)
    marking.open_batch("r", "t1")
    marking.finish_batch("r", "t1")
    marking.open_batch("r", "t2")
    marking.forget_batch("t2")
    assert sessions == []
    assert not marking._driver.markers.pending()  # t1 was never written, so nothing is left to remove
