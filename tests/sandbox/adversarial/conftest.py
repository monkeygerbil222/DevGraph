"""Harness for the adversarial sandbox suite (spec §10, E2a).

THE E2b TEST-EDIT RULE. These tests are the acceptance bar for the runner, so
E2b may change this directory (including `_child.py` and `recorded/`), the
E2a-settled constants (`IMAGE_PINS` and `APPARMOR_PROFILE_PREFIX` in
`devgraph/sandbox/invocation.py`; `SECCOMP_ERRNO`, `SECCOMP_DENIED`,
`SECCOMP_STOCK_PERMITTED` and the frame caps in `devgraph/sandbox/limits.py`)
and the stub signatures in only two ways: removing `xfail` markers, and filling
the empty or `TODO` recorded-JSON baselines under `recorded/`. `IMAGE_PINS` is
not a fill. Any other change (an assertion, a parametrisation, a fixture
body's intent, a settled constant, a signature) is listed line by line in the
E2b PR body for the user's sign-off.

Local absence of rootless Podman >= 5.0 deselects `sandbox` tests at
collection; no test body skips. `DEVGRAPH_TEST_REQUIRE_SANDBOX` is read only by
the session hook in `tests/conftest.py`.
"""

from __future__ import annotations

import json
import platform
import secrets
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from devgraph.sandbox import _testing
from devgraph.sandbox.runner import RunConfig
from tests.conftest import sandbox_probe

REPO_ROOT = Path(__file__).resolve().parents[3]
RECORDED_DIR = Path(__file__).parent / "recorded"

# A `sandbox` test parametrised over `arch` with one of these values runs only
# on a host of that architecture.
ARCHES = ("x86_64", "aarch64")


def pytest_collection_modifyitems(config, items):
    """Deselect every `sandbox` test when rootless Podman >= 5.0 is absent, and
    every `sandbox` test whose `arch` parameter is not the host's."""
    host_arch = platform.machine()
    keep, drop = [], []
    for item in items:
        if item.get_closest_marker("sandbox") is not None:
            callspec = getattr(item, "callspec", None)
            arch = callspec.params.get("arch") if callspec is not None else None
            if not sandbox_probe().available or (arch in ARCHES and arch != host_arch):
                drop.append(item)
                continue
        keep.append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep


@pytest.fixture
def faults():
    """The `_TestFaults` constructor, with the testing switch on for this test only.
    The only place outside `_child.py` that turns the switch on."""
    _testing.enable()
    try:
        yield _testing._TestFaults
    finally:
        _testing.disable()


@pytest.fixture
def run_config():
    """The `RunConfig` constructor: timing overrides that may only lower a limit."""
    return RunConfig


@pytest.fixture
def recorded():
    """Load a recorded JSON baseline from `recorded/<name>.json`."""

    def load(name: str) -> Any:
        return json.loads((RECORDED_DIR / f"{name}.json").read_text(encoding="utf-8"))

    return load


@dataclass(frozen=True)
class HostCanary:
    token: str
    path: Path


@pytest.fixture(scope="session")
def host_canary(tmp_path_factory) -> HostCanary:
    """A unique token in a host file outside the repository. Asserted absent from
    every result, record, probe output, stderr capture and log."""
    base = tmp_path_factory.mktemp("host-canary")
    assert not base.resolve().is_relative_to(REPO_ROOT)
    token = f"devgraph-canary-{secrets.token_hex(16)}"
    path = base / "canary.txt"
    path.write_text(token, encoding="utf-8")
    return HostCanary(token=token, path=path)


@dataclass(frozen=True)
class FakeSnapshot:
    """A duck-typed `runner.ProviderSnapshot`."""

    provider_name: str
    declaration: Mapping[str, Any]
    script_text: str
    inputs: tuple[tuple[str, str], ...] = field(default=())

    def iter_inputs(self) -> Iterator[tuple[str, str]]:
        return iter(self.inputs)


@pytest.fixture
def trivial_snapshot() -> FakeSnapshot:
    """One input file and a `derive` that returns no records."""
    return FakeSnapshot(
        provider_name="runbook_links",
        declaration={
            "provider": {"name": "runbook_links", "inputs": ["docs/runbooks/**/*.md"], "params": {}},
            "node_types": [],
            "relationships": [],
        },
        script_text="def derive(ctx):\n    return []\n",
        inputs=(("docs/runbooks/restart.md", "# Restart\n"),),
    )
