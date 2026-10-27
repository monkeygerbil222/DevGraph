"""The docs tools against a live Neo4j: decisions and requirements are found by
id, title or body; explain_decision suggests decisions by title; with no docs
path set the tools say so; and every notice is in the one `notices` list.
Fixture data is fictional."""

import uuid
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from devgraph.graph.engine import GraphEngine
from devgraph.mcp import tools
from devgraph.registry.store import RepoRecord

REPO = f"zz-docs-findable-{uuid.uuid4().hex[:8]}"


class _Registry:
    def __init__(self, docs_path):
        self.record = RepoRecord(REPO, Path("/srv/example"), True, True, None, docs_path=docs_path)

    def get(self, repo_id):
        return self.record if repo_id == REPO else None


@pytest.fixture
def engine():
    engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    engine.init_schema()
    engine.upsert_nodes([
        {"label": "Module", "repo_id": REPO, "name": "api/app.py", "properties": {"file": "api/app.py"}},
        {"label": "DesignDecision", "repo_id": REPO, "name": "ADR-001", "properties": {
            "source_file": "docs/adr-1.md", "title": "Use Redis for sessions", "body": "Sessions live in a cache."}},
        {"label": "Requirement", "repo_id": REPO, "name": "REQ-7", "properties": {
            "source_file": "docs/req-7.md", "title": "Sessions survive restarts", "body": ""}},
    ])
    try:
        yield engine
    finally:
        engine.delete_repository(REPO)
        engine.close()


def _found(engine, query):
    return [(r["labels"][0], r["name"], r["description"]) for r in tools.search_component(engine, REPO, query)["results"]]


def test_search_finds_design_decision_by_id_and_title(engine):
    assert _found(engine, "ADR-001") == [("DesignDecision", "ADR-001", "Use Redis for sessions")]
    assert _found(engine, "redis") == [("DesignDecision", "ADR-001", "Use Redis for sessions")]
    assert ("Requirement", "REQ-7", "Sessions survive restarts") in _found(engine, "survive")
    assert _found(engine, "cache") == [("DesignDecision", "ADR-001", "Use Redis for sessions")]  # by body


def test_explain_decision_suggests_by_title(engine):
    with pytest.raises(ToolError) as caught:
        tools.explain_decision(engine, REPO, "Redis sessions", registry=_Registry("docs"))
    message = str(caught.value)
    assert "name='ADR-001', title='Use Redis for sessions'" in message
    assert "search_component finds decisions by id, title or body" in message
    assert tools.explain_decision(engine, REPO, "ADR-001", registry=_Registry("docs"))["title"] == (
        "Use Redis for sessions"
    )


def test_requirements_tools_notice_when_no_docs_path(engine):
    registry = _Registry(None)
    for result in (
        tools.find_requirements_for(engine, REPO, "api/app.py", registry=registry),
        tools.trace_design_rationale(engine, REPO, "api/app.py", registry=registry),
    ):
        assert "notice" not in result
        assert any(f"devgraph annotate {REPO} --docs-path" in n for n in result["notices"])
    with pytest.raises(ToolError, match="no docs path is set"):
        tools.explain_decision(engine, REPO, "ADR-9", registry=registry)
    docs_set = _Registry("docs")
    assert "notices" not in tools.find_requirements_for(engine, REPO, "api/app.py", registry=docs_set)
    assert "notices" not in tools.trace_design_rationale(engine, REPO, "api/app.py", registry=docs_set)
