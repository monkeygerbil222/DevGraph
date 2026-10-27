"""describe_node and get_source against a live Neo4j: a name with exactly one
definition outside test code means that one, whatever test stubs share it,
and says the stubs exist; two production definitions stay ambiguous. god_nodes
rows, and the dashboard's degree-ranked list, tell same-named nodes apart by
file and owner. Fixture data is fictional."""

import re
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from devgraph.graph.engine import GraphEngine
from devgraph.mcp import tools

REPO = f"zz-prod-over-stubs-{uuid.uuid4().hex[:8]}"
_HTML = Path(__file__).parents[2] / "devgraph" / "dashboard" / "static" / "index.html"


@dataclass
class _Repo:
    repo_id: str
    path: Path


class _Registry:
    def __init__(self, repo):
        self.repo = repo

    def get(self, repo_id):
        return self.repo if repo_id == self.repo.repo_id else None


def _fn(name, file, owner="", start=1, end=2):
    return {"label": "Function", "repo_id": REPO, "name": name,
            "properties": {"file": file, "owner": owner, "start_line": start, "end_line": end}}


@pytest.fixture
def seeded(tmp_path):
    engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    engine.init_schema()
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "dispatch.py").write_text("def index_paths(paths):\n    return paths\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_watch.py").write_text(
        "def test_batch():\n    def index_paths(*a):\n        pass\n"
    )
    engine.upsert_nodes([
        _fn("index_paths", "shop/dispatch.py"),
        _fn("index_paths", "tests/test_watch.py", owner="test_batch", start=2, end=3),  # a monkeypatch stub
        _fn("index_paths", "tests/conftest.py"),
        _fn("save", "shop/a.py"),
        _fn("save", "shop/b.py"),
        _fn("save", "tests/test_save.py"),
        _fn("__init__", "shop/engine.py", owner="Engine"),
        _fn("__init__", "shop/pool.py", owner="Pool"),
        *[_fn(f"use{i}", "shop/use.py") for i in range(3)],
    ])
    calls = [("use0", "shop/engine.py"), ("use1", "shop/engine.py"), ("use2", "shop/engine.py"),
             ("use0", "shop/pool.py")]
    engine.upsert_relationships([
        {"from_label": "Function", "from_name": caller, "rel_type": "CALLS", "to_label": "Function",
         "to_name": "__init__", "repo_id": REPO, "from_file": "shop/use.py", "to_file": target}
        for caller, target in calls
    ])
    try:
        yield engine, _Registry(_Repo(REPO, tmp_path))
    finally:
        engine.delete_repository(REPO)
        engine.close()


def test_describe_node_prefers_the_single_production_definition(seeded):
    engine, _ = seeded
    body = tools.describe_node(engine, REPO, "index_paths")
    assert body["status"] == "found"
    assert body["node"]["file"] == "shop/dispatch.py"
    assert body["also_defined_in_tests"] == [
        {"label": "Function", "name": "index_paths", "file": "tests/conftest.py", "owner": ""},
        {"label": "Function", "name": "index_paths", "file": "tests/test_watch.py", "owner": "test_batch"},
    ]
    assert any("also defined in test code (2 more" in n for n in body["notices"])
    # A test candidate is still reachable by its file and owner.
    stub = tools.describe_node(engine, REPO, "index_paths", file="tests/test_watch.py", owner="test_batch")
    assert stub["status"] == "found" and stub["node"]["owner"] == "test_batch"


def test_get_source_prefers_production_over_test_stub(seeded):
    engine, registry = seeded
    body = tools.get_source(engine, registry, REPO, "index_paths")
    assert body.get("status") != "ambiguous"
    assert body["file"] == "shop/dispatch.py"
    assert body["source"] == "def index_paths(paths):\n    return paths"
    assert sorted(ref["file"] for ref in body["also_defined_in_tests"]) == ["tests/conftest.py", "tests/test_watch.py"]
    assert body["notices"]


def test_ambiguous_kept_when_two_production_definitions(seeded):
    engine, registry = seeded
    described = tools.describe_node(engine, REPO, "save")
    assert described["status"] == "ambiguous" and described["count"] == 3
    source = tools.get_source(engine, registry, REPO, "save")
    assert source["status"] == "ambiguous" and source["count"] == 3


def test_god_nodes_rows_carry_file_and_owner(seeded):
    engine, _ = seeded
    rows = tools.god_nodes(engine, REPO, max_results=2)["results"]
    assert [(r["name"], r["file"], r["owner"], r["degree"]) for r in rows] == [
        ("__init__", "shop/engine.py", "Engine", 3),
        ("use0", "shop/use.py", "", 2),
    ]


def test_dashboard_degree_list_carries_file_and_owner(seeded):
    engine, _ = seeded
    (template,) = re.findall(r"const godJson = await runCypher\(`(.*?)`\);", _HTML.read_text(encoding="utf-8"))
    rows = engine.run_cypher(template.replace("${where}", f'WHERE n.repo_id = "{REPO}" '), {})
    assert (rows[0]["name"], rows[0]["file"], rows[0]["owner"]) == ("__init__", "shop/engine.py", "Engine")
