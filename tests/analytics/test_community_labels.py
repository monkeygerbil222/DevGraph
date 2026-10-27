"""Community labels name production code: a community's test-code members
don't name it while it has production members, and two communities that
would share a label are told apart by their top member. Synthetic graphs,
no Neo4j."""

from devgraph.analytics.insights import compute_insights


def node(nid, file=None, label="Function"):
    return {"id": nid, "name": nid, "labels": [label], "file": file}


def edge(source, target):
    return {"source": source, "target": target, "type": "CALLS"}


def test_community_label_prefers_production_dirs():
    # Three test functions and two production ones, all one community.
    nodes = [node("run_cypher", "shop/graph/engine.py"), node("full_scan", "shop/indexer/scan.py"),
             node("t1", "tests/indexer/test_a.py"), node("t2", "tests/indexer/test_b.py"),
             node("t3", "tests/indexer/test_c.py")]
    ids = [n["id"] for n in nodes]
    edges = [edge(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]]  # one clique: one community
    (community,) = compute_insights(nodes, edges).communities
    assert community["label"] == "shop/graph"


def test_a_community_of_test_code_only_is_named_by_its_test_folder():
    nodes = [node("t1", "tests/indexer/test_a.py"), node("t2", "tests/indexer/test_b.py")]
    assert compute_insights(nodes, [edge("t1", "t2")]).communities[0]["label"] == "tests/indexer"


def test_duplicate_community_labels_disambiguated():
    # Two triangles in one folder, joined by nothing: two communities, both `shop`.
    nodes = [node(n, f"shop/{n}.py") for n in ("a1", "a2", "hub_a", "b1", "b2", "hub_b")]
    edges = [edge("a1", "hub_a"), edge("a2", "hub_a"), edge("a1", "a2"),
             edge("b1", "hub_b"), edge("b2", "hub_b"), edge("b1", "b2")]
    labels = sorted(c["label"] for c in compute_insights(nodes, edges).communities)
    assert labels == ["shop (hub_a)", "shop (hub_b)"]
