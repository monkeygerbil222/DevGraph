"""A dependent's `via` is capped, against a live Neo4j (audit 11 review
F5): a caller of many of a class's methods names the first five in `via`
and how many there are in `via_count`, in impact_analysis (one target or
several files' traced apart) and find_callers. Fixture code is
fictional."""

import textwrap
import uuid

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


METHODS = [f"step{i}" for i in range(1, 8)]
CART = "class Cart:\n" + "".join(f"    def {m}(self):\n        return {i}\n\n" for i, m in enumerate(METHODS))
USER = "from shop.cart import Cart\n\n\ndef run(c: Cart):\n    return " + " + ".join(f"c.{m}()" for m in METHODS) + "\n"
FIRST = ["Cart.step1", "Cart.step2", "Cart.step3", "Cart.step4", "Cart.step5"]


@pytest.fixture(scope="module")
def repo(engine, tmp_path_factory):
    root = tmp_path_factory.mktemp("via-cap")
    repo_id = f"zz-via-cap-{uuid.uuid4().hex[:8]}"
    for rel, text in {"shop/__init__.py": "", "shop/cart.py": CART, "shop/user.py": USER,
                      "legacy/cart.py": CART}.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root)
    yield repo_id
    engine.delete_repository(repo_id)


def _run(envelope):
    (row,) = [row for row in envelope["results"] if row["name"] == "run"]
    return row["via"], row.get("via_count")


def test_impact_analysis_caps_via(engine, repo):
    assert _run(tools.impact_analysis(engine, repo, "Cart", file="shop/cart.py")["direct_dependents"]) == (FIRST, 7)
    # Two files' Cart traced apart: the merged row is capped too.
    assert _run(tools.impact_analysis(engine, repo, "Cart")["direct_dependents"]) == (FIRST, 7)


def test_find_callers_caps_via(engine, repo):
    assert _run(tools.find_callers(engine, repo, "Cart", file="shop/cart.py")) == (FIRST, 7)
