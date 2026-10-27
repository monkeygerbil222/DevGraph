"""JS/TS export aliases against a live Neo4j (audit 12 item 4 review): an
edit at the end of a re-export chain deeper than any limit equals a fresh
`full_scan`. Fixture code is fictional."""

import textwrap

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan, index_paths
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
    repo = live_repo_id("zz-jsalias-deep")
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


DEPTH = 12


def test_editing_the_end_of_a_deep_chain_equals_fresh(engine, repo_id, tmp_path):
    for i in range(DEPTH):
        write(tmp_path, f"src/chain/c{i}.ts", f"export {{ x{i + 1} as x{i} }} from './c{i + 1}';\n")
    last = write(tmp_path, f"src/chain/c{DEPTH}.ts",
                 f"export function base() {{}}\nexport function other() {{}}\nexport {{ base as x{DEPTH} }};\n")
    write(tmp_path, "src/use.ts", "import { x0 } from './chain/c0';\nexport function use() { x0(); }\n")
    scan(engine, repo_id, tmp_path)
    assert callers(engine, repo_id, "base") == ["use"]

    write(tmp_path, f"src/chain/c{DEPTH}.ts",
          f"export function base() {{}}\nexport function other() {{}}\nexport {{ other as x{DEPTH} }};\n")
    index_paths(engine, repo_id, tmp_path, {last})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert callers(engine, repo_id, "other") == ["use"]
