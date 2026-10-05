"""The runner interface E2b fills in (plan §"The runner interface the tests target").

The signature, vocabulary and constant tests call no stub body and pass in
E2a. The tests that call a stub are strict-xfailed and turn green in E2b.
"""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Callable

import pytest

from devgraph.sandbox import _testing, frames, invocation, limits, report, runner
from devgraph.sandbox.runner import (
    FIELD_NAMES,
    INSPECT_FIELDS,
    READINESS_FIELDS,
    REPORT_FIELDS,
    FileResult,
    ProviderSnapshot,
    RunMode,
    SchemaViolation,
    run_provider,
)

E2B = pytest.mark.xfail(strict=True, raises=NotImplementedError, reason="runner lands in E2b")

DEFAULT_CONFIG = (
    "RunConfig(start_deadline_s=None, per_file_deadline_s=None, run_ceiling_s=None, "
    "stdin_write_deadline_s=None, lock_wait_s=None)"
)

SIGNATURES: dict[Callable, str] = {
    runner.run_provider: (
        "(snapshot: 'ProviderSnapshot', repo_id: 'str', canon: 'str', *, "
        "mode: 'RunMode' = <RunMode.WRITE: 'write'>, "
        f"config: 'RunConfig' = {DEFAULT_CONFIG}, "
        "on_stderr: 'Callable[[bytes], None] | None' = None, "
        "_faults: '_TestFaults | None' = None) -> 'RunResult'"
    ),
    runner.validate_records: (
        "(records: 'Sequence[Mapping[str, Any]]', declaration: 'Mapping[str, Any]') "
        "-> 'list[Mapping[str, Any]] | SchemaViolation'"
    ),
    runner.compare_runs: "(first: 'RunResult', second: 'RunResult') -> 'tuple[tuple[str, str], ...]'",
    runner.sweep_leftover_containers: "(*, max_wall_s: 'float', now: 'float | None' = None) -> 'int'",
    runner.SandboxUnavailable.__init__: "(self, reason: 'str', *, field: 'str | None' = None) -> 'None'",
    invocation.podman_env: "() -> 'dict[str, str]'",
    invocation.build_create_argv: (
        "(*, name: 'str', arch: 'str', timeout_s: 'int', bootstrap: 'str', "
        "faults: '_TestFaults | None' = None) -> 'list[str]'"
    ),
    invocation.expected_args: "(*, bootstrap: 'str') -> 'tuple[str, ...]'",
    invocation.assert_inspect: (
        "(container_inspect: 'dict', image_inspect: 'dict', *, arch: 'str', args: 'Sequence[str]') -> 'None'"
    ),
    invocation.assert_image_pin: "(image_inspect: 'dict', *, arch: 'str') -> 'None'",
    invocation.check_readiness: "(info: 'dict') -> 'None'",
    report.check_report: (
        "(report: 'dict', *, expected_env: 'Mapping[str, str]', container_id: 'str', arch: 'str') -> 'None'"
    ),
    frames.write_frame: "(buf: 'BinaryIO', obj: 'Any') -> 'None'",
    frames.read_frame: "(buf: 'BinaryIO', *, max_len: 'int') -> 'bytes'",
    frames.decode_result: "(body: 'bytes') -> 'list[dict[str, Any]] | SchemaViolation'",
    frames.step_frames: "(state: 'SessionState', frame: 'Frame', *, seq: 'int | None') -> 'SessionState'",
    frames.FrameSequence.__init__: "(self, files_total: 'int') -> 'None'",
    frames.FrameSequence.state.fget: "(self) -> 'SessionState'",
    frames.FrameSequence.sent: "(self, seq: 'int') -> 'None'",
    frames.FrameSequence.received: "(self, frame: 'Frame', *, seq: 'int | None') -> 'None'",
    frames.FrameSequence.done.fget: "(self) -> 'bool'",
    _testing.enable: "() -> 'None'",
    _testing.disable: "() -> 'None'",
    _testing.enabled: "() -> 'bool'",
}

