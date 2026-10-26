"""Runs the Layout menu's preset-layout checks (preset_layouts.js) as part of
the normal suite.

The breadthfirst, concentric and grid layouts were Cytoscape's; the page now
places the nodes itself.

Skipped when node isn't on PATH -- the Python suite is the one that has to run
everywhere.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_name("preset_layouts.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_preset_layouts():
    result = subprocess.run(
        [shutil.which("node"), str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
