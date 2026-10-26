"""A Rust call on a typed value of another file's type links that type's
method only (audit 11 item 4): two types of one module with a same-named
method are told apart by their owner, for a typed parameter, a typed
local, a struct-literal local and an associated `Type::f()` path, and a
trait's default method reaches each type of its file implementing it (heir
keys). The edges stay `resolved`, and live indexing equals a fresh scan."""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan, index_paths
from devgraph.indexer.rust.extractor import extract_rust_file
from devgraph.mcp import tools
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff

MODEL = """\
    pub struct Order { pub n: u32 }
    pub struct Invoice { pub n: u32 }
    pub trait Priced {
        fn price(&self) -> u32;
        fn doubled(&self) -> u32 { self.price() * 2 }
    }
    impl Order {
        pub fn new(n: u32) -> Self { Order { n } }
        pub fn total(&self) -> u32 { self.n }
    }
    impl Invoice {
        pub fn new(n: u32) -> Self { Invoice { n } }
        pub fn total(&self) -> u32 { self.n + 1 }
    }
    impl Priced for Order { fn price(&self) -> u32 { self.n } }
    """

SVC = """\
    use crate::model::{Invoice, Order, Priced};

    pub fn a(o: &Order) -> u32 { o.total() }
    pub fn b() -> u32 { let o = Order { n: 1 }; o.total() }
    pub fn c(i: Invoice) -> u32 { i.total() }
    pub fn d() -> u32 { let i: Invoice = Invoice::new(2); i.total() }
    pub fn e(o: Order) -> u32 { o.doubled() }
    """


def _owners(rows, caller, callee):
    """The (owner, confidence) of `caller`'s rows to `callee` pinned to
    src/model.rs (the others pin where the module could also be:
    src/model/mod.rs, and the src/model/ directory)."""
    return {(r.to_owner, r.properties["confidence"]) for r in rows
            if (r.from_name, r.to_name, r.to_file) == (caller, callee, "src/model.rs")}


def test_a_typed_receiver_links_only_its_own_types_method():
    result = extract_rust_file(textwrap.dedent(SVC), "src/svc.rs", "repo")
    rows = [r for r in result.relationships if r.rel_type == "CALLS"]
    assert _owners(rows, "a", "total") == {("Order", "resolved")}
    assert _owners(rows, "b", "total") == {("Order", "resolved")}
    assert _owners(rows, "c", "total") == {("Invoice", "resolved")}
    assert _owners(rows, "d", "total") == {("Invoice", "resolved")}
    # An associated function through the type's path names the type too.
    assert _owners(rows, "d", "new") == {("Invoice", "resolved")}
    # A trait's default method is reached through the implementing type.
    assert _owners(rows, "e", "doubled") == {("Order", "resolved")}


def test_a_traits_default_method_is_a_member_of_its_files_implementers():
    result = extract_rust_file(textwrap.dedent(MODEL), "src/model.rs", "repo")
    doubled = next(n for n in result.nodes if n.label == "Function" and n.name == "doubled")
    assert doubled.properties["owner"] == "Priced"
    assert doubled.properties["mkeys"] == ["<Order"]
    module = next(n for n in result.nodes if n.label == "Module")
    assert module.properties["heir_open"] == []


# --- live --------------------------------------------------------------------------


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
    repo = f"zz-rustowners-{uuid.uuid4().hex[:8]}"
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


def _equals_fresh(engine, repo_id, root):
    expected = fresh_snapshot(engine, repo_id, root)
    actual = graph_snapshot(engine, repo_id)
    if actual != expected:
        pytest.fail("graph does not equal a fresh full_scan:\n" + snapshot_diff(expected, actual))


def _callers(engine, repo_id, name, owner):
    result = tools.find_callers(engine, repo_id, name, owner=owner)
    return sorted((row["name"], row["confidence"]) for row in result["results"])


def test_find_callers_tells_two_types_same_named_methods_apart(engine, repo_id, tmp_path):
    _write(tmp_path, "Cargo.toml", '[package]\nname = "shop"\nversion = "0.1.0"\n')
    _write(tmp_path, "src/lib.rs", "pub mod model;\npub mod svc;\n")
    model = _write(tmp_path, "src/model.rs", MODEL)
    _write(tmp_path, "src/svc.rs", SVC)
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    assert _callers(engine, repo_id, "total", "Invoice") == [("c", "resolved"), ("d", "resolved")]
    assert _callers(engine, repo_id, "total", "Order") == [("a", "resolved"), ("b", "resolved")]
    assert _callers(engine, repo_id, "new", "Order") == []
    assert _callers(engine, repo_id, "doubled", "Priced") == [("e", "resolved")]
    _equals_fresh(engine, repo_id, tmp_path)

    # Order stops implementing Priced: `o.doubled()` no longer reaches the default.
    model.write_text(textwrap.dedent(MODEL).replace("impl Priced for Order { fn price(&self) -> u32 { self.n } }\n", ""))
    index_paths(engine, repo_id, tmp_path, {model})
    assert _callers(engine, repo_id, "doubled", "Priced") == []
    _equals_fresh(engine, repo_id, tmp_path)
