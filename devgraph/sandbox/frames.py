"""The host side of the frame protocol (spec §3.3, §4.4). Stub until E2b.

A frame is a 4-byte big-endian length, then that many bytes. The boundary:

- `protocol` (`runner.ProtocolError`, raised): length, framing and sequence
  faults, found by `read_frame` and `step_frames` before any JSON is parsed.
- `schema_violation` (`runner.SchemaViolation`, returned): everything from
  UTF-8 decoding and `json.loads` onward, applied to a body that passed framing.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, BinaryIO, Literal

from devgraph.sandbox import limits
from devgraph.sandbox.runner import SchemaViolation

# The only `max_len` values a read uses.
MAX_REPORT_FRAME = limits.REPORT_FRAME_MAX_BYTES  # the report and `ready`
MAX_RESULT_FRAME = limits.RESULT_FRAME_MAX_BYTES + limits.RESULT_FRAME_ENVELOPE_BYTES


def write_frame(buf: BinaryIO, obj: Any) -> None:
    raise NotImplementedError("E2b")


def read_frame(buf: BinaryIO, *, max_len: int) -> bytes:
    """Raise `ProtocolError` on a declared length over `max_len`, before allocating,
    and on a truncated prefix or body."""
    raise NotImplementedError("E2b")


def decode_result(body: bytes) -> list[dict[str, Any]] | SchemaViolation:
    """The strict §3.3 decoder for a result body that passed `FrameSequence`.
    Never raises: the records, or one `SchemaViolation` (`record_index` -1 when
    the failure precedes any record)."""
    raise NotImplementedError("E2b")


class Stage(enum.Enum):
    AWAIT_REPORT = "await_report"
    AWAIT_READY = "await_ready"
    AWAIT_RESULT = "await_result"
    CLOSED = "closed"


@dataclass(frozen=True)
class Frame:
    kind: Literal["report", "ready", "error", "result"]  # from the host's stage, not the container
    body: bytes


@dataclass(frozen=True)
class SessionState:
    stage: Stage
    next_seq: int  # the `seq` the host last sent
    files_total: int


def step_frames(state: SessionState, frame: Frame, *, seq: int | None) -> SessionState:
    """The next state, or `ProtocolError` on a sequence fault. `seq` is the value the
    caller extracted from the frame's envelope."""
    raise NotImplementedError("E2b")


class FrameSequence:
    """A mutable wrapper over `step_frames` for the reader thread."""

    def __init__(self, files_total: int) -> None:
        raise NotImplementedError("E2b")

    @property
    def state(self) -> SessionState:
        raise NotImplementedError("E2b")

    def sent(self, seq: int) -> None:
        """The host wrote request `seq`."""
        raise NotImplementedError("E2b")

    def received(self, frame: Frame, *, seq: int | None) -> None:
        raise NotImplementedError("E2b")

    @property
    def done(self) -> bool:
        raise NotImplementedError("E2b")
