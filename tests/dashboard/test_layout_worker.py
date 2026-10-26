"""Runs the canvas layout worker's checks (layout_worker.js) as part of the
normal suite.

The force-directed settle runs in a Web Worker so a large graph settles
without freezing the page; the JS file checks it reproduces the main-thread
settle it replaced step for step, and drives its message loop.

Skipped when node isn't on PATH -- the Python suite is the one that has to run
everywhere.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_name("layout_worker.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_layout_worker():
    result = subprocess.run(
        [shutil.which("node"), str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
