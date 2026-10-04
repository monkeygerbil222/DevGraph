"""Sandbox limits and constants (spec §6, Q9).

Code constants with no override: nothing here is read from `Settings`, a `.env`
file, the environment or the repository.
"""

from __future__ import annotations

# Reader and selection (§3.2, §6).
SCRIPT_MAX_BYTES = 64 * 1024  # over: static_reject
INPUT_MAX_FILE_BYTES = 1024 * 1024  # over: input_cap, file skipped
INPUT_MAX_RUN_BYTES = 32 * 1024 * 1024  # over: input_cap, run refused
INPUT_MAX_FILES = 5_000  # over: input_cap

# Approval display (§3.2, §5.4).
APPROVAL_SAMPLE_SIZE = 20  # first N sorted matched paths
GROWTH_FACTOR = 2  # flag a matched count above GROWTH_FACTOR x the approved count

# Multiple digests (§5.6).
MAX_ACTIVE_DIGESTS = 5

# Schema YAML (§4.2).
YAML_ALIAS_MAX_NODES = 10_000

# Static-scan worker (§4.2).
SCAN_WALL_SECONDS = 5
SCAN_RLIMIT_AS_BYTES = 256 * 1024 * 1024
SCAN_RLIMIT_CPU_SECONDS = 5
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
