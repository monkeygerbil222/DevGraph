"""A JS/TS Module's `js_exports` against a live Neo4j (audit 12 item 4
review): an edit that removes every alias of a file, and deleting the only
target of a file's alias, each equal a fresh `full_scan` (the property is
written empty, as a Module write merges properties). Fixture code is
fictional."""

import textwrap

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan, index_paths, remove_paths
from devgraph.mcp.tools import find_callers
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, live_repo_id, snapshot_diff


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
    repo = live_repo_id("zz-jsalias-stale")
    yield repo
    for each in (repo, f"{repo}_fresh"):
        engine.delete_repository(each)


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def scan(engine, repo_id, root):
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root)


def incremental_equals_fresh(engine, repo_id, root):
    expected = fresh_snapshot(engine, repo_id, root)
    actual = graph_snapshot(engine, repo_id)
    if actual != expected:
        pytest.fail("graph does not equal a fresh full_scan:\n" + snapshot_diff(expected, actual))


def callers(engine, repo_id, name):
    return sorted(row["name"] for row in find_callers(engine, repo_id, name)["results"])


BARREL = "export { default as subtract } from './math';\n"
CALLER = "import { subtract } from './lib';\nexport function diff(a, b) { return subtract(a, b); }\n"


def test_an_edit_removing_every_alias_equals_fresh(engine, repo_id, tmp_path):
    write(tmp_path, "src/lib/math.ts", "export default function sub(a, b) { return a - b; }\n")
    barrel = write(tmp_path, "src/lib/index.ts", BARREL)
    write(tmp_path, "src/app.ts", CALLER)
    scan(engine, repo_id, tmp_path)
    assert callers(engine, repo_id, "sub") == ["diff"]

    write(tmp_path, "src/lib/index.ts", "export const version = 1;\n")
    index_paths(engine, repo_id, tmp_path, {barrel})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    # Its aliases changed again: the importer follows.
    write(tmp_path, "src/lib/index.ts", BARREL)
    index_paths(engine, repo_id, tmp_path, {barrel})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert callers(engine, repo_id, "sub") == ["diff"]


def test_deleting_the_only_alias_target_equals_fresh(engine, repo_id, tmp_path):
    math = write(tmp_path, "src/lib/math.ts", "export default function sub(a, b) { return a - b; }\n")
    write(tmp_path, "src/lib/index.ts", BARREL)
    write(tmp_path, "src/app.ts", CALLER)
    scan(engine, repo_id, tmp_path)

    math.unlink()
    remove_paths(engine, repo_id, tmp_path, {math})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    write(tmp_path, "src/lib/math.ts", "export default function sub(a, b) { return a - b; }\n")
    index_paths(engine, repo_id, tmp_path, {math})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert callers(engine, repo_id, "sub") == ["diff"]
