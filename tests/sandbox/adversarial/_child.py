"""Test-only child process for the tests that need a real second process.

    python -m tests.sandbox.adversarial._child <sandbox-home> <scenario>

A child inherits none of the parent's monkeypatches, so before anything reads a
path this module points `paths.sandbox_home` at `<sandbox-home>` (the parent
test's `tmp_path`) and turns the testing switch on. It then runs one scenario
from the fixed table below (argv names a scenario; it never carries code) and
prints one JSON line: the outcome, with the resolved sandbox home, trust-store
path and lock path, so the parent can check they stay in tmp.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _report_paths() -> dict[str, str]:
    return {"outcome": "ok"}


# Later tasks add `hold_lock` and `run_then_wait` here.
SCENARIOS = {"report_paths": _report_paths}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in SCENARIOS:
        print(f"usage: _child <sandbox-home> <{'|'.join(SCENARIOS)}>", file=sys.stderr)
        return 2
    home = Path(argv[0])

    from devgraph.sandbox import paths

    paths.sandbox_home = lambda: home

    from devgraph.sandbox import _testing

    _testing.enable()

    line = SCENARIOS[argv[1]]()
    resolved = paths.sandbox_home()
    line.update(
        sandbox_home=str(resolved),
        trust_store=str(paths.trust_store_path(resolved)),
        lock=str(paths.sandbox_lock_path(resolved)),
    )
    print(json.dumps(line))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
