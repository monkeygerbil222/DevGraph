"""A class contains its own members only, against a live Neo4j (audit 11
review F1): a CONTAINS edge's target end ignores heir keys and open
classes, which only calls follow. When an heir that inherited a member
comes to define it itself, its file's re-index must not keep a CONTAINS
from the heir to the base's member (written while the base's member still
carried the heir's key), in Python, Rust, Go and C++ (whose extractor writes
heir keys too since audit 11's C++ receiver change): live indexing equals a
fresh scan."""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan, index_paths
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff


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
    repo = f"zz-containsheirs-{uuid.uuid4().hex[:8]}"
    for each in (repo, f"{repo}_fresh"):
        engine.delete_repository(each)
    yield repo
    for each in (repo, f"{repo}_fresh"):
        engine.delete_repository(each)


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def _contains(engine, repo_id, heir, member):
    rows = engine.run_cypher(
        "MATCH (c:Class {repo_id: $r, name: $heir})-[:CONTAINS]->(f:Function {name: $member}) RETURN f.owner AS o",
        {"r": repo_id, "heir": heir, "member": member},
    )
    return sorted(row["o"] for row in rows)


PYTHON = ("m.py", """\
    class Base:
        def save(self):
            return 1


    class Sub(Base):
        def other(self):
            return 2
    """, "    def other(self):\n        return 2\n", "    def other(self):\n        return 2\n\n    def save(self):\n        return 3\n",
    "Sub", "save")

RUST = ("src/model.rs", """\
    pub struct Order { pub n: u32 }
    pub trait Local { fn hi(&self) -> u32; fn deflt(&self) -> u32 { 5 } }
    impl Local for Order { fn hi(&self) -> u32 { 2 } }
    """, "fn hi(&self) -> u32 { 2 } }", "fn hi(&self) -> u32 { 2 } fn deflt(&self) -> u32 { 6 } }",
    "Order", "deflt")

GO = ("model/model.go", """\
    package model

    type Base struct{}

    func (b *Base) ID() int { return 0 }

    type Order struct{ Base }
    """, "type Order struct{ Base }\n", "type Order struct{ Base }\n\nfunc (o *Order) ID() int { return 1 }\n",
    "Order", "ID")

CPP = ("shop.hpp", """\
    class Base {
    public:
        int save() { return 1; }
    };

    class Sub : public Base {
    public:
        int other() { return 2; }
    };
    """, "    int other() { return 2; }\n", "    int other() { return 2; }\n    int save() { return 3; }\n",
    "Sub", "save")


@pytest.mark.parametrize("case", [PYTHON, RUST, GO, CPP], ids=["python", "rust", "go", "cpp"])
def test_an_heir_defining_an_inherited_member_contains_only_its_own(engine, repo_id, tmp_path, case):
    rel, text, old, new, heir, member = case
    _write(tmp_path, "Cargo.toml", '[package]\nname = "shop"\nversion = "0.1.0"\n')
    _write(tmp_path, "src/lib.rs", "pub mod model;\n")
    _write(tmp_path, "go.mod", "module example.com/shop\n\ngo 1.22\n")
    path = _write(tmp_path, rel, text)
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    assert _contains(engine, repo_id, heir, member) == []

    path.write_text(textwrap.dedent(text).replace(old, new))
    index_paths(engine, repo_id, tmp_path, {path})
    assert _contains(engine, repo_id, heir, member) == [heir]
    expected = fresh_snapshot(engine, repo_id, tmp_path)
    actual = graph_snapshot(engine, repo_id)
    if actual != expected:
        pytest.fail("graph does not equal a fresh full_scan:\n" + snapshot_diff(expected, actual))
