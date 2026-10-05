"""Sandbox limits and constants (spec §6, Q9).

Code constants with no override: nothing here is read from `Settings`, a `.env`
file, the environment or the repository.
"""

from __future__ import annotations

import errno
from types import MappingProxyType

# Reader and selection (§3.2, §6).
SCRIPT_MAX_BYTES = 64 * 1024  # over: static_reject
INPUT_MAX_FILE_BYTES = 1024 * 1024  # over: input_cap, file skipped
INPUT_MAX_RUN_BYTES = 32 * 1024 * 1024  # over: input_cap, run refused
INPUT_MAX_FILES = 5_000  # over: input_cap
INDEX_MAX_ENTRIES = 50 * INPUT_MAX_FILES  # git index entries read for selection; over: input_cap

# The only PATH a sandbox subprocess (selection's `git`, the static-scan worker) sees.
FIXED_PATH = "/usr/bin:/bin"
GIT_TIMEOUT_SECONDS = 30

# Approval display (§3.2, §5.4).
APPROVAL_SAMPLE_SIZE = 20  # first N sorted matched paths
GROWTH_FACTOR = 2  # flag a matched count above GROWTH_FACTOR x the approved count

# Multiple digests (§5.6).
MAX_ACTIVE_DIGESTS = 5

# Schema YAML (§4.2): the alias bound is `devgraph.config.yaml_bound.YAML_MAX_NODES`
# (10,000), shared by every config reader.

# Static-scan worker (§4.2).
SCAN_WALL_SECONDS = 5
SCAN_RLIMIT_AS_BYTES = 256 * 1024 * 1024
SCAN_RLIMIT_CPU_SECONDS = 5
SCAN_OUTPUT_MAX_BYTES = 1024 * 1024  # worker stdout; over: static_reject
SCAN_FEATURE_VERSION = (3, 11)  # the image's Python minor version

# Secret-name denylist (§3.2): always applies, matched on the NFC-normalised,
# case-folded path.
DENYLIST = (
    ".env*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "*.kdbx",
    "*.tfstate*",
    "*credential*",
    "*secret*",
)

# In-container hygiene (§4.3): importable modules and denied builtins.
INPUT_ALLOWLIST_MODULES = (
    "re",
    "json",
    "pathlib",
    "os.path",
    "string",
    "textwrap",
    "collections",
    "itertools",
    "functools",
    "dataclasses",
    "typing",
    "math",
    "fnmatch",
)
DENIED_NAMES = ("open", "exec", "eval", "compile", "__import__", "input", "breakpoint")

# Digest (§5.2) and trust store (§5.1).
DIGEST_DOMAIN = b"devgraph-script-trust\x00"
DIGEST_VERSION = 1
TRUST_SCHEMA_VERSION = 1

# Runtime and platform (§4.3, §4.5).
SANDBOX_RUNTIME = "podman"
SUPPORTED_PLATFORMS = ("linux",)

# Runner (§6, §4.4). A test may lower a timing value through `runner.RunConfig`,
# never raise it; nothing overrides a security limit.
CPU_PER_CALL_SECONDS = 2  # in-container timer per `derive` call (hygiene); over: timeout
PER_FILE_DEADLINE_SECONDS = 5  # host, request write to result read; over: kill, timeout
START_DEADLINE_SECONDS = 10  # host, report + ready; over: sandbox_unavailable
RUN_WALL_BASE_SECONDS = 30  # per-run wall = base + per file, capped at the ceiling
RUN_WALL_PER_FILE_SECONDS = 0.05
RUN_WALL_CEILING_SECONDS = 300  # bounds a run's first container and its retry together
CONMON_SLACK_SECONDS = 5  # conmon --timeout = the container's wall + this
RETRY_MIN_BUDGET_SECONDS = 10  # under this much wall left: no retry, the rest aborted
STDIN_WRITE_DEADLINE_SECONDS = 5  # per frame written to the container
MEMORY_BYTES = 256 * 1024 * 1024  # memory and memory+swap alike: no swap; over: memory
PIDS_LIMIT = 32  # over: crash
OUTPUT_MAX_FILE_RECORDS = 1_000  # over: that file output_cap
OUTPUT_MAX_FILE_BYTES = 1024 * 1024
OUTPUT_MAX_RUN_RECORDS = 50_000  # over: the rest of the run output_cap
OUTPUT_MAX_RUN_BYTES = 16 * 1024 * 1024
STDERR_KEEP_BYTES = 8 * 1024  # the rest is drained and discarded
LOCK_WAIT_SECONDS = 60  # then busy

# Frame caps (§4.4): a declared length above the cap is `protocol` before allocation.
REPORT_FRAME_MAX_BYTES = 64 * 1024  # the report and `ready`
RESULT_FRAME_MAX_BYTES = 1024 * 1024
RESULT_FRAME_ENVELOPE_BYTES = 4 * 1024  # `seq` and the frame keys around a result's records

# Error codes (§8). Runner and tests import them from here only.
RUN_ERROR_CODES = frozenset(
    {
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
)

# Seccomp expectation (§4.3). Every denied family fails with ENOSYS, so a filtered
# call cannot be mistaken for a capability check's EPERM or a permitted call's
# argument errno (EINVAL, EFAULT, EBADF). Keyed by family, then architecture; a
# family absent on an architecture is listed as None.
SECCOMP_ERRNO = errno.ENOSYS
SECCOMP_DENIED = MappingProxyType(
    {
        family: MappingProxyType({"x86_64": SECCOMP_ERRNO, "aarch64": SECCOMP_ERRNO})
        for family in (
            "unshare",
            "clone",  # with any CLONE_NEW* flag
            "clone3",
            "setns",
            "io_uring_setup",
            "io_uring_enter",
            "io_uring_register",
            "bpf",
            "userfaultfd",
            "keyctl",
            "add_key",
            "request_key",
            "perf_event_open",
            "ptrace",
            "process_vm_readv",
            "process_vm_writev",
            "mount",
            "umount2",
            "pivot_root",
            "fsopen",
            "fsmount",
            "open_tree",
            "move_mount",
            "socket",
            "socketpair",
        )
    }
)
# The families Podman's stock profile plus --cap-drop=all lets reach the kernel:
# the stock-profile control route for the denied-syscall probes.
SECCOMP_STOCK_PERMITTED = frozenset({"socket", "socketpair"})
