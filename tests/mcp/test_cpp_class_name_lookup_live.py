"""A C++ class declared in a header, with its constructor defined in a .cpp,
answers its name with the class (audit 11 item 5, both halves): the .cpp
makes no file-less class stub any more, and the out-of-class constructor,
owned by the class however the definition qualifies it, is no candidate of
its own. Fixture code is fictional."""

import textwrap
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan
from devgraph.mcp import tools


@pytest.fixture(scope="module")
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield test_engine
    test_engine.close()


FILES = {
    "order.hpp": """\
        class Order {
        public:
            Order(int n);
            int total() const;
        private:
            int n_;
        };
        """,
    "order.cpp": """\
        #include "order.hpp"
        Order::Order(int n) : n_(n) {}
        int Order::total() const { return n_; }
        """,
    "ledger.hpp": """\
        namespace shop {
        class Ledger {
        public:
            Ledger();
            int sum() const;
        };
        }
        """,
    "ledger.cpp": """\
        #include "ledger.hpp"
        shop::Ledger::Ledger() {}
        int shop::Ledger::sum() const { return 0; }
        """,
}


@dataclass
class _Repo:
    repo_id: str
    path: Path
    active: bool = True


class _Registry:
    def __init__(self, repos):
        self.repos = repos

    def get(self, repo_id):
        return next((r for r in self.repos if r.repo_id == repo_id), None)


@pytest.fixture(scope="module")
def repo(engine, tmp_path_factory):
    root = tmp_path_factory.mktemp("cpp-class-lookup")
    repo_id = f"zz-cpp-class-lookup-{uuid.uuid4().hex[:8]}"
    for rel, text in FILES.items():
        (root / rel).write_text(textwrap.dedent(text))
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root)
    yield repo_id, _Registry([_Repo(repo_id, root)])
    engine.delete_repository(repo_id)


@pytest.mark.parametrize("name, header, source", [("Order", "order.hpp", "order.cpp"), ("Ledger", "ledger.hpp", "ledger.cpp")])
def test_a_header_class_with_an_out_of_class_constructor_is_found(engine, repo, name, header, source):
    repo_id, registry = repo
    described = tools.describe_node(engine, repo_id, name)
    assert described["status"] == "found", described
    assert described["node"]["label"] == "Class" and described["node"]["file"] == header
    assert [(c["label"], c["file"]) for c in described["constructors"]] == [("Function", source)]

    found = tools.get_source(engine, registry, repo_id, name)
    assert "status" not in found, found
    assert found["label"] == "Class" and found["file"] == header
    assert [c["file"] for c in found["constructors"]] == [source]
