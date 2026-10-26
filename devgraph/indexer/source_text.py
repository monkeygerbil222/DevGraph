"""Decoding a source file's bytes for extraction.

Every extractor works on text, and a byte it can't decode must not cost an
identifier: `def café()` in a Latin-1 file read as UTF-8 with replacement
characters became `caf`. The order, for every file:

0. UTF-16 or UTF-32 (`decode_wide`): a file its byte-order mark names as
   one (Visual Studio's "Unicode" save, PowerShell 5's `Out-File`), or one
   whose NUL bytes fall on every other byte as UTF-16 text's do.
1. Python only: the PEP 263 coding declaration on line 1 or 2, when it names
   a codec that decodes the file.
2. UTF-8.
3. cp1252 (Windows "ANSI", the commonest undeclared non-UTF-8 source).
4. Latin-1, which decodes any byte, so nothing is ever replaced.
"""

from __future__ import annotations

import codecs
import io
import tokenize
from pathlib import Path

_PYTHON_SUFFIXES = (".py", ".pyi", ".pyw")

#: Byte-order marks, UTF-32's first: its little-endian mark begins with UTF-16's.
_WIDE_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"),
)
#: How much of a file without a mark is looked at to tell UTF-16 text.
_WIDE_SNIFF_BYTES = 8192
#: The control characters text without a mark may hold.
_WHITESPACE = frozenset("\t\n\v\f\r")


def decode_source(data: bytes, python: bool = False) -> str:
    """`data` as text, by the rules in the module docstring."""
    return decode_source_as(data, python)[0]


def decode_source_as(data: bytes, python: bool = False) -> tuple[str, str]:
    """`decode_source`, with the codec that decoded it ("utf-8" for valid UTF-8)."""
    wide = decode_wide(data)
    if wide is not None:
        return wide
    if python:
        declared = declared_encoding(data)
        if declared is not None:
            try:
                return data.decode(declared), declared
            except (LookupError, UnicodeDecodeError):
                pass
    for encoding in ("utf-8", "cp1252"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1"), "latin-1"


def is_python_path(path: str | Path) -> bool:
    """Whether a path's suffix gets the PEP 263 rule."""
    return Path(path).suffix in _PYTHON_SUFFIXES


def read_source(path: Path) -> str:
    """The file's text for extraction (see `decode_source`). Raises OSError
    when it can't be read."""
    return decode_source(Path(path).read_bytes(), python=is_python_path(path))


def declared_encoding(data: bytes) -> str | None:
    """The codec a PEP 263 declaration names, or None without one (a UTF-8
    BOM or no declaration: UTF-8 is tried anyway). `tokenize` applies the
    PEP's rules: line 1, or line 2 after a comment or blank line."""
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    except SyntaxError:  # an unknown codec, or one contradicting a BOM
        return None
    return None if encoding in ("utf-8", "utf-8-sig") else encoding


def decode_wide(data: bytes) -> tuple[str, str] | None:
    """`data` as text with its codec when it is UTF-16 or UTF-32, else None.

    A UTF-16 or UTF-32 byte-order mark names the codec (the mark itself is
    not part of the text). Without one, `data` is UTF-16 when, in its first
    8 KiB, every other byte is NUL in at least 70% of places and none of the
    rest is (ASCII-range source in UTF-16 LE or BE), and the text holds no
    control character but whitespace. Either way it must decode strictly,
    to text holding no NUL, or it is not taken for one.
    """
    codec = next((name for bom, name in _WIDE_BOMS if data.startswith(bom)), None)
    marked = codec is not None
    if codec is None and len(data) >= 2 and len(data) % 2 == 0:
        sample = data[:_WIDE_SNIFF_BYTES]
        even, odd = sample[0::2], sample[1::2]
        if b"\0" not in even and odd.count(0) * 10 >= len(odd) * 7:
            codec = "utf-16-le"
        elif b"\0" not in odd and even.count(0) * 10 >= len(even) * 7:
            codec = "utf-16-be"
    if codec is None:
        return None
    try:
        text = data.decode(codec)
    except UnicodeDecodeError:
        return None
    if "\0" in text or (not marked and any(c < " " and c not in _WHITESPACE for c in text[:_WIDE_SNIFF_BYTES])):
        return None
    return text, codec
