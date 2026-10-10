"""The agent's ordered, bounded shutdown, shared by the tray and headless agents."""

from __future__ import annotations

import threading
import time
from typing import Any

#: Total time shutdown waits for in-flight work before giving up on it.
SHUTDOWN_WAIT_S = 5.0


def shutdown(
    engine: Any,
    watcher: Any,
    schema_rescans: Any,
    insights: Any,
    *,
    dashboard_server: Any = None,
    dashboard_thread: threading.Thread | None = None,
    wait_s: float = SHUTDOWN_WAIT_S,
) -> None:
    """Stop every graph engine user, then close the engine, in `wait_s` total.

    The dashboard is told to exit first; the schedulers are stopped before the
    watcher, so a pass waiting on a watcher batch's lock finds itself stopped
    when it gets it. Each stop waits for its running work against one shared
    deadline, and the engine close gets what remains: it refuses new sessions
    and waits for open ones, abandoning (with one warning) a query still
    running then rather than closing the driver under it.
    """
    deadline = time.monotonic() + wait_s

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    if dashboard_server is not None:
        dashboard_server.should_exit = True
    schema_rescans.stop(timeout=remaining())
    insights.stop(timeout=remaining())
    watcher.stop(timeout=remaining())
    if dashboard_thread is not None:
        dashboard_thread.join(timeout=remaining())
    engine.close(timeout=remaining())
