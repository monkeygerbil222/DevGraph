"""A Go method call on a value of a concrete type of another package (a
pointer, a composite literal, `new(T)`) links that type's method only (audit
11 item 4), still at package confidence; a struct's method promoted from a
struct of its file it embeds is reached through it (heir keys). A value
whose type may be an interface (`o model.Order`) keeps linking every method
of the name in the package. Live indexing equals a fresh scan."""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan, index_paths
from devgraph.indexer.go.extractor import extract_go_file
from devgraph.indexer.resolver_config import ResolverConfig
from devgraph.mcp import tools
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff

GO_MOD = "module example.com/shop\n\ngo 1.22\n"

MODEL = """\
    package model

    type Base struct{}

    func (b *Base) ID() int { return 0 }

    type Order struct{ Base }

    func (o *Order) Total() int { return 1 }

    type Invoice struct{}

    func (i *Invoice) Total() int { return 2 }

    func (i *Invoice) ID() int { return 3 }
    """

SVC = """\
    package svc

    import "example.com/shop/model"

    func GA(o *model.Order) int { return o.Total() }
    func GB() int { i := &model.Invoice{}; return i.Total() }
    func GC() int { i := new(model.Invoice); return i.Total() }
    func GD() int { o := model.Order{}; return o.ID() }
    func GE(o model.Order) int { return o.Total() }
    """


def _owners(tmp_path, caller, callee):
    (tmp_path / "go.mod").write_text(GO_MOD)
    result = extract_go_file(textwrap.dedent(SVC), "svc/svc.go", "repo", ResolverConfig(tmp_path))
    return {(r.to_owner, r.properties["confidence"]) for r in result.relationships
            if r.rel_type == "CALLS" and (r.from_name, r.to_name) == (caller, callee)}


def test_a_concrete_receiver_links_only_its_own_types_method(tmp_path):
    assert _owners(tmp_path, "GA", "Total") == {("Order", "package")}
    assert _owners(tmp_path, "GB", "Total") == {("Invoice", "package")}
    assert _owners(tmp_path, "GC", "Total") == {("Invoice", "package")}
    assert _owners(tmp_path, "GD", "ID") == {("Order", "package")}
    # A value of a type that may be an interface: any Total of the package.
    assert _owners(tmp_path, "GE", "Total") == {(None, "package")}


def test_an_embedded_structs_method_is_a_member_of_its_embedder():
    result = extract_go_file(textwrap.dedent(MODEL), "model/model.go", "repo")
    base_id = next(n for n in result.nodes if n.label == "Function" and n.name == "ID"
                   and n.properties["owner"] == "Base")
    assert base_id.properties["mkeys"] == ["<Order"]
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
    repo = f"zz-goowners-{uuid.uuid4().hex[:8]}"
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
    _write(tmp_path, "go.mod", GO_MOD)
    model = _write(tmp_path, "model/model.go", MODEL)
    _write(tmp_path, "svc/svc.go", SVC)
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    assert _callers(engine, repo_id, "Total", "Invoice") == [("GB", "package"), ("GC", "package"), ("GE", "package")]
    assert _callers(engine, repo_id, "Total", "Order") == [("GA", "package"), ("GE", "package")]
    assert _callers(engine, repo_id, "ID", "Base") == [("GD", "package")]
    assert _callers(engine, repo_id, "ID", "Invoice") == []
    _equals_fresh(engine, repo_id, tmp_path)

    # Order stops embedding Base: `o.ID()` no longer reaches Base's.
    model.write_text(textwrap.dedent(MODEL).replace("type Order struct{ Base }", "type Order struct{}"))
    index_paths(engine, repo_id, tmp_path, {model})
    assert _callers(engine, repo_id, "ID", "Base") == []
    _equals_fresh(engine, repo_id, tmp_path)
