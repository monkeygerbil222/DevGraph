"""The canvas's node ceiling bounds the nodes the canvas draws, live.

The canvas query caps the matched nodes (`WITH n LIMIT <ceiling>`) and then
returns each one's relationships with their far ends (`m`). Those far ends
used to come back whatever they were, so the ceiling bounded only half the
canvas: django's default view asked for 1,500 nodes and drew 8,204. The query
index.html builds (currentStateQuery, printed by canvas_ceiling_query.js) is
run here through the real /api/cypher route against Neo4j, and the distinct
nodes in its reply are counted.

Skipped when node isn't on PATH or Neo4j isn't reachable.
"""

import json
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from devgraph.dashboard.app import build_app
from devgraph.dashboard.events import EventBroadcaster
from devgraph.graph.engine import GraphEngine
from devgraph.registry.store import RepoRegistry

_SCRIPT = Path(__file__).with_name("canvas_ceiling_query.js")
REPO = f"dash_ceiling_{uuid.uuid4().hex[:10]}"
LEAVES = 10

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@pytest.fixture
def client():
    engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    engine.init_schema()
    # A star: one module containing every function, and a chain of calls
    # between the functions, so any capped subset has relationships leading
    # out of it.
    engine.upsert_node("Module", REPO, "hub.py")
    for i in range(LEAVES):
        engine.upsert_node("Function", REPO, f"leaf{i}")
        engine.upsert_relationship("Module", "hub.py", "CONTAINS", "Function", f"leaf{i}", REPO)
        if i:
            engine.upsert_relationship("Function", f"leaf{i - 1}", "CALLS", "Function", f"leaf{i}", REPO)
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = RepoRegistry(Path(tmpdir) / "registry.sqlite3")
            yield TestClient(build_app(engine, registry, EventBroadcaster()), base_url="http://127.0.0.1")
            registry.close()
    finally:
        engine.delete_repository(REPO)
        engine.close()


def _state_query(**state) -> str:
    out = subprocess.run(
        [shutil.which("node"), str(_SCRIPT), json.dumps({"repo": REPO, **state})],
        capture_output=True, text=True, timeout=60, check=True,
    )
    return out.stdout


def _drawn(client, query: str) -> tuple[set[str], int]:
    body = client.post("/api/cypher", json={"query": query, "canvas": True}).json()
    assert not body["errors"], body["errors"]
    rows = body["results"][0]["data"]
    nodes = {n["key"] or n["id"] for row in rows for n in row["graph"]["nodes"]}
    rels = {r["id"] for row in rows for r in row["graph"]["relationships"]}
    return nodes, len(rels)


TOTAL = LEAVES + 1
REL_MAX = 1000


@pytest.mark.parametrize("ceiling", [1, 3, TOTAL - 1])
def test_the_node_ceiling_bounds_every_node_drawn(client, ceiling):
    query = _state_query(nodeCeiling=ceiling, nodeMax=TOTAL, relCeiling=REL_MAX, relMax=REL_MAX)
    nodes, _ = _drawn(client, query)
    assert len(nodes) == ceiling, query


def test_relationships_between_capped_nodes_still_come_back(client):
    query = _state_query(nodeCeiling=TOTAL - 1, nodeMax=TOTAL, relCeiling=REL_MAX, relMax=REL_MAX)
    _, rels = _drawn(client, query)
    assert rels > 0, query


def test_the_ceiling_bounds_an_isolated_relationship_type(client):
    query = _state_query(nodeCeiling=3, nodeMax=TOTAL, relCeiling=REL_MAX, relMax=REL_MAX, isolatedRel="CALLS")
    nodes, _ = _drawn(client, query)
    assert len(nodes) <= 3, query


def test_the_ceiling_bounds_an_isolated_entity_type(client):
    query = _state_query(nodeCeiling=3, nodeMax=LEAVES, relCeiling=REL_MAX, relMax=REL_MAX, isolatedEntity="Function")
    nodes, _ = _drawn(client, query)
    assert len(nodes) == 3, query


def test_an_uncapped_view_still_draws_the_whole_scope(client):
    query = _state_query(nodeCeiling=TOTAL, nodeMax=TOTAL, relCeiling=REL_MAX, relMax=REL_MAX)
    nodes, rels = _drawn(client, query)
    assert len(nodes) == TOTAL and rels == LEAVES + (LEAVES - 1), query
