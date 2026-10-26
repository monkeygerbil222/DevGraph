"""A file whose name isn't valid UTF-8 is left out and reported by its path,
instead of failing the scan, catch-up or live batch of its whole repository
(audit 11 item 2): Python keeps such a name with lone surrogates, which no
Neo4j query can carry."""

import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from rich.console import Console

from devgraph.agent.sync import RepoSync
from devgraph.cli import main as cli_main
from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import catch_up, full_scan, index_paths
from devgraph.indexer.walk import UNENCODABLE_NAME, indexable_paths, printable_path
from devgraph.registry.store import RepoRegistry
from tests.watcher.live_helpers import (
    fresh_snapshot,
    graph_snapshot,
    live_repo_id,
    snapshot_diff,
    sweep_stale_live_repos,
)

BAD = b"caf\xe9.py"
SHOWN = "caf\\xe9.py"


def _bad_path(folder: Path, name: bytes = BAD) -> Path:
    """Create `name` (raw bytes) in `folder`, or skip where the OS refuses
    such a name (macOS and Windows file systems)."""
    raw = os.path.join(os.fsencode(folder), name)
    try:
        with open(raw, "wb") as f:
            f.write(b"def latin():\n    return 2\n")
    except (OSError, ValueError) as exc:
        pytest.skip(f"this file system refuses a non-UTF-8 name: {exc}")
    return Path(os.fsdecode(raw))


@pytest.mark.skipif(os.name == "nt", reason="Windows names are UTF-16, so no name holds an undecodable byte")
def test_printable_path_shows_each_undecodable_byte():
    assert printable_path(os.fsdecode(b"pkg/caf\xe9.py")) == "pkg/caf\\xe9.py"
    assert printable_path("pkg/plain.py") == "pkg/plain.py"


@pytest.mark.skipif(os.name == "nt", reason="Windows names are UTF-16, so no name holds an undecodable byte")
def test_the_walk_leaves_an_unencodable_file_and_folder_out_and_reports_them(tmp_path):
    (tmp_path / "good.py").write_text("x = 1\n")
    _bad_path(tmp_path)
    folder = Path(os.fsdecode(os.path.join(os.fsencode(tmp_path), b"d\xff")))
    folder.mkdir()
    (folder / "inner.py").write_text("y = 2\n")
    reported: list[str] = []
    found = indexable_paths(tmp_path, unencodable=reported)
    assert {p.relative_to(tmp_path).as_posix() for p in found} == {"good.py"}
    assert sorted(reported) == [SHOWN, "d\\xff/"]


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
    sweep_stale_live_repos(engine, "zz-badname")
    repo = live_repo_id("zz-badname")
    yield repo
    engine.delete_repository(repo)


def _functions(engine, repo_id):
    rows = engine.run_cypher("MATCH (f:Function {repo_id: $r}) RETURN f.name AS n", {"r": repo_id})
    return sorted(row["n"] for row in rows)


def _assert_fresh(engine, repo_id, root):
    expected = fresh_snapshot(engine, repo_id, root)
    actual = graph_snapshot(engine, repo_id)
    assert actual == expected, snapshot_diff(expected, actual)


def _scan(engine, repo_id, root, **kwargs):
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    return full_scan(engine, repo_id, root, **kwargs)


def test_a_full_scan_indexes_the_rest_and_the_cli_names_the_file(engine, repo_id, tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "good.py").write_text("def good():\n    return 1\n")
    (root / "pkg" / "other.py").write_text("from good import good\ndef other():\n    return good()\n")
    _bad_path(root)
    skipped: dict[str, str] = {}
    _scan(engine, repo_id, root, skipped=skipped)
    assert _functions(engine, repo_id) == ["good", "other"]
    assert skipped == {SHOWN: UNENCODABLE_NAME}

    console = Console(record=True, width=200)
    monkeypatch.setattr(cli_main, "console", console)
    cli_main._print_skipped(skipped)
    assert f"{SHOWN} ({UNENCODABLE_NAME})" in console.export_text()


def test_a_batch_and_a_catch_up_index_the_good_files_beside_one(engine, repo_id, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "good.py").write_text("def good():\n    return 1\n")
    _scan(engine, repo_id, root)
    since = datetime.now(timezone.utc) - timedelta(seconds=1)
    time.sleep(0.05)

    (root / "good.py").write_text("def good():\n    return 1\ndef added():\n    return 2\n")
    bad = _bad_path(root)
    catch_up(engine, repo_id, root, since)
    assert _functions(engine, repo_id) == ["added", "good"]

    (root / "good.py").write_text("def good():\n    return 1\ndef again():\n    return 3\n")
    skipped: dict[str, str] = {}
    index_paths(engine, repo_id, root, {root / "good.py", bad}, skipped=skipped)
    assert _functions(engine, repo_id) == ["again", "good"]
    assert skipped == {SHOWN: UNENCODABLE_NAME}
    _assert_fresh(engine, repo_id, root)


def test_live_batches_creating_and_deleting_one_stay_equal_to_fresh(engine, repo_id, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
    (root / "good.py").write_text("def good():\n    return 1\n")
    registry = RepoRegistry(tmp_path / "registry.sqlite3")
    try:
        registry.add_repo(root, repo_id=repo_id)
        _scan(engine, repo_id, root)
        sync = RepoSync(engine, registry, lambda event: None, lambda *args: None)

        bad = _bad_path(root)
        (root / "good.py").write_text("def good():\n    return 1\ndef added():\n    return 2\n")
        sync.on_changes(repo_id, {bad, root / "good.py"}, set())
        assert _functions(engine, repo_id) == ["added", "good"]
        _assert_fresh(engine, repo_id, root)

        bad.unlink()
        sync.on_changes(repo_id, set(), {bad})
        _assert_fresh(engine, repo_id, root)
        assert registry.get(repo_id).last_indexed is not None
    finally:
        registry.close()
