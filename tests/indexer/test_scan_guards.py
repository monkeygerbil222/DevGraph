"""Files a scan skips: too large, binary, minified or generated, or ignored by a
.gitignore -- the same way for a full scan, a live batch and a catch-up.

The unit tests stub the graph; the live ones run against Neo4j.
"""

import uuid
from datetime import UTC, datetime

import pytest

from devgraph.graph.engine import GraphEngine
from devgraph.indexer import dispatch, walk
from devgraph.indexer.dispatch import catch_up, full_scan, index_paths, remove_paths
from devgraph.indexer.walk import BINARY, GENERATED, TOO_LARGE, content_skip_reason

LIMIT = 4096


# --- the content filters -----------------------------------------------------


def test_a_file_over_the_size_limit_is_too_large(tmp_path):
    path = tmp_path / "big.py"
    path.write_text("x = 1\n" * 1000)  # 6000 bytes
    assert content_skip_reason(path, LIMIT) == TOO_LARGE
    assert content_skip_reason(path, 10_000) is None


def test_a_nul_byte_near_the_start_is_binary(tmp_path):
    path = tmp_path / "blob.py"
    path.write_bytes(b"x = 1\n\x00\x01\x02")
    assert content_skip_reason(path, LIMIT) == BINARY


def test_a_nul_byte_past_the_first_8_kib_is_not_sniffed(tmp_path):
    path = tmp_path / "late.py"
    path.write_bytes(b"x = 1\n" * 2000 + b"\x00")
    assert content_skip_reason(path, 1_000_000) is None


def test_a_minified_file_with_one_huge_line_is_generated(tmp_path):
    path = tmp_path / "app.min.js"
    path.write_text("var a=1;" * 2000)  # one 16 KB line
    assert content_skip_reason(path, 1_000_000) == GENERATED


def test_a_file_of_long_lines_on_average_is_generated(tmp_path):
    path = tmp_path / "table.py"
    path.write_text(("x = '" + "y" * 400 + "'\n") * 40)
    assert content_skip_reason(path, 1_000_000) == GENERATED


def test_ordinary_source_and_a_short_one_liner_are_kept(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n" * 200)
    (tmp_path / "tiny.min.js").write_text("var a=1;" * 100)  # 800 bytes on one line
    assert content_skip_reason(tmp_path / "a.py", 1_000_000) is None
    assert content_skip_reason(tmp_path / "tiny.min.js", 1_000_000) is None


def test_prose_is_not_judged_by_its_line_length(tmp_path):
    path = tmp_path / "README.md"
    path.write_text(("word " * 100 + "\n\n") * 40)  # long paragraphs, one per line
    assert content_skip_reason(path, 1_000_000) is None


def test_the_size_limit_is_a_setting_with_a_one_mib_default(monkeypatch):
    from devgraph.config.settings import Settings

    assert Settings().max_file_bytes == 1024 * 1024
    monkeypatch.setenv("DEVGRAPH_MAX_FILE_BYTES", "2048")
    assert Settings().max_file_bytes == 2048


# --- index_paths, with the graph stubbed -------------------------------------


class _Engine:
    def __init__(self):
        self.deleted: list[str] = []

    def find_importing_modules(self, repo_id, module_name):
        return []

    def list_file_nodes(self, repo_id, files):
        return set()

    def read_applied_schema(self, repo_id):
        return None

    def delete_nodes_by_source_file(self, repo_id, rel):
        self.deleted.append(rel)


@pytest.fixture
def small_limit(monkeypatch):
    monkeypatch.setattr(dispatch, "_max_file_bytes", lambda: LIMIT)


def test_index_paths_skips_and_reports_filtered_files_and_clears_their_nodes(tmp_path, monkeypatch, small_limit):
    (tmp_path / ".gitignore").write_text("out/\n")
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "big.py").write_text("x = 1\n" * 1000)
    (tmp_path / "blob.js").write_bytes(b"\x00\x01")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "gen.py").write_text("x = 1\n")
    seen = []
    monkeypatch.setattr(dispatch, "_index_single_path", lambda e, r, root, resolved, rel, *a: seen.append(rel) or 1)
    engine = _Engine()
    skipped: dict[str, str] = {}

    count = index_paths(
        engine, "_unit_guards", tmp_path, {tmp_path / n for n in ("a.py", "big.py", "blob.js", "out/gen.py")},
        skipped=skipped,
    )

    assert seen == ["a.py"] and count == 1
    assert skipped == {"big.py": TOO_LARGE, "blob.js": BINARY, "out/gen.py": walk.GITIGNORED}
    assert sorted(engine.deleted) == ["big.py", "blob.js", "out/gen.py"]


