"""list_services against a live Neo4j: a service declared in two compose files
is two rows, told apart by the file declaring each and what it runs (image)
or builds (build_context). Fixture data is fictional."""

import uuid

import pytest

from devgraph.graph.engine import GraphEngine
from devgraph.mcp import tools

REPO = f"zz-list-services-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def engine():
    engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    engine.init_schema()
    engine.upsert_nodes([
        {"label": "Service", "repo_id": REPO, "name": "shop-db", "properties": {"file": "deploy/docker-compose.yml"}},
        {"label": "Service", "repo_id": REPO, "name": "shop-db", "properties": {"file": "deploy/podman-compose.yml"}},
        {"label": "Service", "repo_id": REPO, "name": "shop-api",
         "properties": {"file": "deploy/docker-compose.yml", "build_context": "api"}},
        {"label": "Container", "repo_id": REPO, "name": "postgres", "properties": {"image": "postgres:16"}},
    ])
    engine.upsert_relationships([
        {"from_label": "Service", "from_name": "shop-db", "rel_type": "RUNS", "to_label": "Container",
         "to_name": "postgres", "repo_id": REPO, "from_file": file}
        for file in ("deploy/docker-compose.yml", "deploy/podman-compose.yml")
    ])
    try:
        yield engine
    finally:
        engine.delete_repository(REPO)
        engine.close()


def test_list_services_rows_carry_compose_file(engine):
    rows = tools.list_services(engine, REPO)["results"]
    assert rows == [
        {"name": "shop-api", "repo_id": REPO, "description": None, "file": "deploy/docker-compose.yml",
         "build_context": "api"},
        {"name": "shop-db", "repo_id": REPO, "description": None, "file": "deploy/docker-compose.yml",
         "image": "postgres:16"},
        {"name": "shop-db", "repo_id": REPO, "description": None, "file": "deploy/podman-compose.yml",
         "image": "postgres:16"},
    ]
