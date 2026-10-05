"""The in-container mount, identity and environment report check (spec §4.4).
Stub until E2b.

`check_report` raises `runner.SandboxUnavailable(field=...)` naming a
`runner.REPORT_FIELDS` member, never its value. The report is compared and
discarded, never logged: `mountinfo` holds the host's storage paths.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class MountRule:
    mount_point: str
    fs_type: str
    mode: Literal["ro", "rw"]


# Generated on a clean host by `test_mount_allowlist_clean_host` (E2b) and reviewed.
MOUNT_ALLOWLIST: tuple[MountRule, ...] = ()


def check_report(report: dict, *, expected_env: Mapping[str, str], container_id: str, arch: str) -> None:
    raise NotImplementedError("E2b")
