"""The suite's own harness: the marker, the require-sandbox session gate and the
child-process entry (plan §"The child-process harness", spec §10.1)."""

from __future__ import annotations

import json
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

from tests.conftest import REQUIRE_SANDBOX_ENV, SandboxProbe, require_sandbox_or_exit
from tests.sandbox.adversarial.conftest import REPO_ROOT

ABSENT = SandboxProbe(available=False, reason="podman not found")
PRESENT = SandboxProbe(available=True, reason="rootless podman 5.7.0")


def test_sandbox_marker_registered():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        pytest.mark.sandbox  # noqa: B018 - access is what warns for an unknown marker
        with pytest.raises(pytest.PytestUnknownMarkWarning):
            pytest.mark.sandbox_marker_that_is_not_registered  # noqa: B018


class _Exit:
    def __init__(self):
        self.calls = []

    def __call__(self, reason, returncode):
        self.calls.append(returncode)


@pytest.mark.parametrize("probe", [ABSENT, PRESENT], ids=["absent", "present"])
def test_require_sandbox_exits_session(monkeypatch, probe):
    exit_ = _Exit()
    monkeypatch.delenv(REQUIRE_SANDBOX_ENV, raising=False)
    require_sandbox_or_exit(probe, exit=exit_)
    assert exit_.calls == []

    monkeypatch.setenv(REQUIRE_SANDBOX_ENV, "1")
    require_sandbox_or_exit(probe, exit=exit_)
    if probe.available:
        assert exit_.calls == []
    else:
        assert len(exit_.calls) == 1 and exit_.calls[0] != 0


@pytest.mark.parametrize("value", ["", "0"])
def test_require_sandbox_off_values(monkeypatch, value):
    """Empty and "0" leave local absence to collection-time deselection."""
    exit_ = _Exit()
    monkeypatch.setenv(REQUIRE_SANDBOX_ENV, value)
    require_sandbox_or_exit(ABSENT, exit=exit_)
    assert exit_.calls == []


@pytest.mark.parametrize("value", ["true", "yes", " 1", "2", "on"])
@pytest.mark.parametrize("probe", [ABSENT, PRESENT], ids=["absent", "present"])
def test_require_sandbox_fails_closed(monkeypatch, value, probe):
    """Any other non-empty value ends the session, even with Podman present: a
    typo in CI must not quietly turn the gate off."""
    exit_ = _Exit()
    monkeypatch.setenv(REQUIRE_SANDBOX_ENV, value)
    require_sandbox_or_exit(probe, exit=exit_)
    assert len(exit_.calls) == 1 and exit_.calls[0] != 0


def _child(*args):
    return subprocess.run(
        [sys.executable, "-m", "tests.sandbox.adversarial._child", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_child_paths_stay_in_tmp(tmp_path):
    proc = _child(str(tmp_path), "report_paths")
    assert proc.returncode == 0, proc.stderr
    line = json.loads(proc.stdout)
    assert line["outcome"] == "ok"
    for key in ("sandbox_home", "trust_store", "lock"):
        assert Path(line[key]).is_relative_to(tmp_path), key


def test_child_refuses_unknown_scenario(tmp_path):
    proc = _child(str(tmp_path), "not_a_scenario")
    assert proc.returncode != 0
    assert proc.stdout == ""
