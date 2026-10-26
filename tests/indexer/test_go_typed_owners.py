"""A Go method call on a value of a struct its file declares links that
struct's method only (audit 11 item 4, narrowed by review F2), still at
package confidence, when every type the struct embeds is a struct of the
file too: its methods are then its own and those promoted from them (heir
keys). Any other receiver keeps linking every method of the name in the
package: another file's type may embed a type of a third file, whose
promoted method the owner would lose, or be an interface. Live indexing
equals a fresh scan."""

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

USE = """\
    package model

    type Base struct{}

    func (b *Base) ID() int { return 0 }

    type Order struct{ Base }
    type Plain struct{}
    type Cross struct{ Remote }
    type Wrapped struct{ Cross }
    type Generic struct{ Box[int] }

    func UseOrder() int { o := &Order{}; return o.Total() + o.ID() }
    func UsePlain() int { p := Plain{}; return p.Total() }
    func UseCross() int { c := &Cross{}; return c.Total() }
    func UseWrapped() int { w := Wrapped{}; return w.Total() }
    func UseGeneric() int { g := Generic{}; return g.Total() }
    """

SVC = """\
    package svc

    import "example.com/shop/model"

    func PromotedCross() int { o := &model.Order{}; return o.ID() }
    """


def _owners(tmp_path, source, path, caller, callee):
    (tmp_path / "go.mod").write_text(GO_MOD)
    result = extract_go_file(textwrap.dedent(source), path, "repo", ResolverConfig(tmp_path))
    return {(r.to_owner, r.properties["confidence"]) for r in result.relationships
            if r.rel_type == "CALLS" and (r.from_name, r.to_name) == (caller, callee)}


def test_a_struct_of_the_file_with_known_embeds_links_only_its_own_method(tmp_path):
    def owners(caller, callee="Total"):
        return _owners(tmp_path, USE, "model/use.go", caller, callee)

    assert owners("UseOrder") == {("Order", "package")}
    assert owners("UseOrder", "ID") == {("Order", "package")}
    assert owners("UsePlain") == {("Plain", "package")}
    # Embedding another file's type, directly or through a struct of the
    # file, or one it can't name: any Total of the package.
    assert owners("UseCross") == {(None, "package")}
    assert owners("UseWrapped") == {(None, "package")}
    assert owners("UseGeneric") == {(None, "package")}
    # Base.ID is promoted to Order: its heir key.
    result = extract_go_file(textwrap.dedent(USE), "model/use.go", "repo")
    base_id = next(n for n in result.nodes if n.label == "Function" and n.name == "ID")
    assert base_id.properties["mkeys"] == ["<Order"]
    assert next(n for n in result.nodes if n.label == "Module").properties["heir_open"] == []


def test_another_files_type_keeps_every_method_of_the_name(tmp_path):
    assert _owners(tmp_path, SVC, "svc/svc.go", "PromotedCross", "ID") == {(None, "package")}


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


PLAIN = """\
    package model

    type Plain struct{}

    func UsePlain() int { p := Plain{}; return p.Total() }
    """


def test_find_callers_and_promotion_across_files(engine, repo_id, tmp_path):
    _write(tmp_path, "go.mod", GO_MOD)
    _write(tmp_path, "model/base.go", "package model\n\ntype Base struct{}\n\nfunc (b *Base) ID() int { return 0 }\n")
    _write(tmp_path, "model/order.go", "package model\n\ntype Order struct{ Base }\n")
    _write(tmp_path, "model/other.go", "package model\n\ntype Other struct{}\n\nfunc (o Other) Total() int { return 2 }\n")
    _write(tmp_path, "model/methods.go", "package model\n\nfunc (p Plain) Total() int { return 1 }\n")
    plain = _write(tmp_path, "model/plain.go", PLAIN)
    _write(tmp_path, "svc/svc.go", SVC)
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    # A method promoted from a struct of another file is still reached.
    assert _callers(engine, repo_id, "ID", "Base") == [("PromotedCross", "package")]
    # Plain's own method, defined in another file of its package; not Other's.
    assert _callers(engine, repo_id, "Total", "Plain") == [("UsePlain", "package")]
    assert _callers(engine, repo_id, "Total", "Other") == []
    _equals_fresh(engine, repo_id, tmp_path)

    # Plain comes to embed Other (another file's): any Total of the package.
    plain.write_text(textwrap.dedent(PLAIN).replace("type Plain struct{}", "type Plain struct{ Other }"))
    index_paths(engine, repo_id, tmp_path, {plain})
    assert _callers(engine, repo_id, "Total", "Other") == [("UsePlain", "package")]
    _equals_fresh(engine, repo_id, tmp_path)
