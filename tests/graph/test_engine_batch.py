"""Tests for GraphEngine's batched write methods (upsert_nodes,
upsert_relationships, replace_file_nodes) — the UNWIND-grouped, single-
transaction alternative to upserting one node/edge per round-trip."""

import neo4j
import pytest

from devgraph.graph.engine import GraphEngine


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    test_engine.init_schema()
    yield test_engine
    test_engine.close()


class TestUpsertNodes:
    def test_creates_nodes_with_mixed_labels(self, engine):
        repo_id = "_smoketest_batch_nodes_mixed"
        nodes = [
            {"label": "Module", "repo_id": repo_id, "name": "a.py", "properties": {"type": "module"}},
            {"label": "Class", "repo_id": repo_id, "name": "A", "properties": {"file": "a.py"}},
            {"label": "Function", "repo_id": repo_id, "name": "f", "properties": {"file": "a.py"}},
        ]
        try:
            engine.upsert_nodes(nodes)
            rows = engine.run_cypher(
                "MATCH (n {repo_id: $repo_id}) RETURN labels(n)[0] as label, n.name as name",
                {"repo_id": repo_id},
            )
            assert {(r["label"], r["name"]) for r in rows} == {
                ("Module", "a.py"),
                ("Class", "A"),
                ("Function", "f"),
            }
        finally:
            engine.delete_repository(repo_id)

    def test_rerunning_the_same_batch_does_not_duplicate(self, engine):
        repo_id = "_smoketest_batch_nodes_idempotent"
        nodes = [{"label": "Module", "repo_id": repo_id, "name": "a.py", "properties": {}}]
        try:
            engine.upsert_nodes(nodes)
            engine.upsert_nodes(nodes)
            rows = engine.run_cypher(
                "MATCH (n {repo_id: $repo_id}) RETURN COUNT(*) as c", {"repo_id": repo_id}
            )
            assert rows[0]["c"] == 1
        finally:
            engine.delete_repository(repo_id)

    def test_empty_batch_is_a_no_op(self, engine):
        engine.upsert_nodes([])  # must not raise


class TestUpsertRelationships:
    def test_merges_edges_with_mixed_triples(self, engine):
        repo_id = "_smoketest_batch_rels_mixed"
        try:
            engine.upsert_nodes(
                [
                    {"label": "Module", "repo_id": repo_id, "name": "a.py", "properties": {}},
                    {"label": "Module", "repo_id": repo_id, "name": "b.py", "properties": {}},
                    {"label": "Function", "repo_id": repo_id, "name": "f", "properties": {}},
                    {"label": "Function", "repo_id": repo_id, "name": "g", "properties": {}},
                ]
            )
            engine.upsert_relationships(
                [
                    {
                        "from_label": "Module",
                        "from_name": "a.py",
                        "rel_type": "IMPORTS",
                        "to_label": "Module",
                        "to_name": "b.py",
                        "repo_id": repo_id,
                        "properties": {},
                    },
                    {
                        "from_label": "Function",
                        "from_name": "f",
                        "rel_type": "CALLS",
                        "to_label": "Function",
                        "to_name": "g",
                        "repo_id": repo_id,
                        "properties": {"caller_class": "C"},
                    },
                ]
            )
            rows = engine.run_cypher(
                "MATCH (a {repo_id: $repo_id})-[r]->(b {repo_id: $repo_id}) "
                "RETURN type(r) as rel_type, a.name as from_name, b.name as to_name, r.caller_class as caller_class",
                {"repo_id": repo_id},
            )
            by_type = {r["rel_type"]: r for r in rows}
            assert by_type["IMPORTS"]["from_name"] == "a.py" and by_type["IMPORTS"]["to_name"] == "b.py"
            assert by_type["CALLS"]["from_name"] == "f" and by_type["CALLS"]["caller_class"] == "C"
        finally:
            engine.delete_repository(repo_id)

    def test_edge_to_nonexistent_node_is_silently_absent(self, engine):
        repo_id = "_smoketest_batch_rels_dangling"
        try:
            engine.upsert_nodes([{"label": "Module", "repo_id": repo_id, "name": "a.py", "properties": {}}])
            engine.upsert_relationships(
                [
                    {
                        "from_label": "Module",
                        "from_name": "a.py",
                        "rel_type": "IMPORTS",
                        "to_label": "Module",
                        "to_name": "never_existed.py",
                        "repo_id": repo_id,
                        "properties": {},
                    }
                ]
            )
            rows = engine.run_cypher(
                "MATCH (a:Module {repo_id: $repo_id})-[r]->() RETURN COUNT(*) as c", {"repo_id": repo_id}
            )
            assert rows[0]["c"] == 0
        finally:
            engine.delete_repository(repo_id)

    def test_empty_batch_is_a_no_op(self, engine):
        engine.upsert_relationships([])  # must not raise


