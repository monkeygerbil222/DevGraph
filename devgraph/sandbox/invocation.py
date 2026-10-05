"""The podman invocation: constructed environment, create argv, image pin, the
post-create inspect assertion and readiness (spec §4.3). Stub until E2b.

Each check raises `runner.SandboxUnavailable(field=...)`, naming the field from
`runner.FIELD_NAMES` and never its value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from devgraph.sandbox._testing import _TestFaults

# A host-level expectation, not per image: with AppArmor enabled `AppArmorProfile`
# starts with this prefix and is never `unconfined`; with it disabled, empty.
APPARMOR_PROFILE_PREFIX = "containers-default-"


@dataclass(frozen=True)
class ImagePin:
    image_id: str
    manifest_list_digest: str
    per_arch_digest: str
    repo: str
    interpreter_path: str
    config_env: tuple[str, ...]
    home: str  # UID 65534's home in the image's /etc/passwd
    entrypoint: tuple[str, ...]  # the image's own, recorded; never used


# Keyed by architecture (`x86_64`, `aarch64`); settled by inspection in E2a.
IMAGE_PINS: Mapping[str, ImagePin] = MappingProxyType({})


def podman_env() -> dict[str, str]:
    """`PATH` (fixed), `HOME` (password database), `XDG_RUNTIME_DIR`, `LANG`; nothing else."""
    raise NotImplementedError("E2b")


def build_create_argv(
    *, name: str, arch: str, timeout_s: int, bootstrap: str, faults: _TestFaults | None = None
) -> list[str]:
    """The `podman create` argv. `--entrypoint` and the image come from
    `IMAGE_PINS[arch]`; the trailing args are `expected_args(bootstrap=bootstrap)`.
    A `faults.divergence` alters exactly one other flag."""
    raise NotImplementedError("E2b")


def expected_args(*, bootstrap: str) -> tuple[str, ...]:
    """The inspect `Args` for the bootstrap actually passed."""
    raise NotImplementedError("E2b")


def assert_inspect(
    container_inspect: dict, image_inspect: dict, *, arch: str, args: Sequence[str]
) -> None:
    """Raise on the first §4.3 table row that diverges."""
    raise NotImplementedError("E2b")


def assert_image_pin(image_inspect: dict, *, arch: str) -> None:
    """Image ID, list digest, per-architecture digest and `RepoDigests`."""
    raise NotImplementedError("E2b")


def check_readiness(info: dict) -> None:
    """The `podman info` readiness gate; names a `runner.READINESS_FIELDS` member."""
    raise NotImplementedError("E2b")
