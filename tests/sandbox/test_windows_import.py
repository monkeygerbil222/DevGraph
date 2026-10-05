"""The sandbox modules load on a platform without the Unix-only modules (Windows)."""

import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SCRIPT = textwrap.dedent(
    """
    import importlib, os, pkgutil, sys

    for name in ("pwd", "grp", "resource", "fcntl"):
        sys.modules[name] = None
    del os.getuid

    import devgraph
    import traceback

    failed = []

    def own_failure(exc):
        # A third-party package that itself imports fcntl (it would not on Windows) is
        # not ours to fix; only a failure raised from DevGraph's own frames counts.
        last = traceback.extract_tb(exc.__traceback__)[-1].filename
        return os.sep + "devgraph" + os.sep in last or last.endswith("conftest.py")

    for info in pkgutil.walk_packages(devgraph.__path__, "devgraph."):
        try:
            importlib.import_module(info.name)
        except Exception as exc:
            if own_failure(exc):
                failed.append(f"{info.name}: {exc!r}")
    sys.path.insert(0, "tests")
    try:
        import conftest
    except Exception as exc:
        if own_failure(exc):
            failed.append(f"conftest: {exc!r}")
    assert not failed, failed

    from pathlib import Path
    from devgraph.sandbox.snapshot import provider_state
    state = provider_state(
        "r", "/repo", None, platform="win32", pending=False,
        registry_path=Path("r"), store_path=Path("s"),
    )
    assert state == "unavailable", state
    print("ok")
    """
)


def test_modules_import_without_unix_only_modules():
    proc = subprocess.run(
        [sys.executable, "-c", SCRIPT], cwd=ROOT, capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"
