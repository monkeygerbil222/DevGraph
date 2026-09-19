"""Shared pytest configuration.

`devgraph.agent.tray` imports pystray at module scope, and pystray binds to an X
display as a side effect of that import. Any module reaching it transitively --
via `devgraph.agent`, `devgraph.cli.main` or `devgraph.mcp.server` -- therefore
fails to import on a headless machine. That includes CI runners and the
Fourthought verifier, whose configured verify commands run this suite.

Skip those modules when no display is present rather than require one.
"""

import os

HEADLESS = not os.environ.get("DISPLAY")

_DISPLAY_BOUND = (
    "agent/test_lifecycle.py",
    "agent/test_tray_on_changes.py",
    "cli/test_cli.py",
    "mcp/test_server.py",
)

collect_ignore = list(_DISPLAY_BOUND) if HEADLESS else []
