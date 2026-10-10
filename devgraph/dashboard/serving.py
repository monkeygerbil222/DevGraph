"""Run the agent's dashboard server, turning a port it cannot bind into a
message instead of uvicorn's silent `SystemExit`.

uvicorn logs a bind failure (hidden at the agent's `critical` log level) and
calls `sys.exit`, which `except Exception` does not catch. Binding the socket
here first gives the real `OSError`; the `SystemExit` catch covers any other
startup failure (a lifespan error).
"""

from __future__ import annotations

import asyncio
import errno
import socket
import sys
from dataclasses import dataclass

import uvicorn

from devgraph.config.settings import devgraph_home

# Windows reports an address in use as WSAEADDRINUSE.
_ADDRESS_IN_USE = {errno.EADDRINUSE, 10048}


@dataclass(frozen=True)
class DashboardFailure:
    """Why the dashboard is off: `summary` fits a tray tooltip, `detail` is the log line."""

    summary: str
    detail: str


def _bind(host: str, port: int) -> socket.socket:
    """The listening socket uvicorn would bind (its `Config.bind_socket` rules)."""
    sock = socket.socket(family=socket.AF_INET6 if ":" in host else socket.AF_INET)
    try:
        if sys.platform != "win32":  # on Windows SO_REUSEADDR lets two servers share a port
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise
    sock.set_inheritable(True)
    return sock


def _fix_hint() -> str:
    return (
        f"set DEVGRAPH_DASHBOARD_PORT to a free port in {devgraph_home() / '.env'} "
        "(or the environment) and restart the agent"
    )


def serve_dashboard(server: uvicorn.Server, loop: asyncio.AbstractEventLoop) -> DashboardFailure | None:
    """Serve until the server exits; the failure when it could not start, else None."""
    host, port = server.config.host, server.config.port
    try:
        sock = _bind(host, port)
    except OSError as exc:
        if exc.errno in _ADDRESS_IN_USE:
            return DashboardFailure(
                f"dashboard port {port} in use",
                f"dashboard port {port} on {host} is already in use (another program, or another "
                f"DevGraph agent, holds it), so the dashboard is off; {_fix_hint()}",
            )
        return DashboardFailure(
            f"dashboard cannot bind port {port}",
            f"dashboard cannot listen on {host}:{port} ({exc.strerror or exc}), so it is off; {_fix_hint()}",
        )
    try:
        loop.run_until_complete(server.serve(sockets=[sock]))
    except SystemExit:
        return DashboardFailure(
            "dashboard failed to start", f"dashboard on {host}:{port} failed to start, so it is off"
        )
    finally:
        sock.close()
    return None
