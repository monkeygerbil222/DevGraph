"""UTF-16 and UTF-32 source (Visual Studio's "Unicode" save, PowerShell 5's
`Out-File`) is decoded and extracted, not skipped as binary for its NUL bytes
(audit 11 item 6); `get_source` reads it as the indexer does, so the stored
line range returns the definition."""

import codecs
import os
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan
from devgraph.indexer.source_text import decode_source, decode_wide
from devgraph.indexer.walk import BINARY, content_skip_reason
from devgraph.mcp.tools import get_source
from devgraph.registry.store import RepoRegistry

CS = "namespace N {\r\n  public class Win {\r\n    public void Run() {\r\n      Helper();\r\n    }\r\n    void Helper() {}\r\n  }\r\n}\r\n"


@pytest.mark.parametrize("codec", ["utf-16", "utf-16-le", "utf-16-be", "utf-32"])
def test_wide_text_is_decoded_with_or_without_a_mark(codec):
    data = CS.encode(codec)
    assert decode_source(data) == CS
    assert decode_source(data, python=True) == CS
    assert decode_wide(data)[0] == CS


def test_bom_led_wide_text_drops_its_mark():
    assert decode_wide(codecs.BOM_UTF16_LE + "x = 1\n".encode("utf-16-le")) == ("x = 1\n", "utf-16")


def test_binary_and_plain_text_are_not_taken_for_wide_text():
    assert decode_wide(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x01\x00") is None
    assert decode_wide(bytes(range(256)) * 4) is None
    assert decode_wide(b"x = 1\n") is None
    assert decode_wide(b"\x00\x01") is None  # two bytes of binary, not U+0001
    assert decode_wide(b"\x01\x00\x02\x00ab") is None
    assert decode_wide("a\0b".encode("utf-16")) is None  # text holding a NUL is no source


def test_a_utf16_file_is_not_binary_but_a_binary_one_still_is(tmp_path):
    (tmp_path / "Win.cs").write_bytes(CS.encode("utf-16"))
    (tmp_path / "blob.cs").write_bytes(os.urandom(64) + b"\x00\x01\x02")
    assert content_skip_reason(tmp_path / "Win.cs", 1 << 20) is None
    assert content_skip_reason(tmp_path / "blob.cs", 1 << 20) == BINARY


def test_a_utf16_file_s_lines_are_judged_as_utf8(tmp_path):
    lines = "".join(f"x{i} = '{'a' * 140}'\n" for i in range(40))  # about 300 bytes a line in UTF-16, 150 in UTF-8
    (tmp_path / "gen.py").write_bytes(lines.encode("utf-16"))
    assert content_skip_reason(tmp_path / "gen.py", 1 << 20) is None


def test_a_utf16_csharp_file_is_indexed_and_get_source_returns_its_methods(tmp_path):
    engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        engine.verify_connectivity()
    except Exception as e:
        engine.close()
        pytest.skip(f"Neo4j not available: {e}")
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "Win.cs").write_bytes(CS.encode("utf-16"))
    (root / "Le.cs").write_bytes(CS.replace("Win", "Plain").encode("utf-16-le"))
    registry = RepoRegistry(tmp_path / "registry.sqlite3")
    repo_id = f"zz-utf16-{uuid.uuid4().hex[:8]}"
    try:
        registry.add_repo(root, repo_id=repo_id)
        provision_repository_schema(engine, root)
        engine.upsert_repository(repo_id, repo_id, str(root))
        full_scan(engine, repo_id, root)
        rows = engine.run_cypher(
            "MATCH (n {repo_id: $r}) WHERE n:Class OR n:Function "
            "RETURN labels(n)[0] AS l, coalesce(n.owner, '') AS o, n.name AS n, n.file AS f ORDER BY f, n",
            {"r": repo_id},
        )
        assert [(r["l"], r["o"], r["n"], r["f"]) for r in rows] == [
            ("Function", "Plain", "Helper", "Le.cs"), ("Class", "", "Plain", "Le.cs"),
            ("Function", "Plain", "Run", "Le.cs"),
            ("Function", "Win", "Helper", "Win.cs"), ("Function", "Win", "Run", "Win.cs"),
            ("Class", "", "Win", "Win.cs"),
        ]
        calls = engine.run_cypher(
            "MATCH (a:Function {repo_id: $r, name: 'Run', file: 'Win.cs'})-[:CALLS]->(b) RETURN b.name AS n",
            {"r": repo_id},
        )
        assert [c["n"] for c in calls] == ["Helper"]
        source = get_source(engine, registry, repo_id, "Run", file="Win.cs")
        assert source["source"] == "    public void Run() {\n      Helper();\n    }"
        assert source["start_line"] == 3
    finally:
        engine.delete_repository(repo_id)
        registry.close()
        engine.close()


@pytest.mark.parametrize("codec", ["utf-16-le", "utf-16-be"])
def test_unmarked_utf16_with_a_nul_low_byte_character_is_still_text(codec):
    # U+4E00 is 00 4E in UTF-16 LE: a NUL on the parity ASCII text leaves NUL-free.
    text = "".join(f"// 一{i} 中文 説明\nint f{i}() {{ return {i}; }}\n" for i in range(20))
    assert decode_wide(text.encode(codec)) == (text, codec)

