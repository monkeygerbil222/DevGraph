"""Provider script normalisation (spec §5.3).

The script's bytes become the one `str` that is hashed, shown at approval,
stored and sent to the container, or they are refused as `static_reject`.
Nothing here parses or tokenizes the script.
"""

from __future__ import annotations

import re
import unicodedata

from devgraph.sandbox.reader import InputError

# Refused character categories, beyond tab and LF (which are Cc but allowed).
# Zl/Zp: some renderers break lines at U+2028/U+2029 while Python's tokenizer
# does not. Co/Cn: as the tools-file and glob refusal sets. Cs cannot survive a
# strict UTF-8 decode.
_REFUSED_CATEGORIES = {
    "Cc": "control character",
    "Cf": "format character",
    "Zl": "line separator",
    "Zp": "paragraph separator",
    "Co": "private-use character",
    "Cn": "unassigned character",
}

# PEP 263's cookie pattern, on line 1 or 2. Matched whatever line 1 holds,
# which is stricter than the interpreter and never weaker.
_CODING_COOKIE = re.compile(r"^[ \t\f]*#.*?coding[:=]")


def _reject(rule: str, line: int, column: int, *, byte: bool = False) -> InputError:
    unit = "byte column" if byte else "column"
    return InputError("static_reject", f"script line {line}, {unit} {column}: {rule}")


def normalise_script(raw: bytes) -> str:
    """Return `raw` as strict UTF-8 text with CRLF turned into LF.

    Raises `InputError("static_reject")` for a BOM, invalid UTF-8, a coding
    cookie, a CR not followed by LF, or any control (Cc), format (Cf), line or
    paragraph separator (Zl, Zp), private-use (Co) or unassigned (Cn)
    character other than tab and LF. Such characters can still be written as
    escape sequences inside string literals. The reason gives the rule, the line and
    the column, and names a character only by its code point.
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        raise _reject("UTF-8 BOM", 1, 1)
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        line_start = raw.rfind(b"\n", 0, exc.start) + 1
        line = raw.count(b"\n", 0, exc.start) + 1
        raise _reject("not valid utf-8", line, exc.start - line_start + 1, byte=True) from None

    line, line_start = 1, 0
    for index, char in enumerate(text):
        if char == "\n":
            line, line_start = line + 1, index + 1
            continue
        if char == "\t":
            continue
        column = index - line_start + 1
        if char == "\r":
            if text.startswith("\n", index + 1):
                continue
            raise _reject("CR not followed by LF (U+000D)", line, column)
        kind = _REFUSED_CATEGORIES.get(unicodedata.category(char))
        if kind is not None:
            raise _reject(f"{kind} U+{ord(char):04X}", line, column)

    text = text.replace("\r\n", "\n")
    for number, first_lines in enumerate(text.split("\n", 2)[:2], start=1):
        if _CODING_COOKIE.match(first_lines):
            raise _reject("PEP 263 coding cookie", number, 1)
    return text
