"""Graph accuracy against a live Neo4j: edge sources are pinned, and a file's re-index
retracts the edges it no longer writes and nothing else.

See docs/superpowers/specs/2026-10-08-graph-accuracy-design.md (G1, G2). Every
scenario ends in "incremental equals a fresh `full_scan`" or "`full_scan`
equals the expected graph".
"""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import _UNCLAIM_EDGE, GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan, index_paths
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff

_TOKEN = uuid.uuid4().hex[:8]
RUNBOOK = f"ZzAccRunbook{_TOKEN}"


@pytest.fixture(scope="module", autouse=True)
def _drop_generated_constraints():
    yield
    cleanup = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        cleanup.run_cypher(f"DROP CONSTRAINT {RUNBOOK.lower()}_repo_key IF EXISTS")
        cleanup.run_cypher(f"DROP INDEX {RUNBOOK.lower()}_repo_name IF EXISTS")
    except Exception:
        pass  # Neo4j unavailable: the tests were skipped
    finally:
        cleanup.close()


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
    repo = f"zz-accuracy-{uuid.uuid4().hex[:8]}"
    ids = [repo, f"{repo}_fresh", f"{repo}-other", f"{repo}-other_fresh"]
    for each in ids:
        engine.delete_repository(each)
    yield repo
    for each in ids:
        engine.delete_repository(each)


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def scan(engine, repo_id, root, **options):
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root, **options)


def incremental_equals_fresh(engine, repo_id, root, mentions_enabled=False, docs_path=None):
    expected = fresh_snapshot(engine, repo_id, root, mentions_enabled=mentions_enabled, docs_path=docs_path)
    actual = graph_snapshot(engine, repo_id)
    if actual != expected:
        pytest.fail("graph does not equal a fresh full_scan:\n" + snapshot_diff(expected, actual))


def edges(engine, repo_id, rel_type):
    """The snapshot's edges of one type: (label, name, file, type, label, name, file, origins)."""
    return [edge for edge in graph_snapshot(engine, repo_id)[1] if edge[3] == rel_type]


# --- origins -----------------------------------------------------------------


def _calls(engine, repo_id, origin):
    engine.upsert_relationships([{
        "from_label": "Function", "from_name": "f", "rel_type": "CALLS", "to_label": "Function",
        "to_name": "g", "repo_id": repo_id, "origin": origin,
    }])


def _origins(engine, repo_id):
    rows = engine.run_cypher(
        "MATCH (:Function {repo_id: $repo_id, name: 'f'})-[r:CALLS]->() RETURN r.origins AS o", {"repo_id": repo_id}
    )
    return [row["o"] for row in rows]


def _unclaim(engine, repo_id, file_name):
    engine.run_cypher(
        "MATCH (:Function {repo_id: $repo_id, name: 'f'})-[r:CALLS]->() " + _UNCLAIM_EDGE,
        {"repo_id": repo_id, "f": file_name},
    )


def test_origins_are_a_sorted_set(engine, repo_id):
    engine.upsert_nodes([
        {"label": "Function", "repo_id": repo_id, "name": name, "properties": {}} for name in ("f", "g")
    ])
    for origin in ("b.py", "a.py", "b.py", None):
        _calls(engine, repo_id, origin)
    assert _origins(engine, repo_id) == [["a.py", "b.py"]]

    _unclaim(engine, repo_id, "a.py")
    assert _origins(engine, repo_id) == [["b.py"]]
    _unclaim(engine, repo_id, "b.py")
    assert _origins(engine, repo_id) == []

    _calls(engine, repo_id, None)
    assert _origins(engine, repo_id) == [None]
    _unclaim(engine, repo_id, "any.py")
    assert _origins(engine, repo_id) == []


# --- code ----------------------------------------------------------------------

APP = """\
    def helper():
        return 1


    def main():
        return helper()
"""


def app_and_worker(root):
    write(root, "src/app.py", APP)
    write(root, "src/worker.py", "def main():\n    return 2\n")


def test_same_named_functions_keep_their_own_calls(engine, repo_id, tmp_path):
    app_and_worker(tmp_path)
    scan(engine, repo_id, tmp_path)
    assert edges(engine, repo_id, "CALLS") == [
        ("Function", "main", "src/app.py", "CALLS", "Function", "helper", "src/app.py", ("src/app.py",)),
    ]

    write(tmp_path, "src/worker.py", "def main():\n    return 3\n")
    index_paths(engine, repo_id, tmp_path, {tmp_path / "src/worker.py"})
    incremental_equals_fresh(engine, repo_id, tmp_path)


def test_same_named_classes_keep_their_own_bases(engine, repo_id, tmp_path):
    write(tmp_path, "a.py", "class K(Base):\n    pass\n")
    write(tmp_path, "b.py", "class K:\n    pass\n")
    write(tmp_path, "base.py", "class Base:\n    pass\n")
    scan(engine, repo_id, tmp_path)
    assert edges(engine, repo_id, "EXTENDS") == [
        ("Class", "K", "a.py", "EXTENDS", "Class", "Base", "base.py", ("a.py",)),
    ]
    incremental_equals_fresh(engine, repo_id, tmp_path)


