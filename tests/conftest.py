"""Shared pytest configuration for environments without a desktop display."""

import functools
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

import pytest


if not os.environ.get("DISPLAY"):
    # pystray otherwise selects its X11 backend during module import and aborts
    # collection before tests that do not need a tray icon can run.
    os.environ.setdefault("PYSTRAY_BACKEND", "dummy")


REQUIRE_SANDBOX_ENV = "DEVGRAPH_TEST_REQUIRE_SANDBOX"


@dataclass(frozen=True)
class SandboxProbe:
    available: bool
    reason: str


@functools.cache
def sandbox_probe() -> SandboxProbe:
    """Whether rootless, local Podman >= 5.0 is here (spec §4.3, §10.1). Runs
    `podman info` once per session, only when a session needs the answer."""
    if sys.platform != "linux":
        return SandboxProbe(False, f"platform {sys.platform} is not linux")
    import pwd

    from devgraph.sandbox.limits import FIXED_PATH

    podman = shutil.which("podman", path=FIXED_PATH)
    if podman is None:
        return SandboxProbe(False, "podman not found")
    uid = os.getuid()
    env = {
        "PATH": FIXED_PATH,
        "HOME": pwd.getpwuid(uid).pw_dir,
        "XDG_RUNTIME_DIR": f"/run/user/{uid}",
        "LANG": "C.UTF-8",
    }
    try:
        proc = subprocess.run(
            [podman, "info", "--format", "json"], env=env, capture_output=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SandboxProbe(False, f"podman info failed: {type(exc).__name__}")
    if proc.returncode != 0:
        return SandboxProbe(False, f"podman info exited {proc.returncode}")
    try:
        info = json.loads(proc.stdout)
        rootless = info["host"]["security"]["rootless"]
        remote = info["host"]["serviceIsRemote"]
        version = info["version"]["Version"]
        major = int(version.split(".")[0])
    except (ValueError, KeyError, TypeError, AttributeError):
        return SandboxProbe(False, "podman info output not understood")
    if rootless is not True:
        return SandboxProbe(False, "podman is not rootless")
    if remote is not False:
        return SandboxProbe(False, "podman service is remote")
    if major < 5:
        return SandboxProbe(False, f"podman {version} is older than 5.0")
    return SandboxProbe(True, f"rootless podman {version}")


def _sandbox_required() -> bool:
    """The one read of the require-sandbox switch: a test-harness variable, never
    read by DevGraph."""
    return bool(os.environ.get(REQUIRE_SANDBOX_ENV))


def require_sandbox_or_exit(probe_result: SandboxProbe, *, exit=pytest.exit) -> None:
    """Fail the whole session when the sandbox is required but absent. A session
    hook, not a fixture: a fixture's skip or failure would be swallowed by the
    `xfail` markers and the gate would not bind."""
    if _sandbox_required() and not probe_result.available:
        exit(f"{REQUIRE_SANDBOX_ENV} is set but the sandbox is unavailable: {probe_result.reason}", returncode=1)


def pytest_sessionstart(session):
    if _sandbox_required():
        require_sandbox_or_exit(sandbox_probe())


@pytest.fixture(autouse=True)
def _isolate_project_switch_registry(tmp_path, monkeypatch):
    """Keep the project config switch lookup away from the user's real registry."""
    from devgraph.config import project_switch

    missing = tmp_path / "no-registry" / "registry.db"
    monkeypatch.setattr(project_switch, "_registry_db_path", lambda: missing)


@pytest.fixture(autouse=True)
def _isolate_global_tools_store(tmp_path, monkeypatch):
    """Keep the global tools store away from the user's real ~/.devgraph."""
    from devgraph.config import global_tools

    monkeypatch.setattr(global_tools, "_default_path", lambda: tmp_path / "no-global" / global_tools.GLOBAL_TOOLS_FILENAME)


@pytest.fixture(autouse=True)
def _isolate_project_trust_registry(tmp_path, monkeypatch):
    """The real trust lookup, against an empty registry in tmp: every project tools
    file is untrusted unless a test approves it or asks for `trusted_project_tools`.
    Keeps the lookup away from ~/.devgraph."""
    from devgraph.config import project_trust

    monkeypatch.setattr(project_trust, "_registry_db_path", lambda: tmp_path / "no-registry" / "registry.db")


@pytest.fixture(autouse=True)
def _isolate_sandbox_home(tmp_path, monkeypatch):
    """Route the sandbox home (trust store, fixed-path registry, lock) to tmp,
    never the user's real ~/.devgraph. Code reads it through `paths.sandbox_home`."""
    from devgraph.sandbox import paths

    monkeypatch.setattr(paths, "sandbox_home", lambda: tmp_path / "sandbox-home")


@pytest.fixture
def trusted_project_tools(monkeypatch):
    """Treat every project tools file as trusted. For tests of serving that predate
    the per-repository opt-in; apply it with `pytestmark = pytest.mark.usefixtures(...)`."""
    from devgraph.config import project_trust

    monkeypatch.setattr(project_trust, "project_tools_trust", lambda repo_root, data: "trusted")


@pytest.fixture
def real_project_trust():
    """The trust module, with the real lookup (the default); a test points
    `_registry_db_path` at its own registry."""
    from devgraph.config import project_trust

    return project_trust


@pytest.fixture(autouse=True)
def _wide_cli_console(monkeypatch):
    """Keep Rich output from wrapping at the terminal width (e.g. long tmp paths)."""
    monkeypatch.setenv("COLUMNS", "1000")
    from devgraph.cli import main

    # The CLI console is created at import time, so it has already read COLUMNS.
    monkeypatch.setattr(main.console, "_width", 1000)
