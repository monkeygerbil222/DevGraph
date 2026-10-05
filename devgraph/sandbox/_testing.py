"""The fault fence: test-only fault injection for the sandbox runner (plan item 3).

`runner.run_provider` refuses a `_TestFaults` object, and a `PROBE` run, unless
this process-local switch is on. The switch is not an environment variable and
not a setting: only the adversarial test harness turns it on
(`tests/sandbox/adversarial/conftest.py` and its child-process entry), and
`test_no_production_fault_surface` checks that no module under `devgraph/` does.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path

_enabled = False


def enable() -> None:
    global _enabled
    _enabled = True


def disable() -> None:
    global _enabled
    _enabled = False


def enabled() -> bool:
    return _enabled


@enum.unique
class DivergenceCase(enum.Enum):
    """One divergence of one checked field. The value is `(field, route, change)`:
    `field` is the inspect or report field name the refusal must name; `route` is
    how the runner produces it. `create_flag` alters exactly one `podman create`
    flag; `containers_conf` and `mounts_conf` add one entry under
    `_TestFaults.podman_home`; `forged_report` (the `REPORT_` members) runs a test
    bootstrap whose report phase sends the real report with only that field altered."""

    def __init__(self, field: str, route: str, change: str) -> None:
        self.field = field
        self.route = route
        self.change = change

    IMAGE = ("Image", "create_flag", "a different local image")
    IMAGE_DIGEST = ("ImageDigest", "create_flag", "the per-architecture digest, not the list digest")
    REPO_DIGESTS = ("RepoDigests", "create_flag", "a retagged image without the pinned digests")
    ENTRYPOINT = ("Path", "create_flag", "a different --entrypoint")
    ARGS = ("Args", "create_flag", "a different -c body")
    ENV = ("Config.Env", "create_flag", "an extra --env")
    ENV_VIA_CONTAINERS_CONF = ("Config.Env", "containers_conf", "an extra env entry")
    USER = ("Config.User", "create_flag", "a different --user")
    TIMEOUT = ("Config.Timeout", "create_flag", "a different --timeout")
    MOUNT = ("Mounts", "create_flag", "a --mount")
    BIND = ("HostConfig.Binds", "create_flag", "a --volume")
    TMPFS = ("HostConfig.Tmpfs", "create_flag", "a --tmpfs")
    DEVICE = ("HostConfig.Devices", "create_flag", "a --device")
    DEVICE_CGROUP_RULE = ("HostConfig.DeviceCgroupRules", "create_flag", "a --device-cgroup-rule")
    SYSCTL = ("HostConfig.Sysctls", "create_flag", "a --sysctl")
    PRIVILEGED = ("HostConfig.Privileged", "create_flag", "--privileged")
    SECCOMP_PROFILE = ("HostConfig.SecurityOpt", "create_flag", "the stock seccomp profile")
    NO_NEW_PRIVS = ("HostConfig.SecurityOpt", "create_flag", "no-new-privileges left out")
    PROCESS_LABEL = ("ProcessLabel", "create_flag", "a different SELinux label")
    APPARMOR = ("AppArmorProfile", "create_flag", "a different AppArmor profile")
    ULIMITS = ("HostConfig.Ulimits", "create_flag", "a different --ulimit")
    CAP_ADD = ("HostConfig.CapAdd", "create_flag", "a --cap-add")
    CAP_DROP = ("HostConfig.CapDrop", "create_flag", "--cap-drop=all left out")
    NETWORK_MODE = ("HostConfig.NetworkMode", "create_flag", "a network")
    IPC_MODE = ("HostConfig.IpcMode", "create_flag", "a different --ipc")
    PID_MODE = ("HostConfig.PidMode", "create_flag", "--pid=host")
    UTS_MODE = ("HostConfig.UTSMode", "create_flag", "--uts=host")
    USERNS_MODE = ("HostConfig.IDMappings", "create_flag", "a --userns other than nomap")
    MEMORY = ("HostConfig.Memory", "create_flag", "a different --memory")
    MEMORY_SWAP = ("HostConfig.MemorySwap", "create_flag", "a different --memory-swap")
    PIDS_LIMIT = ("HostConfig.PidsLimit", "create_flag", "a different --pids-limit")
    NANO_CPUS = ("HostConfig.NanoCpus", "create_flag", "a different --cpus")
    LOG_DRIVER = ("HostConfig.LogConfig.Type", "create_flag", "a log driver")
    READONLY_ROOTFS = ("HostConfig.ReadonlyRootfs", "create_flag", "--read-only left out")
    ANNOTATION = ("HostConfig.Annotations", "create_flag", "an extra --annotation")
    EXTRA_HOSTS = ("HostConfig.ExtraHosts", "create_flag", "an --add-host")
    DNS = ("HostConfig.Dns", "containers_conf", "a dns_servers entry")
    MOUNT_VIA_MOUNTS_CONF = ("run_secrets", "mounts_conf", "a non-empty directory at /run/secrets")
    REPORT_MOUNT_POINT = ("mountinfo", "forged_report", "a mount point not in the allowlist")
    REPORT_MOUNT_TYPE = ("mountinfo", "forged_report", "a disallowed filesystem type")
    REPORT_RO_AS_RW = ("mountinfo", "forged_report", "a read-only mount reported as rw")
    REPORT_UID_MAP = ("uid_map", "forged_report", "a range covering parent ID 0")
    REPORT_GID_MAP = ("gid_map", "forged_report", "a range covering parent ID 0")
    REPORT_UID = ("Uid", "forged_report", "a UID other than 65534")
    REPORT_CAP_PRM = ("CapPrm", "forged_report", "a non-zero set")
    REPORT_CAP_EFF = ("CapEff", "forged_report", "a non-zero set")
    REPORT_CAP_BND = ("CapBnd", "forged_report", "a non-zero set")
    REPORT_NO_NEW_PRIVS = ("NoNewPrivs", "forged_report", "0")
    REPORT_SECCOMP = ("Seccomp", "forged_report", "a mode other than 2")
    REPORT_ENV = ("environ", "forged_report", "an extra variable")
    REPORT_HOME = ("HOME", "forged_report", "a different HOME")
    REPORT_HOSTNAME = ("HOSTNAME", "forged_report", "a HOSTNAME not from the container ID")
    REPORT_RUN_SECRETS = ("run_secrets", "forged_report", "a non-empty listing")


@dataclass(frozen=True)
class _TestFaults:
    """Every fault and substitution knob. `run_provider` accepts one only while
    the switch is on."""

    divergence: DivergenceCase | None = None
    # Substitutes only the `-c <body>` string; never `--entrypoint` or the
    # interpreter. A probe body follows the real bootstrap's report phase.
    bootstrap: str | None = None
    # The podman process's HOME, for the containers.conf / mounts.conf cases only;
    # never the sandbox home (`paths.sandbox_home`).
    podman_home: Path | None = None
    # Podman's stock seccomp profile instead of ours: the control for the families
    # in `limits.SECCOMP_STOCK_PERMITTED` only.
    stock_seccomp: bool = False