DATACLASS_FIELDS: dict[type, list[str]] = {
    runner.RunConfig: [
        "start_deadline_s: float | None = None",
        "per_file_deadline_s: float | None = None",
        "run_ceiling_s: float | None = None",
        "stdin_write_deadline_s: float | None = None",
        "lock_wait_s: float | None = None",
    ],
    runner.FileResult: [
        "path: str",
        "outcome: str",
        "nodes: int",
        "edges: int",
        "records: tuple[Mapping[str, Any], ...] | None",
        "probe_output: Mapping[str, Any] | None",
    ],
    runner.RunResult: [
        "outcome: str",
        "files: tuple[FileResult, ...]",
        "error_counts: Mapping[str, int]",
        "duration_s: float",
        "container_launched: bool",
    ],
    runner.SchemaViolation: ["field: str", "record_index: int", "rule: str"],
    invocation.ImagePin: [
        "image_id: str",
        "manifest_list_digest: str",
        "per_arch_digest: str",
        "repo: str",
        "interpreter_path: str",
        "config_env: tuple[str, ...]",
        "home: str",
        "entrypoint: tuple[str, ...]",
    ],
    report.MountRule: ["mount_point: str", "fs_type: str", "mode: Literal['ro', 'rw']"],
    frames.Frame: ["kind: Literal['report', 'ready', 'error', 'result']", "body: bytes"],
    frames.SessionState: ["stage: Stage", "next_seq: int", "files_total: int"],
    _testing._TestFaults: [
        "divergence: DivergenceCase | None = None",
        "bootstrap: str | None = None",
        "podman_home: Path | None = None",
        "stock_seccomp: bool = False",
    ],
}


@pytest.mark.parametrize("func", list(SIGNATURES), ids=lambda f: f.__qualname__)
def test_runner_interface_signatures(func):
    assert str(inspect.signature(func)) == SIGNATURES[func]


def _field_text(f: dataclasses.Field) -> str:
    text = f"{f.name}: {f.type}"
    return text if f.default is dataclasses.MISSING else f"{text} = {f.default!r}"


@pytest.mark.parametrize("cls", list(DATACLASS_FIELDS), ids=lambda c: c.__qualname__)
def test_interface_dataclass_fields(cls):
    assert cls.__dataclass_params__.frozen
    assert [_field_text(f) for f in dataclasses.fields(cls)] == DATACLASS_FIELDS[cls]


def test_interface_enums_and_errors():
    assert [m.name for m in RunMode] == ["WRITE", "DRY_RUN", "DRY_RUN_INTERACTIVE", "PROBE"]
    assert [m.name for m in frames.Stage] == ["AWAIT_REPORT", "AWAIT_READY", "AWAIT_RESULT", "CLOSED"]
    assert issubclass(runner.SandboxUnavailable, Exception)
    assert runner.SandboxUnavailable.code == "sandbox_unavailable"
    assert runner.SandboxUnavailable("diverged", field="Image").field == "Image"
    assert runner.SandboxUnavailable("diverged").field is None
    with pytest.raises(ValueError):
        runner.SandboxUnavailable("diverged", field="not-a-field")
    assert issubclass(runner.ProtocolError, Exception)
    assert runner.ProtocolError.code == "protocol"
    assert not hasattr(runner.RunResult, "stderr")


def test_interface_constants():
    assert frames.MAX_REPORT_FRAME == limits.REPORT_FRAME_MAX_BYTES
    assert frames.MAX_RESULT_FRAME == limits.RESULT_FRAME_MAX_BYTES + limits.RESULT_FRAME_ENVELOPE_BYTES
    assert invocation.APPARMOR_PROFILE_PREFIX == "containers-default-"
    assert all(isinstance(rule, report.MountRule) for rule in report.MOUNT_ALLOWLIST)
    assert all(isinstance(pin, invocation.ImagePin) for pin in invocation.IMAGE_PINS.values())
    assert not hasattr(runner, "RUN_ERROR_CODES")


def test_run_error_codes_complete():
    assert limits.RUN_ERROR_CODES == {
        "disabled",
        "pending",
        "awaiting_approval",
        "sandbox_unavailable",
        "input_unavailable",
        "static_reject",
        "input_cap",
        "input_decode",
        "busy",
        "timeout",
        "memory",
        "output_cap",
        "protocol",
        "schema_violation",
        "crash",
        "aborted",
    }
    assert isinstance(limits.RUN_ERROR_CODES, frozenset)


KIB = 1024
MIB = 1024 * KIB

RUNTIME_CONSTANTS = {
    "CPU_PER_CALL_SECONDS": 2,
    "PER_FILE_DEADLINE_SECONDS": 5,
    "START_DEADLINE_SECONDS": 10,
    "RUN_WALL_BASE_SECONDS": 30,
    "RUN_WALL_PER_FILE_SECONDS": 0.05,
    "RUN_WALL_CEILING_SECONDS": 300,
    "CONMON_SLACK_SECONDS": 5,
    "RETRY_MIN_BUDGET_SECONDS": 10,
    "STDIN_WRITE_DEADLINE_SECONDS": 5,
    "MEMORY_BYTES": 256 * MIB,
    "PIDS_LIMIT": 32,
    "OUTPUT_MAX_FILE_RECORDS": 1_000,
    "OUTPUT_MAX_FILE_BYTES": 1 * MIB,
    "OUTPUT_MAX_RUN_RECORDS": 50_000,
    "OUTPUT_MAX_RUN_BYTES": 16 * MIB,
    "STDERR_KEEP_BYTES": 8 * KIB,
    "LOCK_WAIT_SECONDS": 60,
    "REPORT_FRAME_MAX_BYTES": 64 * KIB,
    "RESULT_FRAME_MAX_BYTES": 1 * MIB,
}