class TestReplaceFileNodes:
    def test_replaces_old_nodes_with_new_ones(self, engine):
        repo_id = "_smoketest_replace_file_nodes"
        try:
            engine.upsert_node("Function", repo_id, "old_func", {"source_file": "f.py"})
            engine.upsert_node("Module", repo_id, "unrelated.py", {"source_file": "unrelated.py"})

            engine.replace_file_nodes(
                repo_id,
                "f.py",
                nodes=[
                    {"label": "Module", "repo_id": repo_id, "name": "f.py", "properties": {"source_file": "f.py"}},
                    {"label": "Function", "repo_id": repo_id, "name": "new_func", "properties": {"source_file": "f.py"}},
                ],
                rels=[],
            )

            rows = engine.run_cypher(
                "MATCH (n {repo_id: $repo_id}) RETURN n.name as name", {"repo_id": repo_id}
            )
            names = {r["name"] for r in rows}
            assert names == {"f.py", "new_func", "unrelated.py"}
        finally:
            engine.delete_repository(repo_id)

    def test_calling_twice_with_different_content_leaves_no_stale_nodes(self, engine):
        repo_id = "_smoketest_replace_file_nodes_twice"
        try:
            engine.replace_file_nodes(
                repo_id,
                "f.py",
                nodes=[
                    {"label": "Module", "repo_id": repo_id, "name": "f.py", "properties": {"source_file": "f.py"}},
                    {"label": "Function", "repo_id": repo_id, "name": "func_v1", "properties": {"source_file": "f.py"}},
                ],
                rels=[],
            )
            engine.replace_file_nodes(
                repo_id,
                "f.py",
                nodes=[
                    {"label": "Module", "repo_id": repo_id, "name": "f.py", "properties": {"source_file": "f.py"}},
                    {"label": "Function", "repo_id": repo_id, "name": "func_v2", "properties": {"source_file": "f.py"}},
                ],
                rels=[],
            )

            rows = engine.run_cypher(
                "MATCH (n {repo_id: $repo_id}) RETURN n.name as name", {"repo_id": repo_id}
            )
            names = {r["name"] for r in rows}
            assert names == {"f.py", "func_v2"}
        finally:
            engine.delete_repository(repo_id)


class TestBatchingReducesRoundTrips:
    def test_replace_file_nodes_issues_a_small_constant_number_of_tx_run_calls(self, engine, monkeypatch):
        repo_id = "_smoketest_batch_round_trips"
        call_count = {"n": 0}
        original_run = neo4j.ManagedTransaction.run

        def counting_run(self, *args, **kwargs):
            call_count["n"] += 1
            return original_run(self, *args, **kwargs)

        monkeypatch.setattr(neo4j.ManagedTransaction, "run", counting_run)

        nodes = [
            {"label": "Function", "repo_id": repo_id, "name": f"func_{i}", "properties": {"source_file": "big.py"}}
            for i in range(20)
        ]
        rels = [
            {
                "from_label": "Function",
                "from_name": f"func_{i}",
                "rel_type": "CALLS",
                "to_label": "Function",
                "to_name": f"func_{i + 1}",
                "repo_id": repo_id,
                "properties": {},
            }
            for i in range(19)
        ]

        try:
            engine.replace_file_nodes(repo_id, "big.py", nodes, rels)
            # 1 read of the Module's old name_ref_sources (none here, so no
            # foreign-source unclaim) + 1 delete + 1 unclaim (source-tracked
            # nodes) + 1 UNWIND per node label (just "Function") + 1 UNWIND
            # per relationship (from_label, rel_type, to_label) triple (just
            # one triple here) = 5 tx.run calls total, independent of the 20
            # nodes / 19 edges — not the 39+ calls the old per-item loop cost.
            assert call_count["n"] == 5
        finally:
            engine.delete_repository(repo_id)


class TestNameLookupIndexes:
    """Bare-name edge ends and describe_node match Class/Function/Service on
    (repo_id, name); their uniqueness constraint indexes (repo_id, name, file)."""

    def test_init_schema_provisions_one_per_file_scoped_label(self, engine):
        from devgraph.graph.schema import FILE_SCOPED_LABELS

        engine.init_schema()  # a second run is a no-op
        rows = engine.run_cypher(
            "SHOW INDEXES YIELD name, type, labelsOrTypes, properties "
            "WHERE name ENDS WITH '_repo_name_lookup' RETURN name, type, labelsOrTypes, properties"
        )
        assert sorted((r["labelsOrTypes"][0], r["type"], tuple(r["properties"])) for r in rows) == sorted(
            (label, "RANGE", ("repo_id", "name")) for label in FILE_SCOPED_LABELS
        )

    def test_a_bare_name_match_is_an_index_seek(self, engine):
        with engine._driver.session() as session:
            summary = session.run(
                "EXPLAIN MATCH (b:Function {repo_id: $r, name: $n}) RETURN b", r="r", n="g"
            ).consume()

        def operators(node):
            yield node["operatorType"]
            for child in node.get("children", []):
                yield from operators(child)

        ops = list(operators(summary.plan))
        assert any(op.startswith("NodeIndexSeek") for op in ops), ops
        assert not any(op.startswith("NodeByLabelScan") for op in ops), ops

    def test_a_pinned_end_is_hinted_onto_its_unique_index(self):
        """With a second (repo_id, name) index the planner can pick a full
        scan of it for a pinned end on a near-empty database (seen in CI: a
        5,000-module relink at 8 s instead of 0.5 s), so the pinned end of a
        file-scoped label names the (repo_id, name, file) index."""
        from devgraph.graph.engine import _upsert_relationships_tx

        class Tx:
            queries = []

            def run(self, query, **params):
                self.queries.append(query)

        rel = {"repo_id": "r", "rel_type": "CALLS", "properties": {}, "origin": "a.py"}
        _upsert_relationships_tx(Tx(), [
            rel | {"from_label": "Function", "from_name": "f", "from_file": "a.py",
                   "to_label": "Function", "to_name": "g", "to_file": "b.py"},
            rel | {"from_label": "Module", "from_name": "a.py", "to_label": "Function", "to_name": "g"},
        ])
        pinned, bare = Tx.queries
        assert "USING INDEX a:Function(repo_id, name, file)" in pinned
        assert "USING INDEX b:Function(repo_id, name, file)" in pinned
        assert "USING INDEX" not in bare
