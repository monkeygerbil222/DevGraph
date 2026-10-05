"""The fault fence (plan item 3): `RunConfig` is timing-only and lower-only, and
every fault lives in `_TestFaults`, which `run_provider` refuses unless the
testing switch is on. Unmarked; these pass in E2a because the switch check
precedes the stub's `NotImplementedError`.

The static scan is a tripwire, not a proof: it catches every route listed in
`BYPASSES` below, and review covers the rest.
"""

from __future__ import annotations

import ast
import dataclasses
import io
import math
import re
import tokenize
from pathlib import Path

import pytest

import devgraph
from devgraph.sandbox import _testing, limits
from devgraph.sandbox.runner import FaultsRefused, RunConfig, RunMode, run_provider

DEVGRAPH_ROOT = Path(devgraph.__file__).parent

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
    for bad in (limit + 0.5, 0, -1.0, math.nan, math.inf, -math.inf, True, False, "1"):
        with pytest.raises(ValueError, match=name):
            RunConfig(**{name: bad})
    assert getattr(RunConfig(**{name: limit / 2}), name) == limit / 2
    assert getattr(RunConfig(**{name: limit}), name) == limit


# --- The static fence -------------------------------------------------------

RUNNER = "sandbox/runner.py"
INVOCATION = "sandbox/invocation.py"
TESTING = "sandbox/_testing.py"

FAULT_NAMES = re.compile(r"\b(_TestFaults|DivergenceCase)\b")
TESTING_TOKEN = re.compile(r"\b_testing\b")
RUN_CONFIG_TOKEN = re.compile(r"\bRunConfig\b")
PROBE_TOKEN = re.compile(r"\bRunMode\s*\.\s*PROBE\b")
DYNAMIC_ACCESS = {"getattr", "setattr", "hasattr", "import_module", "__import__"}