def test_a_file_no_extractor_reads_is_not_judged_by_its_content(tmp_path, monkeypatch, small_limit):
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\x00" * 2000)
    monkeypatch.setattr(dispatch, "_index_single_path", lambda *a: 0)
    skipped: dict[str, str] = {}
    index_paths(_Engine(), "_unit_guards", tmp_path, {tmp_path / "logo.png"}, skipped=skipped)
    assert skipped == {}


def test_catch_up_does_not_offer_a_skipped_file_the_graph_does_not_have(tmp_path, monkeypatch, small_limit):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "big.py").write_text("x = 1\n" * 1000)
    offered = []
    monkeypatch.setattr(dispatch, "prune_stale_files", lambda *a, **k: 0)
    monkeypatch.setattr(dispatch, "schema_pending", lambda *a, **k: False)
    monkeypatch.setattr(dispatch, "index_outdated", lambda *a, **k: False)
    monkeypatch.setattr(dispatch, "_graph_files", lambda *a, **k: set())
    monkeypatch.setattr(dispatch, "_docs_note_files", lambda *a, **k: set())
    monkeypatch.setattr(dispatch, "index_paths", lambda e, r, root, paths, **k: offered.append(paths) or len(paths))

    result = catch_up(object(), "_unit_guards", tmp_path, datetime.now(UTC))

    assert offered == [{tmp_path / "a.py"}]
    assert result.unknown == 1


# --- live: incremental equals fresh ------------------------------------------


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    test_engine.init_schema()
    ids = [f"_smoketest_guards_{uuid.uuid4().hex[:8]}", f"_smoketest_guards_fresh_{uuid.uuid4().hex[:8]}"]
    yield test_engine, ids
    for repo_id in ids:
        test_engine.delete_repository(repo_id)
    test_engine.close()


def _graph(engine, repo_id):
    nodes = engine.run_cypher(
        "MATCH (n {repo_id: $r}) WHERE NOT n:Repository "
        "RETURN labels(n)[0] AS label, n.name AS name, coalesce(n.file, n.source_file, n.path, '') AS file",
        {"r": repo_id},
    )
    return sorted((n["label"], n["name"] or "", n["file"]) for n in nodes)


def _files(engine, repo_id):
    return {file for _label, _name, file in _graph(engine, repo_id) if file}


def _matches_fresh(engine, live, fresh, root):
    engine.delete_repository(fresh)
    engine.upsert_repository(fresh, fresh, str(root))
    full_scan(engine, fresh, root)
    assert _graph(engine, live) == _graph(engine, fresh)


def test_a_file_crossing_the_size_limit_leaves_and_rejoins_the_graph(engine, tmp_path, small_limit):
    engine, (live, fresh) = engine
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("def alpha():\n    return 1\n")
    (root / "b.py").write_text("def beta():\n    return 2\n")
    engine.upsert_repository(live, live, str(root))
    full_scan(engine, live, root)
    assert "b.py" in _files(engine, live)

    (root / "b.py").write_text("def beta():\n    return 2\n" + "# padding\n" * 600)  # over 4 KiB
    skipped: dict[str, str] = {}
    index_paths(engine, live, root, {root / "b.py"}, skipped=skipped)  # what the watcher sends
    assert skipped == {"b.py": TOO_LARGE}
    assert "b.py" not in _files(engine, live)
    _matches_fresh(engine, live, fresh, root)

    (root / "b.py").write_text("def beta():\n    return 3\n")
    index_paths(engine, live, root, {root / "b.py"})
    assert "b.py" in _files(engine, live)
    _matches_fresh(engine, live, fresh, root)