M_BEFORE = """\
    import pkg.util


    class Base:
        pass


    class K(Base):
        pass


    def helper():
        pass


    def main():
        helper()
"""
M_AFTER = """\
    class Base:
        pass


    class K:
        pass


    def helper():
        pass


    def main():
        pass
"""


def _removed_edges(engine, repo_id):
    return [
        edge for rel_type in ("IMPORTS", "EXTENDS", "CALLS") for edge in edges(engine, repo_id, rel_type)
        if edge[1] in ("m.py", "K", "main")
    ]


def test_removed_call_base_and_import_are_retracted(engine, repo_id, tmp_path):
    write(tmp_path, "pkg/util.py", "X = 1\n")
    write(tmp_path, "m.py", M_BEFORE)
    scan(engine, repo_id, tmp_path)
    assert len(_removed_edges(engine, repo_id)) == 3

    write(tmp_path, "m.py", M_AFTER)
    index_paths(engine, repo_id, tmp_path, {tmp_path / "m.py"})
    assert _removed_edges(engine, repo_id) == []
    incremental_equals_fresh(engine, repo_id, tmp_path)

    # The same edit, healed by a full_scan alone.
    write(tmp_path, "m.py", M_BEFORE)
    scan(engine, repo_id, tmp_path)
    assert len(_removed_edges(engine, repo_id)) == 3
    write(tmp_path, "m.py", M_AFTER)
    scan(engine, repo_id, tmp_path)
    assert _removed_edges(engine, repo_id) == []
    incremental_equals_fresh(engine, repo_id, tmp_path)


def test_full_scan_heals_a_polluted_graph(engine, repo_id, tmp_path):
    app_and_worker(tmp_path)
    scan(engine, repo_id, tmp_path)
    # The wrong edge bug 1 wrote, and a stale one out of a surviving node,
    # both without origins as an older index left them.
    engine.run_cypher(
        "MATCH (a:Function {repo_id: $r, name: 'main', file: 'src/worker.py'}) "
        "MATCH (b:Function {repo_id: $r, name: 'helper', file: 'src/app.py'}) "
        "MATCH (c:Function {repo_id: $r, name: 'main', file: 'src/app.py'}) "
        "MERGE (a)-[:CALLS]->(b) MERGE (b)-[:CALLS]->(c)",
        {"r": repo_id},
    )
    assert len(edges(engine, repo_id, "CALLS")) == 3

    scan(engine, repo_id, tmp_path)
    incremental_equals_fresh(engine, repo_id, tmp_path)


def test_foreign_impl_edge_survives_owner_reindex(engine, repo_id, tmp_path):
    write(tmp_path, "foo.rs", "pub struct Foo;\n")
    write(tmp_path, "display.rs", "pub trait Display {}\n")
    write(tmp_path, "conv.rs", "impl Display for Foo {}\n")
    scan(engine, repo_id, tmp_path)
    impl_edge = ("Class", "Foo", "foo.rs", "EXTENDS", "Class", "Display", "display.rs", ("conv.rs",))
    assert edges(engine, repo_id, "EXTENDS") == [impl_edge]

    write(tmp_path, "foo.rs", "// Foo.\npub struct Foo;\n")
    index_paths(engine, repo_id, tmp_path, {tmp_path / "foo.rs"})
    assert edges(engine, repo_id, "EXTENDS") == [impl_edge]
    incremental_equals_fresh(engine, repo_id, tmp_path)


# --- docs notes ----------------------------------------------------------------


def note(root, rel, front):
    return write(root, rel, f"---\n{textwrap.dedent(front).strip()}\n---\n# Note\n")


ADR_LINKED = """
    type: design_decision
    id: ADR-1
    links: [src/app.py]
"""
ADR_UNLINKED = """
    type: design_decision
    id: ADR-1
"""
DOCUMENTED = ("Module", "src/app.py", "src/app.py", "DOCUMENTED_BY", "DesignDecision", "ADR-1", "docs/adr-1.md",
              ("docs/adr-1.md",))


def test_docs_note_edge_survives_code_reindex(engine, repo_id, tmp_path):
    write(tmp_path, "src/app.py", APP)
    note(tmp_path, "docs/adr-1.md", ADR_LINKED)
    scan(engine, repo_id, tmp_path, docs_path="docs")
    assert edges(engine, repo_id, "DOCUMENTED_BY") == [DOCUMENTED]

    write(tmp_path, "src/app.py", APP + "\n\n# Touched.\n")
    index_paths(engine, repo_id, tmp_path, {tmp_path / "src/app.py"}, docs_path="docs")
    assert edges(engine, repo_id, "DOCUMENTED_BY") == [DOCUMENTED]
    incremental_equals_fresh(engine, repo_id, tmp_path, docs_path="docs")


