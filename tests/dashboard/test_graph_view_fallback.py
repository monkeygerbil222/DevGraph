"""Runs the canvas stand-in checks (graph_view_fallback.js) as part of the
normal suite.

Without WebGL 2 the page uses a stand-in canvas; it must answer every call
the real one does, or the whole dashboard stops.

Skipped when node isn't on PATH -- the Python suite is the one that has to run
everywhere.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_name("graph_view_fallback.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_graph_view_fallback():
    result = subprocess.run(
        [shutil.which("node"), str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
