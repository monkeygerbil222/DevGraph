"""The sandbox runner's public interface (spec §3.3, §4, §6). Stub until E2b.

Signatures are final and pinned by `test_runner_interface_signatures`; every
function body raises `NotImplementedError("E2b")`. `run_provider` first refuses
test faults and probe runs while the testing switch is off.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Any, Protocol, runtime_checkable

from devgraph.sandbox import _testing, limits
from devgraph.sandbox._testing import _TestFaults

# The `field` a `SandboxUnavailable` may name: never a value.
INSPECT_FIELDS = frozenset(  # §4.3, the post-create assertion table
    {
        "Image",
        "ImageDigest",
        "RepoDigests",
        "Path",
        "Args",
        "Config.Env",
        "Config.User",
        "Config.Timeout",
        "Mounts",
        "HostConfig.Binds",
        "HostConfig.Tmpfs",
        "HostConfig.Devices",
        "HostConfig.DeviceCgroupRules",
        "HostConfig.Sysctls",
        "HostConfig.Privileged",
        "HostConfig.SecurityOpt",
        "ProcessLabel",
        "AppArmorProfile",
        "HostConfig.Ulimits",
        "HostConfig.CapAdd",
        "HostConfig.CapDrop",
        "HostConfig.NetworkMode",
        "HostConfig.IpcMode",
        "HostConfig.PidMode",
        "HostConfig.UTSMode",
        "HostConfig.IDMappings",  # the user-namespace row
        "HostConfig.Memory",
        "HostConfig.MemorySwap",
        "HostConfig.PidsLimit",
        "HostConfig.NanoCpus",
        "HostConfig.LogConfig.Type",
        "HostConfig.ReadonlyRootfs",
        "HostConfig.Annotations",
        "HostConfig.ExtraHosts",
        "HostConfig.Dns",
    }
)
REPORT_FIELDS = frozenset(  # §4.4, the in-container mount, identity and environment report
    {
        "mountinfo",
        "uid_map",
        "gid_map",
        "Uid",
        "CapPrm",
        "CapEff",
        "CapBnd",
        "NoNewPrivs",
        "Seccomp",
        "environ",
        "HOME",
        "HOSTNAME",
        "run_secrets",
    }
)
READINESS_FIELDS = frozenset(  # §4.3, `podman info` readiness
    {"rootful", "remote", "cgroup_version", "controllers", "seccomp", "podman_version", "image_absent"}
)
FIELD_NAMES = INSPECT_FIELDS | REPORT_FIELDS | READINESS_FIELDS


@runtime_checkable
class ProviderSnapshot(Protocol):
    provider_name: str
    declaration: Mapping[str, Any]
    script_text: str

    def iter_inputs(self) -> Iterator[tuple[str, str]]:
        """(repo-relative POSIX path, UTF-8 text) per input file."""
        ...


# The limit each `RunConfig` field may lower.
_TIMING_LIMITS = {
    "start_deadline_s": limits.START_DEADLINE_SECONDS,
    "per_file_deadline_s": limits.PER_FILE_DEADLINE_SECONDS,
    "run_ceiling_s": limits.RUN_WALL_CEILING_SECONDS,
    "stdin_write_deadline_s": limits.STDIN_WRITE_DEADLINE_SECONDS,
    "lock_wait_s": limits.LOCK_WAIT_SECONDS,
}


@dataclass(frozen=True)
class RunConfig:
    """Timing overrides for tests; `None` means the `limits` constant. An override
    may only lower its limit. There is no field for any security limit."""

    start_deadline_s: float | None = None
    per_file_deadline_s: float | None = None
    run_ceiling_s: float | None = None
    stdin_write_deadline_s: float | None = None
    lock_wait_s: float | None = None

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            if value is not None and value > _TIMING_LIMITS[f.name]:
                raise ValueError(f"{f.name} may only lower its limit")


class RunMode(enum.Enum):
    WRITE = "write"  # the indexer's path (E3); in E2b it behaves like DRY_RUN
    DRY_RUN = "dry_run"  # `run --dry-run`, non-interactive
    DRY_RUN_INTERACTIVE = "dry_run_interactive"  # `run --dry-run` at a TTY
    PROBE = "probe"  # a `_TestFaults.bootstrap` run; refused unless the testing switch is on


@dataclass(frozen=True)
class FileResult:
    """`records`: validated records for an `ok` file in the dry-run modes, else
    `None`. `probe_output`: the probe's decoded output record in `PROBE`, else
    `None`. There is no stderr field."""

    path: str
    outcome: str  # "ok" or an error code
    nodes: int
    edges: int
    records: tuple[Mapping[str, Any], ...] | None
    probe_output: Mapping[str, Any] | None


@dataclass(frozen=True)
class RunResult:
    outcome: str
    files: tuple[FileResult, ...]
    error_counts: Mapping[str, int]
    duration_s: float
    container_launched: bool


@dataclass(frozen=True)
class SchemaViolation:
    """A decode or validation failure. Names the record and the rule, never the value (§8)."""

    field: str
    record_index: int  # -1 when the failure precedes any record
    rule: str


class SandboxUnavailable(Exception):
    """The sandbox cannot be trusted for this run; raised, never returned."""

    code = "sandbox_unavailable"

    def __init__(self, reason: str, *, field: str | None = None) -> None:
        if field is not None and field not in FIELD_NAMES:
            raise ValueError("field must be one of FIELD_NAMES")
        super().__init__(reason)
        self.field = field


class ProtocolError(Exception):
    """A length, framing or sequence fault (§4.4)."""

    code = "protocol"


def run_provider(
    snapshot: ProviderSnapshot,
    repo_id: str,
    canon: str,
    *,
    mode: RunMode = RunMode.WRITE,
    config: RunConfig = RunConfig(),
    on_stderr: Callable[[bytes], None] | None = None,
    _faults: _TestFaults | None = None,
) -> RunResult:
    """Run one provider over its inputs in a fresh container. `on_stderr` is
    accepted only with `DRY_RUN_INTERACTIVE` and is the only route by which
    container stderr (the kept tail) leaves the runner."""
    if (_faults is not None or mode is RunMode.PROBE) and not _testing.enabled():
        raise RuntimeError("test faults and probe runs need the testing switch")
    raise NotImplementedError("E2b")


def validate_records(
    records: Sequence[Mapping[str, Any]], declaration: Mapping[str, Any]
) -> list[Mapping[str, Any]] | SchemaViolation:
    """The §3.3 record validator."""
    raise NotImplementedError("E2b")


def compare_runs(first: RunResult, second: RunResult) -> tuple[tuple[str, str], ...]:
    """The `--twice` comparison: (path, differing aspect) pairs, empty when the runs agree."""
    raise NotImplementedError("E2b")


def sweep_leftover_containers(*, max_wall_s: float, now: float | None = None) -> int:
    """Remove `devgraph.sandbox=1` containers older than `max_wall_s` (§4.4)."""
    raise NotImplementedError("E2b")
