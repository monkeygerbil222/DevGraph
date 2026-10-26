"""A class name means the class, against a live Neo4j (audit 11 item 5): a
Java, C# or C++ constructor is a Function named like its class and owned by
it, and describe_node and get_source answered `ambiguous` between the two.
They now answer with the class and name its `constructors`, which `owner`
reaches. Fixture code is fictional."""

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
    "java/shop/Order.java": """\
        package shop;
        public class Order {
            private int n;
            public Order() { this(0); }
            public Order(int n) { this.n = n; }
            public int total() { return n; }
            public static class Line {
                public Line() {}
            }
        }
        """,
    "cs/Svc.cs": """\
        namespace Shop {
          public class Svc {
            public Svc() { }
            public int Run() { return 1; }
          }
        }
        """,
    "cpp/shapes.hpp": """\
        namespace geo {
        class Circle {
        public:
            Circle(double r) : r_(r) {}
            double area() const { return 3.14 * r_ * r_; }
        private:
            double r_;
        };
        }
        """,
    "java/billing/Invoice.java": """\
        package billing;
        public class Invoice { public Invoice() {} }
        """,
    "java/legacy/Invoice.java": """\
        package legacy;
        public class Invoice { public Invoice() {} }
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
    root = tmp_path_factory.mktemp("class-lookup")
    repo_id = f"zz-class-lookup-{uuid.uuid4().hex[:8]}"
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root)
    yield repo_id, _Registry([_Repo(repo_id, root)])
    engine.delete_repository(repo_id)


@pytest.mark.parametrize("name, file, owner", [
    ("Order", "java/shop/Order.java", ""),
    ("Line", "java/shop/Order.java", "Order"),
    ("Svc", "cs/Svc.cs", ""),
    ("Circle", "cpp/shapes.hpp", ""),
])
def test_describe_node_and_get_source_answer_a_class_name_with_the_class(engine, repo, name, file, owner):
    repo_id, registry = repo
    constructor = {"name": name, "file": file, "owner": f"{owner}.{name}" if owner else name}

    described = tools.describe_node(engine, repo_id, name)
    assert described["status"] == "found"
    assert described["node"]["label"] == "Class" and described["node"]["file"] == file
    assert described["constructors"] == [{"label": "Function", **constructor}]

    source = tools.get_source(engine, registry, repo_id, name)
    assert "status" not in source
    assert source["label"] == "Class" and source["file"] == file
    assert source["source"].lstrip().startswith(("public class", "class", "public static class"))
    assert source["constructors"] == [constructor]


def test_owner_still_reaches_the_constructor(engine, repo):
    repo_id, registry = repo
    source = tools.get_source(engine, registry, repo_id, "Order", owner="Order")
    assert source["label"] == "Function" and "constructors" not in source
    described = tools.describe_node(engine, repo_id, "Order.Order")
    assert described["status"] == "found" and described["node"]["label"] == "Function"


def test_two_classes_of_a_name_stay_ambiguous_between_the_classes_only(engine, repo):
    repo_id, registry = repo
    described = tools.describe_node(engine, repo_id, "Invoice")
    assert described["status"] == "ambiguous"
    assert [(c["label"], c["file"]) for c in described["candidates"]] == [
        ("Class", "java/billing/Invoice.java"), ("Class", "java/legacy/Invoice.java"),
    ]
    source = tools.get_source(engine, registry, repo_id, "Invoice")
    assert source["status"] == "ambiguous"
    assert sorted((c["label"], c["file"]) for c in source["candidates"]) == [
        ("Class", "java/billing/Invoice.java"), ("Class", "java/legacy/Invoice.java"),
    ]
