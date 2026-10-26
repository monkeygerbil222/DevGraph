"""A C++ typed receiver's owners follow how the calling file names the
type's namespace (audit 11 review, item 2): `using geo::Circle;`, a namespace
alias and a C++17 nested namespace all reach `double geo::Circle::area()`
defined outside `namespace geo`'s block, owned by `geo.Circle`."""

import textwrap

import pytest

from devgraph.indexer.cpp.extractor import extract_cpp_file


def _owners(source):
    result = extract_cpp_file(textwrap.dedent(source), "svc.cpp", "repo")
    return {r.to_owner for r in result.relationships if r.rel_type == "CALLS" and r.to_name == "area"}


@pytest.mark.parametrize("source", [
    '#include "geo/circle.h"\nusing geo::Circle;\ndouble f(Circle& c) { return c.area(); }\n',
    '#include "geo/circle.h"\nnamespace g = geo;\ndouble f(g::Circle& c) { return c.area(); }\n',
    '#include "geo/circle.h"\nnamespace geo::render { double f(Circle& c) { return c.area(); } }\n',
    '#include "geo/circle.h"\nnamespace geo { namespace render { double f(Circle& c) { return c.area(); } } }\n',
    '#include "geo/circle.h"\nusing namespace geo;\ndouble f(Circle& c) { return c.area(); }\n',
], ids=["using-declaration", "namespace-alias", "nested-namespace", "nested-blocks", "using-namespace"])
def test_the_namespaced_owner_is_reached(source):
    assert {"Circle", "geo.Circle"} <= _owners(source)


def test_a_namespace_alias_of_a_nested_namespace_is_spelt_out():
    source = '#include "geo/circle.h"\nnamespace gr = geo::raster;\ndouble f(gr::Circle& c) { return c.area(); }\n'
    assert {"Circle", "geo.raster.Circle", "raster.Circle"} <= _owners(source)


def test_a_class_s_using_of_a_base_member_declares_no_type():
    source = '#include "geo/circle.h"\nclass D : B { using B::area; };\ndouble f(area& c) { return c.area(); }\n'
    assert "B.area" not in _owners(source)
