"""A class's impact and callers, against a live Neo4j (audit 11 item 1): a
Class stands with the methods it CONTAINS, so impact_analysis,
impact_analysis_for_diff and find_callers count the callers of its methods
(each naming them in `via`), and impact_analysis its subclasses (EXTENDS) and
theirs; risk_level follows. Fixture code is fictional."""

import subprocess
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


CART = """\
    class Cart:
        discount = {VALUE}

        def __init__(self):
            self.items = []

        def add(self, item):
            self.items.append(item)
            return self.total()

        def total(self):
            return len(self.items) - self.discount
    """

SHOP = """\
    from cart import Cart


    def checkout(c: Cart):
        return c.total()


    def fill(c: Cart):
        c.add(1)
        return c


    def refill(c: Cart):
        return fill(c)


    def count(c: Cart):
        return c.total()


    class GiftCart(Cart):
        pass


    class WrappedGiftCart(GiftCart):
        pass
    """

CONTROLLER = """\
    package shop;

    @RestController
    @RequestMapping("/api/carts")
    public class CartController {
        @GetMapping("/{id}")
        public String show() { return "cart"; }
    }
    """


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


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)


@pytest.fixture(scope="module")
def repo(engine, tmp_path_factory):
    """Cart (whose methods checkout, fill and count call, and refill through
    fill), its subclass GiftCart and theirs, WrappedGiftCart; a Spring
    controller class; the head commit edits Cart's class attribute only."""
    root = tmp_path_factory.mktemp("class-impact")
    repo_id = f"zz-class-impact-{uuid.uuid4().hex[:8]}"
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "dev@example.com")
    _git(root, "config", "user.name", "Example Dev")
    (root / "cart.py").write_text(textwrap.dedent(CART).replace("{VALUE}", "0"))
    (root / "shop.py").write_text(textwrap.dedent(SHOP))
    controller = root / "src" / "main" / "java" / "shop" / "CartController.java"
    controller.parent.mkdir(parents=True)
    controller.write_text(textwrap.dedent(CONTROLLER))
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    (root / "cart.py").write_text(textwrap.dedent(CART).replace("{VALUE}", "1"))
    _git(root, "commit", "-q", "-am", "head")
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root)
    yield repo_id, _Registry([_Repo(repo_id, root)])
    engine.delete_repository(repo_id)


def _rows(envelope):
    return {row["name"]: row.get("via", []) for row in envelope["results"]}


def test_impact_of_a_class_counts_its_method_callers_and_subclasses(engine, repo):
    repo_id, _ = repo
    result = tools.impact_analysis(engine, repo_id, "Cart")
    assert _rows(result["direct_dependents"]) == {
        "checkout": ["Cart.total"], "count": ["Cart.total"], "fill": ["Cart.add"], "GiftCart": [],
    }
    # Further out: a caller of a method's caller, and a subclass's subclass.
    assert _rows(result["transitive_dependents"]) == {"refill": [], "WrappedGiftCart": []}
    assert result["risk_level"] == "medium"


def test_find_callers_of_a_class_lists_the_callers_of_its_methods(engine, repo):
    repo_id, _ = repo
    result = tools.find_callers(engine, repo_id, "Cart")
    # Cart.add calling Cart.total is a call within the class, not one of its callers.
    assert _rows(result) == {"checkout": ["Cart.total"], "count": ["Cart.total"], "fill": ["Cart.add"]}
    # A function's callers carry no via.
    assert _rows(tools.find_callers(engine, repo_id, "fill")) == {"refill": []}


def test_impact_and_find_callers_of_a_controller_class_list_its_routes(engine, repo):
    repo_id, _ = repo
    impact = tools.impact_analysis(engine, repo_id, "CartController")
    assert [e["name"] for e in impact["endpoints"]["results"]] == ["GET /api/carts/{id}"]
    callers = tools.find_callers(engine, repo_id, "CartController")
    assert [e["name"] for e in callers["routed_by"]["results"]] == ["GET /api/carts/{id}"]


def test_impact_analysis_for_diff_of_a_class_body_lists_its_method_callers(engine, repo):
    repo_id, registry = repo
    result = tools.impact_analysis_for_diff(engine, registry, repo_id, "HEAD~1", "HEAD")
    assert result["changed_components"] == ["Cart"]
    assert _rows(result["direct_dependents"]) == {
        "checkout": ["Cart.total"], "count": ["Cart.total"], "fill": ["Cart.add"], "GiftCart": [],
    }
    assert result["risk_level"] == "medium"
