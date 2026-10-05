"""Script normalisation and the reject set (spec §5.3, §10.2)."""

import pytest

from devgraph.sandbox.reader import InputError
from devgraph.sandbox.source import normalise_script

REJECTS = [
    pytest.param(b"x = '\xff'\n", "utf-8", id="not-strict-utf8"),
    pytest.param(b"x = '\xed\xa0\x80'\n", "utf-8", id="encoded-surrogate"),
    pytest.param(b"\xef\xbb\xbfx = 1\n", "BOM", id="bom"),
    pytest.param(b"# -*- coding: latin-1 -*-\nx = 1\n", "coding cookie", id="cookie-line1"),
    pytest.param(b"#!/usr/bin/env python\n# vim: set fileencoding=utf-8 :\n", "coding cookie", id="cookie-line2"),
    pytest.param(b"x = 1\x00\n", "U+0000", id="nul"),
    pytest.param(b"x = 1\ry = 2\n", "CR not followed by LF", id="lone-cr"),
    pytest.param(b"x = 1\r", "CR not followed by LF", id="trailing-cr"),
    pytest.param(b"x = 1\n\x0cy = 2\n", "U+000C", id="form-feed"),
    pytest.param(b"x = '\x1b[2J'\n", "U+001B", id="esc"),
    pytest.param(b"x = '\x07'\n", "U+0007", id="bel"),
    pytest.param(b"x = '\x0b'\n", "U+000B", id="vertical-tab"),
    pytest.param(b"x = '\x7f'\n", "U+007F", id="del"),
    pytest.param("x = '\u0085'\n".encode(), "U+0085", id="c1-nel"),
    pytest.param("x = '\u009b'\n".encode(), "U+009B", id="c1-csi"),
    pytest.param("x = 'a‮b'\n".encode(), "U+202E", id="bidi-override-rlo"),
    pytest.param("x = 'a‪b'\n".encode(), "U+202A", id="bidi-embedding-lre"),
    pytest.param("x = 'a⁦b'\n".encode(), "U+2066", id="bidi-isolate-lri"),
    pytest.param("x = 'a⁩b'\n".encode(), "U+2069", id="bidi-isolate-pdi"),
    pytest.param("x = 'a‎b'\n".encode(), "U+200E", id="lrm"),
    pytest.param("x = 'a‏b'\n".encode(), "U+200F", id="rlm"),
    pytest.param("x = 'a​b'\n".encode(), "U+200B", id="zero-width-space"),
    pytest.param("x = 'a‍b'\n".encode(), "U+200D", id="zero-width-joiner"),
    pytest.param("x = 1\n# a﻿b\n".encode(), "U+FEFF", id="zwnbsp-mid-file"),
    pytest.param("x = 'a\U000e0041b'\n".encode(), "U+E0041", id="astral-tag-char"),
    pytest.param("x = 1\u2028y = 2\n".encode(), "U+2028", id="line-separator"),
    pytest.param("x = 'a\u2029b'\n".encode(), "U+2029", id="paragraph-separator"),
    pytest.param("x = 'a\ue000b'\n".encode(), "U+E000", id="private-use"),
    pytest.param("# a\U000f0000b\n".encode(), "U+F0000", id="private-use-astral"),
    pytest.param("x = 'a\u0378b'\n".encode(), "U+0378", id="unassigned"),
]


@pytest.mark.parametrize(("raw", "fragment"), REJECTS)
def test_normalisation_rejection_table(raw, fragment):
    with pytest.raises(InputError) as info:
        normalise_script(raw)
    assert info.value.code == "static_reject"
    assert fragment in info.value.reason
    assert "line " in info.value.reason and "column " in info.value.reason


def test_crlf_becomes_lf():
    assert normalise_script(b"x = 1\r\ny = 2\r\n") == "x = 1\ny = 2\n"
    assert normalise_script(b"x = 1\r\ny = 2\r\n") == normalise_script(b"x = 1\ny = 2\n")


def test_tab_and_non_ascii_in_string_are_kept():
    raw = "def derive(ctx):\n\treturn 'naïve — ✓'\n".encode()
    assert normalise_script(raw) == "def derive(ctx):\n\treturn 'naïve — ✓'\n"


def test_reason_locates_without_quoting_the_character():
    with pytest.raises(InputError) as info:
        normalise_script("a = 1\nbb = '‮'\n".encode())
    reason = info.value.reason
    assert "line 2, column 7" in reason
    assert "‮" not in reason


def test_reason_locates_invalid_utf8_by_byte():
    with pytest.raises(InputError) as info:
        normalise_script(b"a = 1\nbb = '\xff'\n")
    assert "line 2, byte column 7" in info.value.reason
    assert "\\xff" not in info.value.reason


def test_cookie_beyond_line_two_is_not_a_cookie():
    raw = b"x = 1\ny = 2\n# coding: latin-1\n"
    assert normalise_script(raw) == raw.decode()


def test_separators_as_escape_sequences_are_kept():
    raw = b"x = '\\u2028\\u2029\\ue000'\n"
    assert normalise_script(raw) == "x = '\\u2028\\u2029\\ue000'\n"