def _testing_uses(tree: ast.AST) -> list[str]:
    """In runner.py and invocation.py: `_testing` is imported only as
    `from devgraph.sandbox import _testing` and used only as `_testing.enabled`;
    `_testing` names are imported only as `from devgraph.sandbox._testing import
    _TestFaults`. Anything else touching `_testing` is a violation."""
    found = []
    allowed_names = set()
    for node in ast.walk(tree):
        # `_testing.enabled` only as the function of a call, read not written.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            func = node.func
            if func.attr == "enabled" and isinstance(func.ctx, ast.Load):
                if isinstance(func.value, ast.Name) and func.value.id == "_testing":
                    allowed_names.add(id(func.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            absolute = node.level == 0
            for alias in node.names:
                if alias.name == "_testing" and not (
                    absolute and module == "devgraph.sandbox" and alias.asname is None
                ):
                    found.append(f"line {node.lineno}: _testing imported other than as itself")
                if alias.name == "*" and (not absolute or module == "devgraph.sandbox" or "_testing" in module):
                    found.append(f"line {node.lineno}: star import")
            if absolute and module == "devgraph.sandbox._testing":
                if any(alias.name != "_TestFaults" or alias.asname is not None for alias in node.names):
                    found.append(f"line {node.lineno}: imports more than _TestFaults")
            elif "_testing" in module:
                found.append(f"line {node.lineno}: imports from {module}")
        elif isinstance(node, ast.Import):
            if any("_testing" in alias.name for alias in node.names):
                found.append(f"line {node.lineno}: imports the _testing module")
        elif isinstance(node, ast.Name) and node.id == "_testing" and id(node) not in allowed_names:
            found.append(f"line {node.lineno}: _testing used other than as _testing.enabled")
        elif isinstance(node, ast.Attribute) and node.attr == "_testing":
            found.append(f"line {node.lineno}: _testing reached as an attribute")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "_testing" in node.value:
            found.append(f"line {node.lineno}: _testing named in a string")
    return found


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _run_provider_uses(tree: ast.AST) -> list[str]:
    """`run_provider` is only ever called directly, with no `config=`, `_faults=`
    or `**` argument: never aliased, never passed as a value."""
    found = []
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if any(alias.name == "run_provider" and alias.asname is not None for alias in node.names):
                found.append(f"line {node.lineno}: run_provider imported under another name")
        if isinstance(node, ast.Call) and _call_name(node.func) == "run_provider":
            called.add(id(node.func))
            if any(kw.arg in ("config", "_faults", None) for kw in node.keywords):
                found.append(f"line {node.lineno}: run_provider given config, _faults or **")
    for node in ast.walk(tree):
        ref = (isinstance(node, ast.Name) and node.id == "run_provider") or (
            isinstance(node, ast.Attribute) and node.attr == "run_provider"
        )
        if ref and id(node) not in called:
            found.append(f"line {node.lineno}: run_provider used as a value")
    return found


def _dynamic_names(tree: ast.AST) -> list[str]:
    """No attribute or module name built at run time (concatenation, f-string or a
    call), which would hide a `_testing` reach from the token rule."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node.func) in DYNAMIC_ACCESS:
            index = 0 if _call_name(node.func) in ("import_module", "__import__") else 1
            if len(node.args) > index and isinstance(node.args[index], (ast.BinOp, ast.JoinedStr, ast.Call)):
                found.append(f"line {node.lineno}: a computed name passed to {_call_name(node.func)}")
    return found


_SKIPPED_TOKENS = {
    tokenize.COMMENT,
    tokenize.NL,
    tokenize.NEWLINE,
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.ENCODING,
    tokenize.ENDMARKER,
}


def _code_text(source: str) -> str:
    """The source's tokens without comments, space-joined: names, operators and
    string literals (so an `importlib` string still counts), never a comment."""
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    return " ".join(tok.string for tok in tokens if tok.type not in _SKIPPED_TOKENS)


def fence_violations(rel: str, source: str) -> list[str]:
    """Every fence rule, for the module at `rel` (relative to `devgraph/`)."""
    tree = ast.parse(source)
    code = _code_text(source)
    found = []
    if rel not in (RUNNER, INVOCATION, TESTING):
        if FAULT_NAMES.search(code):
            found.append("names _TestFaults or DivergenceCase")
        if TESTING_TOKEN.search(code):
            found.append("names _testing")
    if rel in (RUNNER, INVOCATION):
        found += _testing_uses(tree)
    if rel != RUNNER:
        if RUN_CONFIG_TOKEN.search(code):
            found.append("names RunConfig")
        if PROBE_TOKEN.search(code):
            found.append("names RunMode.PROBE")
    found += _run_provider_uses(tree)
    found += _dynamic_names(tree)
    return found


def test_no_production_fault_surface():
    violations = {}
    for module in sorted(DEVGRAPH_ROOT.rglob("*.py")):
        rel = module.relative_to(DEVGRAPH_ROOT).as_posix()
        found = fence_violations(rel, module.read_text(encoding="utf-8"))
        if found:
            violations[rel] = found
    assert violations == {}


OTHER = "cli/main.py"

BYPASSES = [
    (OTHER, "from devgraph.sandbox import _testing\n_testing.enable()\n"),
    (OTHER, "from devgraph.sandbox import _testing as t\nt.enable()\n"),
    (RUNNER, "from devgraph.sandbox import _testing as t\nt.enable()\n"),
    (OTHER, "import devgraph.sandbox._testing\ndevgraph.sandbox._testing.enable()\n"),
    (RUNNER, "import devgraph.sandbox._testing\ndevgraph.sandbox._testing.enable()\n"),
    (INVOCATION, "import devgraph.sandbox as s\ns._testing.enable()\n"),
    (OTHER, "from devgraph.sandbox._testing import *\nenable()\n"),
    (RUNNER, "from devgraph.sandbox._testing import *\nenable()\n"),
    (RUNNER, "from devgraph.sandbox._testing import enable\nenable()\n"),
    (RUNNER, "from devgraph.sandbox._testing import _TestFaults as F\n"),
    (RUNNER, "from devgraph.sandbox import _testing\n_testing.enable()\n"),
    (RUNNER, "from devgraph.sandbox import _testing\n_testing._enabled = True\n"),
    (OTHER, "from devgraph.sandbox import _testing\n_testing._enabled = True\n"),
    (OTHER, "import importlib\nimportlib.import_module('devgraph.sandbox._testing').enable()\n"),
    (INVOCATION, "import importlib\nimportlib.import_module('devgraph.sandbox._testing').enable()\n"),
    (RUNNER, "from devgraph.sandbox import _testing\ngetattr(_testing, 'en' + 'able')()\n"),
    (OTHER, "import devgraph.sandbox as s\ngetattr(s, '_test' + 'ing').enable()\n"),
    (OTHER, "import importlib\nimportlib.import_module('devgraph.sandbox.' + name)\n"),
    (OTHER, "_TestFaults()\n"),
    (OTHER, "x = DivergenceCase.MEMORY\n"),
    (OTHER, "run_provider(s, r, c, config=cfg)\n"),
    (OTHER, "runner.run_provider(s, r, c, _faults=None)\n"),
    (OTHER, "run_provider(s, r, c, **kwargs)\n"),
    (OTHER, "import functools\nfunctools.partial(run_provider, config=cfg)(s, r, c)\n"),
    (OTHER, "rp = run_provider\nrp(s, r, c, config=cfg)\n"),
    (OTHER, "rp = runner.run_provider\n"),
    (OTHER, "cfg = RunConfig(lock_wait_s=1)\n"),
    (INVOCATION, "cfg = RunConfig()\n"),
    (OTHER, "run_provider(s, r, c, mode=RunMode.PROBE)\n"),
    (INVOCATION, "mode = RunMode . PROBE\n"),
    (OTHER, "from devgraph.sandbox.runner import run_provider as rp\nrp(s, r, c, config=cfg)\n"),
    (OTHER, "from devgraph.sandbox.runner import run_provider as rp\n"),
    (RUNNER, "from . import _testing as t\nt.enable()\n"),
    (INVOCATION, "from . import _testing\n_testing.enable()\n"),
    (RUNNER, "from ._testing import enable\nenable()\n"),
    (RUNNER, "from . import *\n"),
    (RUNNER, "from devgraph.sandbox import _testing\n_testing.enabled = lambda: True\n"),
    (RUNNER, "from devgraph.sandbox import _testing\nis_on = _testing.enabled\n"),
    (OTHER, "import importlib\nimportlib.import_module('devgraph.sandbox._testing')  # loads it\n"),
]


@pytest.mark.parametrize("rel, source", BYPASSES)
def test_fault_surface_scan_detects(rel, source):
    """The twin of the static scan: each planted bypass is caught."""
    assert fence_violations(rel, source)


@pytest.mark.parametrize(
    "rel, source",
    [
        (RUNNER, "from devgraph.sandbox import _testing\nif not _testing.enabled():\n    pass\n"),
        (RUNNER, "from devgraph.sandbox._testing import _TestFaults\n"),
        (RUNNER, "cfg = RunConfig()\nmode = RunMode.PROBE\n"),
        (INVOCATION, "from devgraph.sandbox._testing import _TestFaults\n"),
        (OTHER, "run_provider(snap, repo_id, canon, mode=RunMode.DRY_RUN)\n"),
        (OTHER, "getattr(settings, field_name)\n"),
        (OTHER, "x = 1  # the _testing switch, RunConfig and RunMode.PROBE stay in runner.py\n"),
        (OTHER, "from devgraph.sandbox.runner import run_provider\nrun_provider(s, r, c)\n"),
    ],
)
def test_fault_surface_scan_accepts(rel, source):
    """The acceptance twin: the forms the fence permits pass."""
    assert fence_violations(rel, source) == []


# --- The run-time fence ------------------------------------------------------


def test_faults_refused_without_switch(trivial_snapshot):
    assert not _testing.enabled()
    with pytest.raises(FaultsRefused) as excinfo:
        run_provider(trivial_snapshot, "repo-1", "/srv/repo", _faults=_testing._TestFaults())
    assert excinfo.type is FaultsRefused
    with pytest.raises(FaultsRefused) as excinfo:
        run_provider(trivial_snapshot, "repo-1", "/srv/repo", mode=RunMode.PROBE)
    assert excinfo.type is FaultsRefused


def test_faults_reach_stub_with_switch(faults, trivial_snapshot):
    assert _testing.enabled()
    with pytest.raises(NotImplementedError):
        run_provider(trivial_snapshot, "repo-1", "/srv/repo", _faults=faults())
    with pytest.raises(NotImplementedError):
        run_provider(trivial_snapshot, "repo-1", "/srv/repo", mode=RunMode.PROBE)


def test_faults_refused_is_not_the_stub_error():
    assert issubclass(FaultsRefused, RuntimeError)
    assert not issubclass(NotImplementedError, FaultsRefused)
