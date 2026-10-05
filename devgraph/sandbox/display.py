"""Display escaping for repository-sourced text (spec §5.3).

Control (Cc: C0, DEL, C1), format (Cf), line and paragraph separator (Zl, Zp),
surrogate (Cs), private-use (Co) and unassigned (Cn) characters are shown as
escapes, so terminal sequences, bidi overrides and line breaks a renderer
invents cannot make what the user reads differ from what runs.

Output is plain text and **not safe as Rich markup**: `[` and `]` pass through.
Callers printing through Rich apply `rich.markup.escape`, or print with
`markup=False` or as a `rich.text.Text`.

Nothing here parses or tokenizes a script. The literal spans for
`script_for_review` come from the static-scan worker (Task 5), under this
contract:

- A span is a half-open `[start, end)` pair of **code point** offsets into the
  normalised script `str`. Spans are sorted and do not overlap; a span outside
  `0 <= start <= end <= len(text)`, or out of order, is `static_reject`.
- A span marks a **string literal or comment**. Everything else is code, so a
  missing span fails closed: its non-ASCII characters are escaped.
- Offsets are built from `tokenize` (row, col) positions, whose columns are
  code points. Line starts come only from the positions of `"\\n"` in the text,
  never from `str.splitlines()`, which also breaks at other characters.
- Never use `ast` `col_offset`, which counts UTF-8 bytes.
- Ranges come from STRING, FSTRING_MIDDLE and COMMENT tokens. Take care around
  `{{` and `}}` in f-strings: an FSTRING_MIDDLE token's position can skip a
  source character there, so derive its range from the source, not from the
  token's string length.
"""

from __future__ import annotations

import unicodedata

from devgraph.sandbox.reader import InputError

_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"})


def _escape(char: str) -> str:
    code = ord(char)
    if code < 0x100:
        return f"\\x{code:02x}"
    if code <= 0xFFFF:
        return f"\\u{code:04x}"
    return f"\\U{code:08x}"


def _escape_non_ascii(char: str) -> str:
    code = ord(char)
    return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"


def _is_hidden(char: str) -> bool:
    return unicodedata.category(char) in _HIDDEN_CATEGORIES


def visible(s: str) -> str:
    """`s` with every hidden character escaped (`\\x..` below U+0100, else
    `\\u....`, or `\\U........` above U+FFFF).

    For one-line fields such as paths, params and labels: tab, CR and LF are
    controls and are escaped too. Not safe as Rich markup (see the module
    docstring).
    """
    return "".join(_escape(c) if _is_hidden(c) else c for c in s)


def script_for_review(text: str, literal_spans: list[tuple[int, int]]) -> str:
    """Script text for the approval prompt.

    As `visible()`, but tab and LF are kept so the script reads as code, and
    every non-ASCII character **outside** the literal spans is escaped as
    `\\u....` (or `\\U........`), so an identifier such as full-width `eval` is
    visibly odd. `literal_spans` mark string literals and comments under the
    contract in the module docstring; invalid spans are `static_reject`. Not
    safe as Rich markup.
    """
    in_literal = bytearray(len(text))
    previous_end = 0
    for start, end in literal_spans:
        if not previous_end <= start <= end <= len(text):
            raise InputError("static_reject", f"display span ({start}, {end}) is invalid")
        in_literal[start:end] = b"\x01" * (end - start)
        previous_end = end
    out = []
    for index, char in enumerate(text):
        if char in "\t\n":
            out.append(char)
        elif _is_hidden(char):
            out.append(_escape(char))
        elif ord(char) > 0x7F and not in_literal[index]:
            out.append(_escape_non_ascii(char))
        else:
            out.append(char)
    return "".join(out)
