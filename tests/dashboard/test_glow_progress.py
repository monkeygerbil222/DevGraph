"""Runs the dashboard's glow-progress check (glow_progress.js)
as part of the normal suite.

The glow-in animation's first frame can be stamped before the animation
started; its progress was then negative and every edge's opacity invalid for
a frame.

Skipped when node isn't on PATH -- the Python suite is the one that has to run
everywhere.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_name("glow_progress.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_glow_progress():
    result = subprocess.run(
        [shutil.which("node"), str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
