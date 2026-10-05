"""The provider digest (spec §5.2).

SHA-256 over a domain-separated, versioned, length-prefixed encoding:

    DIGEST_DOMAIN || u8 DIGEST_VERSION
      || field(provider name)
      || field(canonical JSON of the declaration set)
      || field(normalised script text, UTF-8)
    field(x) = u64 big-endian byte length || x

The caller passes the declaration set and the script text from one snapshot
read; nothing here reads a file.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from devgraph.sandbox.limits import DIGEST_DOMAIN, DIGEST_VERSION
from devgraph.sandbox.reader import InputError

SHORT_DIGEST_LENGTH = 12


def canonical_json(obj: Any) -> bytes:
    """Sorted keys, no insignificant whitespace, ASCII only, lists in order.

    A non-finite float is refused as `static_reject`.
    """
    try:
        text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except ValueError as exc:
        raise InputError("static_reject", f"declaration is not canonical JSON: {exc}") from None
    return text.encode("ascii")


def _field(data: bytes) -> bytes:
    return len(data).to_bytes(8, "big") + data


def provider_digest(name: str, declaration_set: dict[str, Any], script_text: str) -> str:
    """The lowercase hex SHA-256 digest of one provider (format version 1)."""
    message = (
        DIGEST_DOMAIN
        + bytes([DIGEST_VERSION])
        + _field(name.encode("utf-8"))
        + _field(canonical_json(declaration_set))
        + _field(script_text.encode("utf-8"))
    )
    return hashlib.sha256(message).hexdigest()


def short_digest(digest: str) -> str:
    """The first 12 hex characters, for display only."""
    return digest[:SHORT_DIGEST_LENGTH]
