"""Display escaping for repository-sourced text (spec §5.3).

Control (Cc: C0, DEL, C1) and format (Cf) characters are shown as escapes so
that terminal sequences and bidi overrides cannot repaint what the user reads.
Nothing here parses or tokenizes a script: the spans of code outside string
literals and comments come from the static-scan worker.
"""

from __future__ import annotations

import unicodedata


def _escape(char: str) -> str:
    code = ord(char)
    if code < 0x100:
        return f"\\x{code:02x}"
    if code <= 0xFFFF:
        return f"\\u{code:04x}"
    return f"\\U{code:08x}"


def _is_hidden(char: str) -> bool:
    return unicodedata.category(char) in ("Cc", "Cf")


def visible(s: str) -> str:
    """`s` with every control and format character escaped (`\\x..` below
    U+0100, else `\\u....`, or `\\U........` above U+FFFF).

    For one-line fields such as paths, params and labels: tab, CR and LF are
    controls and are escaped too.
    """
    return "".join(_escape(c) if _is_hidden(c) else c for c in s)


def script_for_review(text: str, non_ascii_code_spans: list[tuple[int, int]]) -> str:
    """Script text for the approval prompt.

    As `visible()`, but tab and LF are kept so the script reads as code, and
    every non-ASCII character inside a span is escaped as `\\u....` (or
    `\\U........`), so an identifier such as full-width `eval` is visibly odd.
    Spans are half-open `[start, end)` offsets into `text`, covering the code
    outside string literals and comments.
    """
    in_code = bytearray(len(text))
    for start, end in non_ascii_code_spans:
        in_code[start:end] = b"\x01" * len(in_code[start:end])
    out = []
    for index, char in enumerate(text):
        if char in "\t\n":
            out.append(char)
        elif _is_hidden(char):
            out.append(_escape(char))
        elif in_code[index] and ord(char) > 0x7F:
            out.append(f"\\u{ord(char):04x}" if ord(char) <= 0xFFFF else f"\\U{ord(char):08x}")
        else:
            out.append(char)
    return "".join(out)
