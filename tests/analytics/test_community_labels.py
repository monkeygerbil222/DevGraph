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


def _twin_triangles(folder, hub_a, hub_b):
    """Two unconnected triangles in `folder`, each with a hub named as given."""
    nodes = [node(n, f"{folder}/{n}.py") for n in ("a1", "a2", "b1", "b2")]
    nodes += [{"id": "ha", "name": hub_a, "labels": ["Function"], "file": f"{folder}/ha.py"},
              {"id": "hb", "name": hub_b, "labels": ["Function"], "file": f"{folder}/hb.py"}]
    edges = [edge("a1", "ha"), edge("a2", "ha"), edge("a1", "a2"), edge("b1", "hb"), edge("b2", "hb"), edge("b1", "b2")]
    return compute_insights(nodes, edges).communities


def test_labels_still_shared_after_the_top_member_add_the_community_number():
    labels = sorted(c["label"] for c in _twin_triangles("shop", "run", "run"))
    assert labels == ["shop (run) #0", "shop (run) #1"]


def test_a_long_label_keeps_its_distinguishing_suffix():
    communities = _twin_triangles("x" * 100, "run", "run")
    assert sorted(c["label"] for c in communities) == [
        "x" * (80 - len(" (run) #0")) + " (run) #0", "x" * (80 - len(" (run) #1")) + " (run) #1",
    ]
    assert all(len(c["label"]) == 80 for c in communities)
