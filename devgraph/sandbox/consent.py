"""Consent for custom provider scripts (spec §5.4).

Approving a script and enabling scripts for a repository need a person at a
terminal: both stdin and stdout must be a TTY (`require_tty`). The only
non-interactive path is `approve --sha256`, which must equal the current
digest exactly (`digest_matches`). There is no `--yes`.

`review_lines` builds the approval prompt. Every repository-sourced string in it
goes through `display.visible` or `display.script_for_review`; the lines are
plain text, **not Rich markup**, so the CLI prints them as `rich.text.Text`.
No line here, nor anywhere else DevGraph prints, is an approve command
containing a digest.

The CLI reaches `require_tty` and `current_platform` through this module's
attributes, so tests can patch them.
"""

from __future__ import annotations

import difflib
import hmac
import json
import re
import sys
from datetime import datetime, timezone
from typing import TextIO

from rich.cells import cell_len

from devgraph.sandbox.display import script_for_review, visible
from devgraph.sandbox.limits import GROWTH_FACTOR
from devgraph.sandbox.snapshot import ProviderSnapshot
from devgraph.sandbox.trust import Approval

_HEX_DIGEST = re.compile(r"[0-9a-fA-F]{64}")

#: The marker on a review line wider than the terminal (it wraps, so nothing hides off to the right).
WIDE_MARKER = "!"


class ConsentError(Exception):
    """Consent cannot be asked for here (no terminal)."""


def current_platform() -> str:
    return sys.platform


def require_tty(stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    """Raise `ConsentError` unless both stdin and stdout are terminals (default: the
    current `sys.stdin`/`sys.stdout`, looked up at call time)."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    for name, stream in (("stdin", stdin), ("stdout", stdout)):
        try:
            tty = stream.isatty()
        except (AttributeError, ValueError, OSError):
            tty = False
        if not tty:
            raise ConsentError(f"{name} is not a terminal")


def digest_matches(given: str, current: str) -> bool:
    """`given` is exactly 64 hex characters (either case) equal to `current`."""
    if not isinstance(given, str) or not _HEX_DIGEST.fullmatch(given):
        return False
    return hmac.compare_digest(given.lower().encode("ascii"), current.lower().encode("ascii"))


def utc_time(iso: str) -> str:
    """A stored ISO-8601 timestamp as `YYYY-MM-DD HH:MM:SS UTC` (as given, escaped, if unparsable)."""
    try:
        moment = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return visible(str(iso))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def pretty_declaration(declaration_json: str) -> str:
    """Canonical declaration JSON, pretty-printed (sorted keys, ASCII only)."""
    return json.dumps(json.loads(declaration_json), indent=2, sort_keys=True, ensure_ascii=True)


def grew(matched_count: int, approved_count: int) -> bool:
    """The matched-file count is above `GROWTH_FACTOR` times the approved one (§5.6)."""
    return matched_count > GROWTH_FACTOR * approved_count


def _diff(old: str, new: str) -> list[str]:
    return list(difflib.unified_diff(old.splitlines(), new.splitlines(), "approved", "current", lineterm=""))


def _flag(lines: list[str], width: int, indent: str = "  ") -> list[str]:
    """Indent each line, marking those wider than `width` (they wrap when printed)."""
    out = []
    for line in lines:
        prefix = f"{WIDE_MARKER} " if cell_len(indent + line) > width else indent
        out.append(prefix + line)
    return out


def review_lines(
    repo_id: str, canon: str, snap: ProviderSnapshot, previous: Approval | None, *, width: int
) -> list[str]:
    """The approval prompt (§5.4), as plain-text lines with every repository-sourced
    string escaped. `previous` is the provider's most recent approval, if any: the
    declaration and the script are then shown as unified diffs against it."""
    script_path = f".devgraph/providers/{snap.name}.py"
    globs = ", ".join(visible(g) for g in snap.declaration_set["provider"]["inputs"])
    lines = [
        f"Repository: {visible(repo_id)}",
        f"Canonical path: {visible(canon)}",
        f"Provider: {visible(snap.name)}",
        f"Script: {script_path} ({len(snap.script_text.encode('utf-8'))} bytes)",
        f"Inputs: {globs}",
    ]
    figures = f"  {snap.matched_count} matched file(s)"
    if previous is not None:
        figures += f" (approved with {previous.matched_count})"
        if grew(snap.matched_count, previous.matched_count):
            figures += f"; grew more than {GROWTH_FACTOR}x"
    lines += [
        figures,
        f"  {snap.denied} excluded by the secret-name denylist; {snap.total_bytes} input bytes",
        f"  sample (first {len(snap.sample)}):",
        *(f"    {visible(path)}" for path in snap.sample),
    ]

    declaration = pretty_declaration(snap.declaration_json)
    if previous is None:
        lines.append("Declaration set:")
        body = declaration.splitlines()
    else:
        old = pretty_declaration(previous.declaration_json)
        lines.append(f"Declaration changes since the approval at {utc_time(previous.approved_at)}:")
        body = _diff(old, declaration) or ["(unchanged)"]
    lines += _flag([visible(line) for line in body], width)

    if snap.findings:
        lines.append("Static scan findings:")
        lines += [f"  line {f.line}: {f.rule}: {visible(f.message)}" for f in snap.findings]
    else:
        lines.append("Static scan: no findings")

    if previous is None:
        lines.append(f"Script ({script_path}):")
        shown = script_for_review(snap.script_text, list(snap.literal_spans)).split("\n")
        if shown and shown[-1] == "":
            shown.pop()
        numbered = [f"{n:>4} | {line}" for n, line in enumerate(shown, 1)]
    else:
        lines.append(f"Script changes since the approval at {utc_time(previous.approved_at)}:")
        # Diff lines carry no literal spans, so every non-ASCII character in them is escaped.
        numbered = [script_for_review(line, []) for line in _diff(previous.script_text, snap.script_text)]
        numbered = numbered or ["(unchanged)"]
    flagged = _flag(numbered, width)
    lines += flagged
    if any(line.startswith(WIDE_MARKER) for line in lines):
        lines.append(f"Lines marked {WIDE_MARKER} are wider than the terminal and wrap onto the next rows.")
    return lines
