"""Approval display escaping (spec §5.3, §10.2).

The spans here are given by hand; the end-to-end version with spans from the
scan worker is in the scan tests.
"""

from devgraph.sandbox.display import script_for_review, visible

HOSTILE = ["\x1b", "\x07", "\u202e", "\u200b"]
ESCAPED = ["\\x1b", "\\x07", "\\u202e", "\\u200b"]


def _code_spans(text: str, *literals: str) -> list[tuple[int, int]]:
    """Every span of `text` outside the given string/comment substrings."""
    spans, pos = [], 0
    for literal in literals:
        start = text.index(literal, pos)
        spans.append((pos, start))
        pos = start + len(literal)
    spans.append((pos, len(text)))
    return spans


def test_approval_display_escapes_non_ascii_identifiers():
    text = "ｅｖａｌ(\"naïve\")  # café ✓\nx = 'ü'\n"
    spans = _code_spans(text, '"naïve"', "# café ✓", "'ü'")
    shown = script_for_review(text, spans)
    # Full-width `eval` in code is shown escaped, so it cannot pass for `eval`.
    assert "\\uff45\\uff56\\uff41\\uff4c(" in shown
    assert "ｅ" not in shown
    # Non-ASCII in string literals and comments is shown as text.
    assert '"naïve"' in shown and "# café ✓" in shown and "'ü'" in shown
    # Newlines survive and the rest is unchanged.
    assert shown.endswith("\nx = 'ü'\n")


def test_non_ascii_identifier_escape_covers_latin1_and_astral():
    text = "é = 𝐱\n"
    assert script_for_review(text, [(0, len(text))]) == "\\u00e9 = \\U0001d431\n"


def test_controls_escaped_everywhere_in_script():
    for char, escaped in zip(HOSTILE, ESCAPED, strict=True):
        text = f"x{char} = '{char}'  # {char}\n"
        string_at = text.index("'")
        # In code, in a string and in a comment alike, whatever the spans.
        for spans in ([(0, len(text))], [(0, string_at)], []):
            shown = script_for_review(text, spans)
            assert char not in shown
            assert shown.count(escaped) == 3


def test_tab_and_newline_kept_in_script():
    assert script_for_review("def f():\n\treturn 1\n", [(0, 20)]) == "def f():\n\treturn 1\n"


def test_visible_escapes_controls_in_paths_and_params():
    for char, escaped in zip(HOSTILE, ESCAPED, strict=True):
        assert visible(f"docs/run{char}books/a.md") == f"docs/run{escaped}books/a.md"
        assert visible(f"team-{char}") == f"team-{escaped}"


def test_visible_escape_forms():
    assert visible("a\x00b\x7fc\x85d\x9be") == "a\\x00b\\x7fc\\x85d\\x9be"
    # Single-line fields: newline, CR and tab are controls too.
    assert visible("a\nb\rc\td") == "a\\x0ab\\x0dc\\x09d"
    assert visible("a\u2066b\ufeffc\U000e0041") == "a\\u2066b\\ufeffc\\U000e0041"


def test_visible_keeps_printable_non_ascii():
    assert visible("naïve/日本/✓ é") == "naïve/日本/✓ é"