def test_a_gitignore_edit_is_caught_up_to_a_fresh_scan(engine, tmp_path):
    engine, (live, fresh) = engine
    root = tmp_path / "repo"
    (root / "out").mkdir(parents=True)
    (root / "a.py").write_text("def alpha():\n    return 1\n")
    (root / "out" / "gen.py").write_text("def generated():\n    return 2\n")
    (root / "keep.log.py").write_text("def kept():\n    return 3\n")
    engine.upsert_repository(live, live, str(root))
    full_scan(engine, live, root)
    assert {"a.py", "out/gen.py", "keep.log.py"} <= _files(engine, live)

    # Ignore out/ and *.log.py: the live batch carries the .gitignore, and a
    # catch-up from the batch's start follows it, as RepoSync does.
    started = datetime.now(UTC)
    (root / ".gitignore").write_text("out/\n*.log.py\n")
    index_paths(engine, live, root, {root / ".gitignore"})
    catch_up(engine, live, root, started)
    assert "out/gen.py" not in _files(engine, live) and "keep.log.py" not in _files(engine, live)
    _matches_fresh(engine, live, fresh, root)

    # Re-include keep.log.py with a negation, then drop the .gitignore altogether.
    started = datetime.now(UTC)
    (root / ".gitignore").write_text("out/\n*.log.py\n!keep.log.py\n")
    index_paths(engine, live, root, {root / ".gitignore"})
    catch_up(engine, live, root, started)
    assert "keep.log.py" in _files(engine, live) and "out/gen.py" not in _files(engine, live)
    _matches_fresh(engine, live, fresh, root)

    started = datetime.now(UTC)
    (root / ".gitignore").unlink()
    remove_paths(engine, live, root, {root / ".gitignore"})
    catch_up(engine, live, root, started)
    assert "out/gen.py" in _files(engine, live)
    _matches_fresh(engine, live, fresh, root)


def test_full_scan_reports_what_it_skipped(engine, tmp_path, small_limit):
    engine, (live, _fresh) = engine
    root = tmp_path / "repo"
    (root / "static").mkdir(parents=True)
    (root / "a.py").write_text("def alpha():\n    return 1\n")
    (root / "static" / "app.min.js").write_text("var a=1;" * 2000)
    (root / "data.py").write_bytes(b"\x00" * 10)
    engine.upsert_repository(live, live, str(root))
    skipped: dict[str, str] = {}
    full_scan(engine, live, root, skipped=skipped)
    assert skipped == {"static/app.min.js": TOO_LARGE, "data.py": BINARY}
    assert _files(engine, live) == {"a.py"}



@pytest.mark.parametrize(
    ("gitignore", "gone"),
    [("pkg/.gitignore", {"pkg/b.py", "pkg/.gitignore"}), (".gitignore", {".hidden.py", ".gitignore"})],
)
def test_a_gitignore_that_ignores_itself_reaches_the_catch_up(engine, tmp_path, gitignore, gone):
    """A nested `*` or a root `.*` ignores the .gitignore itself: the watcher
    still queues it, and RepoSync's catch-up leaves a fresh scan's graph."""
    import threading
    from unittest.mock import MagicMock

    from watchdog.events import FileCreatedEvent

    from devgraph.agent.sync import RepoSync
    from devgraph.registry.store import RepoRecord
    from devgraph.watcher.manager import _RepoEventHandler

    engine, (live, fresh) = engine
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "a.py").write_text("def alpha():\n    return 1\n")
    (root / "pkg" / "b.py").write_text("def beta():\n    return 2\n")
    (root / ".hidden.py").write_text("def hidden():\n    return 3\n")
    engine.upsert_repository(live, live, str(root))
    full_scan(engine, live, root)
    assert {"pkg/b.py", ".hidden.py"} <= _files(engine, live)

    registry = MagicMock()
    registry.get.return_value = RepoRecord(live, root, True, True, None, docs_path=None)
    repo_sync = RepoSync(engine, registry, lambda event: None, lambda *a: None)
    handler = _RepoEventHandler(live, root, 500, repo_sync.on_changes, timer_factory=lambda *a: MagicMock(),
                                batch_lock=threading.Lock())
    (root / gitignore).write_text("*\n" if gitignore.startswith("pkg/") else ".*\n")
    handler.dispatch(FileCreatedEvent(str(root / gitignore)))
    handler.flush()

    assert not (gone & _files(engine, live))
    _matches_fresh(engine, live, fresh, root)
