"""describe_node and get_source against a live Neo4j, on a full scan: a
Python function nested in a function of another file (`nested`, which no
other file's call reaches) is no rival of the one top-level production
definition of its name, as a test stub is not; one nested in the same file
as that definition still is. Fixture data is fictional."""

import textwrap
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan
from devgraph.mcp import tools


@dataclass
class _Repo:
    repo_id: str
    path: Path


class _Registry:
    def __init__(self, repo):
        self.repo = repo

    def get(self, repo_id):
        return self.repo if repo_id == self.repo.repo_id else None


FILES = {
    "shop/dispatch.py": "def full_scan(root):\n    return root\n\n\ndef rebuild(root):\n    return root\n",
    # A local helper of another file's function, named like the real one.
    "shop/cli.py": """\
        def main():
            def full_scan(root):
                return None
            return full_scan(".")


        def run():
            def warm():
                return 1
            return warm()
        """,
    "shop/jobs.py": "def warm():\n    return 2\n",
    # One nested in the same file as the top-level definition stays a rival.
    "shop/cache.py": """\
        def rebuild(root):
            return root


        def outer():
            def rebuild(root):
                return None
            return rebuild(1)
        """,
    "tests/test_scan.py": "def test_scan():\n    def full_scan(root):\n        pass\n",
}


@pytest.fixture
def scanned(tmp_path):
    engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    repo_id = f"zz-prod-over-nested-{uuid.uuid4().hex[:8]}"
    for rel, text in FILES.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    try:
        yield engine, _Registry(_Repo(repo_id, tmp_path)), repo_id
    finally:
        engine.delete_repository(repo_id)
        engine.close()


def test_describe_node_sets_a_nested_function_of_another_file_aside(scanned):
    engine, _, repo_id = scanned
    body = tools.describe_node(engine, repo_id, "full_scan")
    assert body["status"] == "found"
    assert body["node"]["file"] == "shop/dispatch.py"
    assert body["also_nested_in_functions"] == [
        {"label": "Function", "name": "full_scan", "file": "shop/cli.py", "owner": "main"},
    ]
    assert body["also_defined_in_tests"] == [
        {"label": "Function", "name": "full_scan", "file": "tests/test_scan.py", "owner": "test_scan"},
    ]
    assert any("nested in another file's functions (1 more" in n for n in body["notices"])
    assert any("also defined in test code (1 more" in n for n in body["notices"])
    # The nested one is still reachable by its file and owner.
    local = tools.describe_node(engine, repo_id, "full_scan", file="shop/cli.py", owner="main")
    assert local["status"] == "found" and local["node"]["owner"] == "main"


def test_get_source_sets_a_nested_function_of_another_file_aside(scanned):
    engine, registry, repo_id = scanned
    body = tools.get_source(engine, registry, repo_id, "warm")
    assert body.get("status") != "ambiguous"
    assert body["file"] == "shop/jobs.py"
    assert body["source"] == "def warm():\n    return 2"
    assert [ref["file"] for ref in body["also_nested_in_functions"]] == ["shop/cli.py"]
    assert "also_defined_in_tests" not in body


def test_a_nested_function_in_the_same_file_stays_a_rival(scanned):
    engine, registry, repo_id = scanned
    described = tools.describe_node(engine, repo_id, "rebuild")
    assert described["status"] == "ambiguous" and described["count"] == 3
    source = tools.get_source(engine, registry, repo_id, "rebuild")
    assert source["status"] == "ambiguous" and source["count"] == 3