@pytest.mark.parametrize("name", list(RUNTIME_CONSTANTS))
def test_runtime_constants_present(name):
    """Each §6 / §4.4 runtime value, pinned to the spec."""
    assert getattr(limits, name) == RUNTIME_CONSTANTS[name]


def test_result_frame_envelope_is_small():
    """The envelope is a fixed allowance for `seq` and the frame keys, not a second cap."""
    assert 0 < limits.RESULT_FRAME_ENVELOPE_BYTES <= limits.REPORT_FRAME_MAX_BYTES


# Spec §4.3, the post-create assertion table, one name per inspect field.
SPEC_INSPECT_FIELDS = {
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
    "HostConfig.IDMappings",
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

# Spec §4.4, the in-container report: mounts, identity and environment.
SPEC_REPORT_FIELDS = {
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

# Spec §4.3, readiness.
SPEC_READINESS_FIELDS = {
    "rootful",
    "remote",
    "cgroup_version",
    "controllers",
    "seccomp",
    "podman_version",
    "image_absent",
}


def test_field_vocabulary():
    assert INSPECT_FIELDS == SPEC_INSPECT_FIELDS
    assert REPORT_FIELDS == SPEC_REPORT_FIELDS
    assert READINESS_FIELDS == SPEC_READINESS_FIELDS
    assert FIELD_NAMES == INSPECT_FIELDS | REPORT_FIELDS | READINESS_FIELDS
    for subset in (INSPECT_FIELDS, REPORT_FIELDS, READINESS_FIELDS, FIELD_NAMES):
        assert isinstance(subset, frozenset)


def test_provider_snapshot_protocol(trivial_snapshot):
    assert isinstance(trivial_snapshot, ProviderSnapshot)
    path, text = next(trivial_snapshot.iter_inputs())
    assert isinstance(path, str) and isinstance(text, str)

    class MissingInputs:
        provider_name = "runbook_links"
        declaration: dict = {}
        script_text = ""

    assert not isinstance(MissingInputs(), ProviderSnapshot)


def test_divergence_case_covers_rows():
    cases = list(_testing.DivergenceCase)
    assert {case.field for case in cases} == INSPECT_FIELDS | REPORT_FIELDS
    for case in cases:
        assert case.route in {"create_flag", "containers_conf", "mounts_conf", "forged_report"}
        forged = case.name.startswith("REPORT_")
        assert forged == (case.route == "forged_report"), case.name
        if forged:
            assert case.field in REPORT_FIELDS, case.name
    # Every inspect row has a container-side divergence and every report field a
    # forged-report one, so each check is reached from a real container.
    assert {c.field for c in cases if c.route != "forged_report"} >= INSPECT_FIELDS
    assert {c.field for c in cases if c.route == "forged_report"} == REPORT_FIELDS


@E2B
def test_decode_result_return_shape():
    records = frames.decode_result(b'{"seq": 1, "records": []}')
    assert isinstance(records, list)
    bad_utf8 = frames.decode_result(b'{"seq": 1, "records": ["\xff"]}')
    assert isinstance(bad_utf8, SchemaViolation) and bad_utf8.record_index == -1
    for body in (b"{", b'{"seq": 1, "records": [NaN]}', b"\x00"):
        assert isinstance(frames.decode_result(body), SchemaViolation)


@E2B
@pytest.mark.sandbox
@pytest.mark.parametrize("mode", [RunMode.WRITE, RunMode.DRY_RUN, RunMode.DRY_RUN_INTERACTIVE])
def test_file_result_fields_per_mode(mode, trivial_snapshot):
    """The `FileResult` table: `records` only in the dry-run modes, `probe_output`
    never outside `PROBE`, and no stderr field anywhere."""
    seen = []
    on_stderr = seen.append if mode is RunMode.DRY_RUN_INTERACTIVE else None
    result = run_provider(trivial_snapshot, "repo-1", "/srv/repo", mode=mode, on_stderr=on_stderr)
    assert result.files
    for file in result.files:
        assert isinstance(file, FileResult) and file.outcome == "ok"
        assert file.probe_output is None
        if mode is RunMode.WRITE:
            assert file.records is None
        else:
            assert file.records == ()


@E2B
@pytest.mark.parametrize("mode", [RunMode.WRITE, RunMode.DRY_RUN, RunMode.PROBE])
def test_on_stderr_only_interactive(mode, faults, trivial_snapshot):
    """`faults` turns the switch on so `PROBE` reaches the `on_stderr` check."""
    with pytest.raises(ValueError):
        run_provider(trivial_snapshot, "repo-1", "/srv/repo", mode=mode, on_stderr=lambda chunk: None)
