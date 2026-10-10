"""The dashboard address to hand a browser, from the configured bind host.

Kept free of FastAPI imports: the CLI and tray build this URL without
loading the web app.
"""

from __future__ import annotations

from devgraph.config.settings import Settings

# Bind-to-everything addresses: meaningful to listen on, never a name a
# browser should address the dashboard by (the Host guard in `app.py`
# refuses them).
WILDCARD_HOSTNAMES = frozenset({"", "0.0.0.0", "::"})


def dashboard_url(settings: Settings) -> str:
    """`http://<host>:<port>` for the running dashboard.

    A wildcard bind is reached over loopback: `0.0.0.0` and the empty host
    listen on 127.0.0.1, while uvicorn binds `::` IPv6-only (asyncio sets
    IPV6_V6ONLY), so only `::1` reaches it. IPv6 literals are bracketed.
    """
    host = settings.dashboard_host.strip().strip("[]")
    if host == "::":
        host = "::1"
    elif host in WILDCARD_HOSTNAMES:
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{settings.dashboard_port}"


#: The body of the dashboard's `GET /api/health`: how a client tells DevGraph's
#: dashboard from another program listening on the same port.
DASHBOARD_IDENTITY = {"service": "devgraph-dashboard"}


def probe_dashboard(url: str, timeout_s: float = 1.0) -> str:
    """Who answers at `url`: "devgraph", "other" (another program holds the
    port) or "none" (nothing listens there)."""
    import http.client
    import json
    import urllib.error
    import urllib.request

    # No proxy: the dashboard is local, and a proxy would answer for it.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{url}/api/health", timeout=timeout_s) as response:
            body = json.loads(response.read(4096))
    except urllib.error.URLError as exc:
        return "none" if isinstance(exc.reason, ConnectionRefusedError) else "other"
    except ConnectionRefusedError:
        return "none"
    except (OSError, ValueError, http.client.HTTPException):
        return "other"
    return "devgraph" if body == DASHBOARD_IDENTITY else "other"
