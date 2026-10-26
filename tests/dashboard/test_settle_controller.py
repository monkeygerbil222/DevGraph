"""Runs the page's settle-controller checks (settle_controller.js) as part of
the normal suite.

The page sends the layout worker the graph and applies the positions it posts
on animation frames, so a hidden tab never moves the graph and then jumps
(#2); it pauses the worker while hidden and ignores a superseded settle.

Skipped when node isn't on PATH -- the Python suite is the one that has to run
everywhere.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_name("settle_controller.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_settle_controller():
    result = subprocess.run(
        [shutil.which("node"), str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
