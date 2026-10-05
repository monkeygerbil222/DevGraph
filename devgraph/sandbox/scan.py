"""The host-side static scan (spec §4.1, §4.2).

The scan is hygiene, not a boundary: it gives readable findings at approval,
and any finding blocks approval. It parses untrusted source, so it runs in a
separate `python -I -S` worker (`scan_worker.py`, sent as `-c` text) with a
constructed environment, `RLIMIT_AS`, `RLIMIT_CPU`, no core file and a wall
timeout. The limits are set by a prologue inside the worker, before it reads
the script. Any crash, signal, timeout, non-zero exit, oversize or malformed
output, or exception the worker reports (SyntaxError, RecursionError,
MemoryError included) is `static_reject`, never a pass.

The worker parses and tokenizes the script as data; nothing here or there
compiles it to a code object or runs it.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from importlib import resources

from devgraph.sandbox.limits import (
    DENIED_NAMES,
    FIXED_PATH,
    INPUT_ALLOWLIST_MODULES,
    SCAN_FEATURE_VERSION,
    SCAN_OUTPUT_MAX_BYTES,
    SCAN_RLIMIT_AS_BYTES,
    SCAN_RLIMIT_CPU_SECONDS,
    SCAN_WALL_SECONDS,
)
from devgraph.sandbox.reader import InputError

WORKER_SOURCE = (
    resources.files("devgraph.sandbox")
    .joinpath("scan_worker.py")
    .read_text(encoding="utf-8")
)

# `LC_ALL=C`, not `C.UTF-8`: loading any named locale makes glibc map the whole
# locale archive (222 MiB on Fedora), which leaves the interpreter unable to start
# under `RLIMIT_AS` 256 MiB. In the C locale Python turns on UTF-8 mode itself, and
# the worker decodes stdin as UTF-8 and writes ASCII JSON regardless.
WORKER_ENV = {"PATH": FIXED_PATH, "LC_ALL": "C"}

_RULE = re.compile(r"[a-z_]{1,32}")
_EXCEPTION_NAME = re.compile(r"[A-Za-z]{1,64}")
_MESSAGE_MAX = 1000


@dataclass(frozen=True)
class Finding:
    rule: str
    line: int
    message: str


@dataclass(frozen=True)
class ScanResult:
    findings: tuple[Finding, ...]
    literal_spans: tuple[tuple[int, int], ...]


def _reject(reason: str) -> InputError:
    return InputError("static_reject", reason)


def _limits_prologue(memory_limit: int) -> str:
    """Source run first in the worker, before it reads the script: sets the
    limits (soft and hard) from inside the child. No `preexec_fn`, which is
    unsafe to fork with from a threaded parent such as the dashboard. Output goes
    to a file, so `RLIMIT_FSIZE` stops a runaway writer at the cap."""
    limits = [
        ("RLIMIT_AS", memory_limit),
        ("RLIMIT_CPU", SCAN_RLIMIT_CPU_SECONDS),
        ("RLIMIT_CORE", 0),
        ("RLIMIT_FSIZE", SCAN_OUTPUT_MAX_BYTES + 1),
    ]
    lines = ["import resource"]
    lines += [
        f"resource.setrlimit(resource.{name}, ({int(value)}, {int(value)}))"
        for name, value in limits
    ]
    return "\n".join(lines) + "\ndel resource\n"


def _run_worker(
    request: bytes, *, timeout: float, memory_limit: int, worker_source: str
) -> bytes:
    # Files rather than pipes: no deadlock between writing stdin and reading stdout.
    with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as stdout:
        stdin.write(request)
        stdin.seek(0)
        try:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    _limits_prologue(memory_limit) + worker_source,
                ],
                stdin=stdin,
                stdout=stdout,
                stderr=subprocess.DEVNULL,
                env=WORKER_ENV,
                cwd="/",
                close_fds=True,
            )
        except (OSError, subprocess.SubprocessError):
            raise _reject("the static scan could not start") from None
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise _reject(f"the static scan timed out after {timeout:g} s") from None
        if returncode < 0:
            raise _reject(f"the static scan was killed by signal {-returncode}")
        if returncode != 0:
            raise _reject(f"the static scan exited with status {returncode}")
        stdout.seek(0)
        output = stdout.read(SCAN_OUTPUT_MAX_BYTES + 1)
    if len(output) > SCAN_OUTPUT_MAX_BYTES:
        raise _reject("the static scan's output is too large")
    return output


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _finding(item: object) -> Finding:
    if (
        isinstance(item, dict)
        and set(item) == {"rule", "line", "message"}
        and isinstance(item["rule"], str)
        and _RULE.fullmatch(item["rule"])
        and _is_int(item["line"])
        and item["line"] >= 0
        and isinstance(item["message"], str)
        and len(item["message"]) <= _MESSAGE_MAX
    ):
        return Finding(item["rule"], item["line"], item["message"])
    raise _reject("the static scan returned a malformed finding")


def _spans(items: object, length: int) -> tuple[tuple[int, int], ...]:
    if not isinstance(items, list):
        raise _reject("the static scan returned malformed spans")
    spans, previous_end = [], 0
    for item in items:
        if not (
            isinstance(item, list) and len(item) == 2 and all(_is_int(v) for v in item)
        ):
            raise _reject("the static scan returned malformed spans")
        start, end = item
        if not previous_end <= start <= end <= length:
            raise _reject(f"the static scan returned an invalid span ({start}, {end})")
        spans.append((start, end))
        previous_end = end
    return tuple(spans)


def _parse(output: bytes, length: int) -> ScanResult:
    try:
        document = json.loads(output.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _reject("the static scan returned malformed output") from None
    if not isinstance(document, dict):
        raise _reject("the static scan returned malformed output")
    if "error" in document:
        name, line = document.get("error"), document.get("line")
        if name == "SyntaxError" and _is_int(line):
            raise _reject(f"script line {line}: syntax error")
        if isinstance(name, str) and _EXCEPTION_NAME.fullmatch(name):
            raise _reject(f"the static scan failed ({name})")
        raise _reject("the static scan failed")
    if set(document) != {"findings", "literal_spans"} or not isinstance(
        document["findings"], list
    ):
        raise _reject("the static scan returned malformed output")
    findings = tuple(_finding(item) for item in document["findings"])
    return ScanResult(findings, _spans(document["literal_spans"], length))


def static_scan(
    text: str,
    *,
    timeout: float = float(SCAN_WALL_SECONDS),
    memory_limit: int = SCAN_RLIMIT_AS_BYTES,
    worker_source: str = WORKER_SOURCE,
) -> ScanResult:
    """Scan normalised script `text` in the limited worker.

    `memory_limit` and `worker_source` exist for tests. Raises
    `InputError("static_reject")` for any failure; findings are returned, and
    any finding blocks approval.
    """
    request = json.dumps(
        {
            "text": text,
            "allowed_modules": list(INPUT_ALLOWLIST_MODULES),
            "denied_names": list(DENIED_NAMES),
            "feature_version": list(SCAN_FEATURE_VERSION),
        }
    ).encode("ascii")
    output = _run_worker(
        request, timeout=timeout, memory_limit=memory_limit, worker_source=worker_source
    )
    return _parse(output, len(text))
