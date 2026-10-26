"""A live batch that loses Neo4j after writing its files' nodes, before it
relinked other files' edges to them, still converges (audit 11 review,
item 1): its retry could diff only against the nodes the lost batch had
already written, find nothing added, and leave `user()`'s call to the new
`helper()` out, so the retry catch-up rescans the repository in full.

The connection is lost for real: the agent's engine talks to Neo4j through a
forwarder this test runs on a port of its own, and the forwarder drops every
connection as the batch reaches `_relink_name_refs`."""

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


def test_a_batch_losing_neo4j_while_relinking_converges_on_its_retry(engine, repo_id, tmp_path, monkeypatch):
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

        forwarder.resume()
        assert sync.on_catch_up(repo_id, requested[0][1], "retry")
        expected = fresh_snapshot(engine, repo_id, root)
        actual = graph_snapshot(engine, repo_id)
        assert actual == expected, snapshot_diff(expected, actual)
    finally:
        registry.close()
        proxied.close()
        forwarder.close()
        dispatch._lost_batches.discard(repo_id)
