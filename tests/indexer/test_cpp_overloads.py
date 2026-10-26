"""C++ overloads in one scope are one node, the first definition, holding
every overload's calls, as README says of two definitions of one name
(audit 11 item 5, C++): the node kept the last one's lines before."""

from devgraph.indexer.cpp.extractor import extract_cpp_file

SOURCE = """\
class D {
  int f() { return one(); }
  int f(int x) { return two(); }
  int one() { return 1; }
  int two() { return 2; }
};
int g() { return 1; }
int g(int x) { return x; }
"""


def test_overloads_are_one_node_at_the_first_definition_with_both_bodies_calls():
    result = extract_cpp_file(SOURCE, "d.cpp", "repo")
    fs = [n for n in result.nodes if n.label == "Function" and n.name == "f"]
    assert [(n.properties["start_line"], n.properties["end_line"]) for n in fs] == [(2, 2)]
    gs = [n for n in result.nodes if n.label == "Function" and n.name == "g"]
    assert [n.properties["start_line"] for n in gs] == [7]
    called = {r.to_name for r in result.relationships if r.rel_type == "CALLS" and r.from_name == "f"}
    assert called == {"one", "two"}