def test_removed_note_links_are_retracted(engine, repo_id, tmp_path):
    write(tmp_path, "src/app.py", APP)
    adr = note(tmp_path, "docs/adr-1.md", ADR_LINKED)
    later = note(tmp_path, "docs/adr-2.md", "type: design_decision\nid: ADR-2\nsupersedes: ADR-1")
    scan(engine, repo_id, tmp_path, docs_path="docs")
    assert edges(engine, repo_id, "DOCUMENTED_BY") == [DOCUMENTED]
    assert len(edges(engine, repo_id, "SUPERSEDES")) == 1

    note(tmp_path, "docs/adr-1.md", ADR_UNLINKED)
    index_paths(engine, repo_id, tmp_path, {adr}, docs_path="docs")
    assert edges(engine, repo_id, "DOCUMENTED_BY") == []
    incremental_equals_fresh(engine, repo_id, tmp_path, docs_path="docs")

    note(tmp_path, "docs/adr-2.md", "type: design_decision\nid: ADR-2")
    index_paths(engine, repo_id, tmp_path, {later}, docs_path="docs")
    assert edges(engine, repo_id, "SUPERSEDES") == []
    incremental_equals_fresh(engine, repo_id, tmp_path, docs_path="docs")

    # A legacy edge (no origins) the note no longer writes goes on a full_scan.
    engine.run_cypher(
        "MATCH (m:Module {repo_id: $r, name: 'src/app.py'}) MATCH (d:DesignDecision {repo_id: $r, name: 'ADR-1'}) "
        "MERGE (m)-[:DOCUMENTED_BY]->(d)",
        {"r": repo_id},
    )
    scan(engine, repo_id, tmp_path, docs_path="docs")
    assert edges(engine, repo_id, "DOCUMENTED_BY") == []
    incremental_equals_fresh(engine, repo_id, tmp_path, docs_path="docs")


def test_doc_note_replace_is_repo_scoped(engine, repo_id, tmp_path):
    other = f"{repo_id}-other"
    roots = {repo_id: tmp_path / "one", other: tmp_path / "two"}
    for repo, root in roots.items():
        write(root, "src/app.py", APP)
        note(root, "docs/adr-1.md", ADR_LINKED)
        scan(engine, repo, root, docs_path="docs")
    others_before = edges(engine, other, "DOCUMENTED_BY")
    assert others_before == [DOCUMENTED]

    adr = note(roots[repo_id], "docs/adr-1.md", ADR_UNLINKED)
    index_paths(engine, repo_id, roots[repo_id], {adr}, docs_path="docs")
    assert edges(engine, repo_id, "DOCUMENTED_BY") == []
    assert edges(engine, other, "DOCUMENTED_BY") == others_before
    for repo, root in roots.items():
        incremental_equals_fresh(engine, repo, root, docs_path="docs")


# --- edges a re-index must keep --------------------------------------------------

ROUTED_APP = """\
    from fastapi import FastAPI

    app = FastAPI()


    def helper():
        return 1


    @app.get("/items")
    def list_items():
        return helper()
"""


def test_reindex_keeps_incoming_and_foreign_edges(engine, repo_id, tmp_path):
    write(tmp_path, "devgraph.schema.yaml", f"""\
        version: 1
        node_types:
          - label: {RUNBOOK}
            key: [path]
            metadata: [{{name: path}}]
            source: {{provider: docs, paths: ["runbooks/*.md"]}}
        relationships:
          - type: ZZ_RUNS
            provider: docs
            from: {RUNBOOK}
            to: Module
            field: module
    """)
    write(tmp_path, "runbooks/app.md", "---\nmodule: src/app.py\n---\n# Runbook\n")
    write(tmp_path, "notes.md", "Call `helper` first.\n")
    app = write(tmp_path, "src/app.py", ROUTED_APP)
    scan(engine, repo_id, tmp_path, mentions_enabled=True)
    engine.run_cypher(
        "MATCH (m:Module {repo_id: $r, name: 'src/app.py'}) "
        "MERGE (c:Commit {repo_id: $r, name: 'c0ffee'}) MERGE (c)-[:MODIFIES]->(m)",
        {"r": repo_id},
    )

    def kept():
        return {
            "mentions": [e for e in edges(engine, repo_id, "MENTIONS") if e[5] == "helper"],
            "provider": edges(engine, repo_id, "ZZ_RUNS"),
            "implements": edges(engine, repo_id, "IMPLEMENTS"),
            "modifies": engine.run_cypher(
                "MATCH (:Commit {repo_id: $r})-[x:MODIFIES]->(m:Module {repo_id: $r}) RETURN m.name AS m",
                {"r": repo_id},
            ),
        }

    before = kept()
    assert all(before.values()), before
    write(tmp_path, "src/app.py", ROUTED_APP + "\n\n# Touched.\n")
    index_paths(engine, repo_id, tmp_path, {app}, mentions_enabled=True)
    assert kept() == before
    incremental_equals_fresh(engine, repo_id, tmp_path, mentions_enabled=True)
