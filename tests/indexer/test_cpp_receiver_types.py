"""A C++ method call on a typed receiver links the receiver's type's method
only, as Java's does, not every same-named method of the files it resolves
to (audit 11 item 4, C++): `o.total()` on an `Order` is no call of
`Invoice::total`."""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.cpp.extractor import extract_cpp_file
from devgraph.indexer.dispatch import full_scan, index_paths
from devgraph.mcp.tools import find_callers
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff

MODEL_H = """\
class Order { public: int total() { return 1; } int id() { return 0; } };
class Invoice { public: int total() { return 2; } };
class Rush : public Order { public: int fee() { return 3; } };
struct Box { Invoice inv; int sum() { return inv.total(); } };
"""

SVC = """\
#include "model.h"
int ca(Order& o) { return o.total(); }
int cb() { Invoice i; return i.total(); }
int cc() { auto p = new Order(); return p->total(); }
int cd(Rush r) { return r.id(); }
"""


def _owners(source, path, caller, callee):
    result = extract_cpp_file(textwrap.dedent(source), path, "repo")
    return sorted(
        r.to_owner for r in result.relationships
        if r.rel_type == "CALLS" and (r.from_name, r.to_name) == (caller, callee)
    )


@pytest.mark.parametrize(("caller", "owner"), [("ca", "Order"), ("cb", "Invoice"), ("cc", "Order")])
def test_a_typed_receiver_s_call_names_its_type_as_the_owner(caller, owner):
    owners = _owners(SVC, "svc.cpp", caller, "total")
    assert owners and set(owners) == {owner}


def test_a_field_s_type_names_the_owner():
    assert set(_owners(MODEL_H, "model.h", "sum", "total")) == {"Invoice"}


def test_a_qualified_type_and_the_file_s_namespaces_name_each_owner_it_can_have():
    source = """\
    #include "net/conn.h"
    using namespace app;
    namespace svc { int f(net::Conn& c) { return c.open(); } }
    """
    assert set(_owners(source, "svc.cpp", "f", "open")) == {
        "Conn", "net.Conn", "app.net.Conn", "svc.net.Conn",
    }


def test_an_inherited_method_of_a_base_in_the_same_file_has_the_heir_key():
    result = extract_cpp_file(MODEL_H, "model.h", "repo")
    order_id = next(n for n in result.nodes if n.label == "Function" and n.name == "id")
    assert "<Rush" in order_id.properties.get("mkeys", [])


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield test_engine
    test_engine.close()


def _calls(engine, repo_id):
    rows = engine.run_cypher(
        "MATCH (a:Function {repo_id: $r})-[e:CALLS]->(b:Function) "
        "RETURN a.name AS a, b.owner AS o, b.name AS b, e.confidence AS c ORDER BY a, o, b",
        {"r": repo_id},
    )
    return [(r["a"], f"{r['o']}.{r['b']}", r["c"]) for r in rows]


def test_live_each_receiver_links_its_own_type_s_method(engine, tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "model.h").write_text(MODEL_H)
    (root / "src" / "svc.cpp").write_text(SVC)
    # An out-of-class definition outside its namespace's block is owned by
    # `shop.Ledger`; a caller using the namespace still reaches it.
    (root / "src" / "ledger.h").write_text("namespace shop { class Ledger { public: int total(); }; }\n")
    (root / "src" / "ledger.cpp").write_text('#include "ledger.h"\nint shop::Ledger::total() { return 4; }\n')
    (root / "src" / "books.cpp").write_text(
        '#include "ledger.h"\nusing namespace shop;\nint lb(Ledger& l) { return l.total(); }\n'
    )
    repo_id = f"zz-cpprecv-{uuid.uuid4().hex[:8]}"
    try:
        provision_repository_schema(engine, root)
        engine.upsert_repository(repo_id, repo_id, str(root))
        full_scan(engine, repo_id, root)
        calls = _calls(engine, repo_id)
        total_calls = [(a, b, c) for a, b, c in calls if b.endswith(".total")]
        assert total_calls == [
            ("ca", "Order.total", "resolved"), ("cb", "Invoice.total", "resolved"),
            ("cc", "Order.total", "resolved"), ("lb", "shop.Ledger.total", "package"),
            ("sum", "Invoice.total", "resolved"),
        ]
        assert ("cd", "Order.id", "resolved") in calls  # inherited from a base in the same header
        callers = find_callers(engine, repo_id, "total", owner="Invoice")
        assert sorted(row["name"] for row in callers["results"]) == ["cb", "sum"]

        (root / "src" / "svc.cpp").write_text(SVC.replace("Invoice i; return i.total();", "Order i; return i.total();"))
        index_paths(engine, repo_id, root, {root / "src" / "svc.cpp"})
        expected = fresh_snapshot(engine, repo_id, root)
        actual = graph_snapshot(engine, repo_id)
        assert actual == expected, snapshot_diff(expected, actual)
    finally:
        engine.delete_repository(repo_id)
