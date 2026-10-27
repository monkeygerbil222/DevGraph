"""A docs note's title: its `title:` front-matter field, else its first `#`
heading, else its id. Fixture data is fictional."""

from devgraph.indexer.docs.extractor import DocsExtractor


def _title(text: str) -> str:
    (doc,) = DocsExtractor("demo").extract_from_source(text, "docs/adr-1.md").docs
    return doc.properties["title"]


def test_frontmatter_title_used():
    assert _title("---\ntype: design_decision\nid: ADR-001\ntitle: Use Redis for sessions\n---\n# Heading\nBody\n") == (
        "Use Redis for sessions"
    )


def test_heading_then_id_when_no_frontmatter_title():
    assert _title("---\ntype: requirement\nid: REQ-7\n---\n# Sessions survive restarts\n") == "Sessions survive restarts"
    assert _title("---\ntype: requirement\nid: REQ-7\ntitle:\n---\nNo heading here.\n") == "REQ-7"
