"""Runs the dashboard's merge neighbour-lookup check (merge_neighbour_lookup.js)
as part of the normal suite.

mergeGraphElements places each new node next to a neighbour; finding that
neighbour used to scan every incoming relationship once per node, which took
seconds on an 8k-node view and minutes at 50k.

Skipped when node isn't on PATH -- the Python suite is the one that has to run
everywhere.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_name("merge_neighbour_lookup.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_merge_neighbour_lookup():
    result = subprocess.run(
        [shutil.which("node"), str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
