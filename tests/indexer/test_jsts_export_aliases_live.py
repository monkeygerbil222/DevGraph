"""JS/TS calls through export aliases against a live Neo4j (audit 12 item
4): a renamed or default re-export, and a default-exported function imported
under another name, link the function, and editing, creating or deleting the
barrel alone, or the target alone, equals a fresh `full_scan`. Fixture code
is fictional."""

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
    repo = live_repo_id("zz-jsalias")
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


MATH = "export function add(a, b) { return a + b; }\nexport default function sub(a, b) { return a - b; }\n"
STRINGS = "export function upper(s) { return s; }\nexport function lower(s) { return s; }\n"
BARREL = "export { upper as shout } from './strings';\nexport { default as subtract } from './math';\n"
CALLER = """
    import { shout, subtract } from './lib';
    import minus from './lib/math';
    export function loud(s) { return shout(s); }
    export function diff(a, b) { return subtract(a, b) + minus(a, b); }
"""


def project(root):
    write(root, "src/lib/math.ts", MATH)
    write(root, "src/lib/strings.ts", STRINGS)
    write(root, "src/lib/index.ts", BARREL)
    write(root, "src/app.ts", CALLER)


def test_aliases_link_the_function_and_a_barrel_edit_alone_relinks(engine, repo_id, tmp_path):
    project(tmp_path)
    scan(engine, repo_id, tmp_path)
    assert callers(engine, repo_id, "upper") == ["loud"]
    assert callers(engine, repo_id, "sub") == ["diff"]

    # The barrel renames another function; the caller is not touched.
    barrel = write(tmp_path, "src/lib/index.ts", BARREL.replace("upper as shout", "lower as shout"))
    index_paths(engine, repo_id, tmp_path, {barrel})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert callers(engine, repo_id, "upper") == []
    assert callers(engine, repo_id, "lower") == ["loud"]

    # The rename goes: `shout` names nothing.
    write(tmp_path, "src/lib/index.ts", "export { default as subtract } from './math';\n")
    index_paths(engine, repo_id, tmp_path, {barrel})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert callers(engine, repo_id, "lower") == []


def test_a_target_edit_alone_relinks_through_the_barrel(engine, repo_id, tmp_path):
    project(tmp_path)
    scan(engine, repo_id, tmp_path)

    # The default export becomes another function of the file: no node is
    # added or removed, and the barrel's own text is the same.
    math = write(tmp_path, "src/lib/math.ts", MATH.replace("export default function sub", "export function sub")
                 + "export default add;\n")
    index_paths(engine, repo_id, tmp_path, {math})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert callers(engine, repo_id, "sub") == []
    assert callers(engine, repo_id, "add") == ["diff"]

    # Renamed in place: a new node, the old one gone.
    write(tmp_path, "src/lib/math.ts", MATH.replace("function sub", "function difference"))
    index_paths(engine, repo_id, tmp_path, {math})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert callers(engine, repo_id, "difference") == ["diff"]


def test_creating_or_deleting_the_barrel_alone_relinks(engine, repo_id, tmp_path):
    project(tmp_path)
    barrel = tmp_path / "src/lib/index.ts"
    barrel.unlink()
    scan(engine, repo_id, tmp_path)
    assert callers(engine, repo_id, "upper") == []

    write(tmp_path, "src/lib/index.ts", BARREL)
    index_paths(engine, repo_id, tmp_path, {barrel})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert callers(engine, repo_id, "upper") == ["loud"]

    barrel.unlink()
    remove_paths(engine, repo_id, tmp_path, {barrel})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert callers(engine, repo_id, "upper") == []
