"""Approval display escaping (spec §5.3, §10.2).

The literal spans here are given by hand; the end-to-end version with spans
from the scan worker is in the scan tests.
"""

import pytest

from devgraph.sandbox.display import script_for_review, visible
from devgraph.sandbox.reader import InputError

HOSTILE = ["\x1b", "\x07", "\u202e", "\u200b", "\u2028", "\u2029"]
ESCAPED = ["\\x1b", "\\x07", "\\u202e", "\\u200b", "\\u2028", "\\u2029"]


def _literal_spans(text: str, *literals: str) -> list[tuple[int, int]]:
    """The spans of the given string/comment substrings, in order."""
    spans, pos = [], 0
    for literal in literals:
        start = text.index(literal, pos)
        pos = start + len(literal)
        spans.append((start, pos))
    return spans


def test_approval_display_escapes_non_ascii_identifiers():
    text = "ｅｖａｌ(\"naïve\")  # café ✓\nx = 'ü'\n"
    spans = _literal_spans(text, '"naïve"', "# café ✓", "'ü'")
    shown = script_for_review(text, spans)
    # Full-width `eval` in code is shown escaped, so it cannot pass for `eval`.
    assert "\\uff45\\uff56\\uff41\\uff4c(" in shown
    assert "ｅ" not in shown
    # Non-ASCII in string literals and comments is shown as text.
    assert '"naïve"' in shown and "# café ✓" in shown and "'ü'" in shown
    # Newlines survive and the rest is unchanged.
    assert shown.endswith("\nx = 'ü'\n")


def test_missing_span_means_escaped():
    assert script_for_review("x = 'é'  # ü\n", []) == "x = '\\u00e9'  # \\u00fc\n"


def test_span_end_is_exclusive():
    # The last character of a span is shown as text; the one at `end` is code.
    assert script_for_review("éé", [(0, 1)]) == "é\\u00e9"
    assert script_for_review("aéé", [(1, 2)]) == "aé\\u00e9"


def test_non_ascii_identifier_escape_covers_latin1_and_astral():
    assert script_for_review("é = 𝐱\n", []) == "\\u00e9 = \\U0001d431\n"


@pytest.mark.parametrize(
    "spans",
    [
        [(-1, 2)],
        [(0, 6)],
        [(3, 2)],
        [(0, 3), (2, 4)],
        [(3, 4), (0, 1)],
    ],
    ids=["negative", "past-end", "reversed", "overlapping", "unsorted"],
)
def test_bad_spans_are_static_reject(spans):
    with pytest.raises(InputError) as info:
        script_for_review("abcde", spans)
    assert info.value.code == "static_reject"


def test_adjacent_and_empty_spans_are_accepted():
    assert script_for_review("éé", [(0, 1), (1, 2), (2, 2)]) == "éé"


def test_controls_escaped_everywhere_in_script():
    for char, escaped in zip(HOSTILE, ESCAPED, strict=True):
        text = f"x{char} = '{char}'  # {char}\n"
        string_at = text.index("'")
        # In code, in a string and in a comment alike, whatever the spans.
        for spans in ([(0, len(text))], [(string_at, len(text))], []):
            shown = script_for_review(text, spans)
            assert char not in shown
            assert shown.count(escaped) == 3


def test_lone_cr_is_escaped_in_script():
    assert script_for_review("a\rb", []) == "a\\x0db"


def test_tab_and_newline_kept_in_script():
    assert script_for_review("def f():\n\treturn 1\n", []) == "def f():\n\treturn 1\n"


def test_visible_escapes_controls_in_paths_and_params():
    for char, escaped in zip(HOSTILE, ESCAPED, strict=True):
        assert visible(f"docs/run{char}books/a.md") == f"docs/run{escaped}books/a.md"
        assert visible(f"team-{char}") == f"team-{escaped}"


def test_visible_escape_forms():
    assert visible("a\x00b\x7fc\x85d\x9be") == "a\\x00b\\x7fc\\x85d\\x9be"
    # Single-line fields: newline, CR and tab are controls too.
    assert visible("a\nb\rc\td") == "a\\x0ab\\x0dc\\x09d"
    assert visible("a\u2066b\ufeffc\U000e0041") == "a\\u2066b\\ufeffc\\U000e0041"
    # Private-use, unassigned and lone surrogates (a surrogateescape path).
    assert visible("a\ue000b\u0378c\udc80") == "a\\ue000b\\u0378c\\udc80"


def test_visible_keeps_printable_non_ascii():
    assert visible("naïve/日本/✓ é") == "naïve/日本/✓ é"
