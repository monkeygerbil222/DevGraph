"""The fault fence (plan item 3): `RunConfig` is timing-only and lower-only, and
every fault lives in `_TestFaults`, which `run_provider` refuses unless the
testing switch is on. Unmarked; these pass in E2a because the switch check
precedes the stub's `NotImplementedError`."""

from __future__ import annotations

import ast
import dataclasses
import re
from pathlib import Path

import pytest

import devgraph
from devgraph.sandbox import _testing, limits
from devgraph.sandbox.runner import RunConfig, RunMode, run_provider

DEVGRAPH_ROOT = Path(devgraph.__file__).parent
SANDBOX = DEVGRAPH_ROOT / "sandbox"
FAULT_NAMES = re.compile(r"\b(_TestFaults|DivergenceCase)\b")
MAY_NAME_FAULTS = {SANDBOX / "runner.py", SANDBOX / "invocation.py", SANDBOX / "_testing.py"}

TIMING_LIMITS = {
    "start_deadline_s": limits.START_DEADLINE_SECONDS,
    "per_file_deadline_s": limits.PER_FILE_DEADLINE_SECONDS,
    "run_ceiling_s": limits.RUN_WALL_CEILING_SECONDS,
    "stdin_write_deadline_s": limits.STDIN_WRITE_DEADLINE_SECONDS,
    "lock_wait_s": limits.LOCK_WAIT_SECONDS,
}


def test_run_config_is_timing_only():
    assert {f.name for f in dataclasses.fields(RunConfig)} == set(TIMING_LIMITS)
    assert all(getattr(RunConfig(), name) is None for name in TIMING_LIMITS)


@pytest.mark.parametrize("name", sorted(TIMING_LIMITS))
def test_run_config_lower_only(name):
    limit = TIMING_LIMITS[name]
    with pytest.raises(ValueError, match=name):
        RunConfig(**{name: limit + 0.5})
    assert getattr(RunConfig(**{name: limit / 2}), name) == limit / 2
    assert getattr(RunConfig(**{name: limit}), name) == limit


def _production_modules():
    return sorted(DEVGRAPH_ROOT.rglob("*.py"))


def _enable_calls(tree: ast.AST) -> list[int]:
    """Lines that call or import `_testing.enable`."""
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "enable":
            if isinstance(node.value, ast.Name) and node.value.id == "_testing":
                lines.append(node.lineno)
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("_testing"):
            if any(alias.name == "enable" for alias in node.names):
                lines.append(node.lineno)
    return lines


def _run_provider_overrides(tree: ast.AST) -> list[int]:
    """Lines that pass `config=`, `_faults=` or `**kwargs` to `run_provider`."""
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name == "run_provider" and any(kw.arg in ("config", "_faults", None) for kw in node.keywords):
            lines.append(node.lineno)
    return lines


def test_no_production_fault_surface():
    named, enables, overrides = [], [], []
    for module in _production_modules():
        source = module.read_text(encoding="utf-8")
        tree = ast.parse(source)
        if module not in MAY_NAME_FAULTS and FAULT_NAMES.search(source):
            named.append(module)
        if module != SANDBOX / "_testing.py" and _enable_calls(tree):
            enables.append(module)
        if _run_provider_overrides(tree):
            overrides.append(module)
    assert named == []
    assert enables == []
    assert overrides == []


@pytest.mark.parametrize(
    "source, check",
    [
        ("from devgraph.sandbox import _testing\n_testing.enable()\n", _enable_calls),
        ("from devgraph.sandbox._testing import enable\n", _enable_calls),
        ("run_provider(snap, r, c, config=RunConfig())\n", _run_provider_overrides),
        ("runner.run_provider(snap, r, c, _faults=None)\n", _run_provider_overrides),
        ("run_provider(snap, r, c, **kwargs)\n", _run_provider_overrides),
    ],
)
def test_fault_surface_scan_detects(source, check):
    """The acceptance twin of the static scan: each planted violation is caught."""
    assert check(ast.parse(source))


@pytest.fixture
def switch_off():
    assert not _testing.enabled()


def test_faults_refused_without_switch(switch_off, trivial_snapshot):
    with pytest.raises(RuntimeError):
        run_provider(trivial_snapshot, "repo-1", "/srv/repo", _faults=_testing._TestFaults())
    with pytest.raises(RuntimeError):
        run_provider(trivial_snapshot, "repo-1", "/srv/repo", mode=RunMode.PROBE)


def test_faults_reach_stub_with_switch(faults, trivial_snapshot):
    assert _testing.enabled()
    with pytest.raises(NotImplementedError):
        run_provider(trivial_snapshot, "repo-1", "/srv/repo", _faults=faults())
    with pytest.raises(NotImplementedError):
        run_provider(trivial_snapshot, "repo-1", "/srv/repo", mode=RunMode.PROBE)


def test_faults_fixture_turns_switch_off_after(switch_off):
    """Runs after the test above in file order: the switch did not leak."""
    assert not _testing.enabled()
