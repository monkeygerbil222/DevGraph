"""The canvas offers no shadow setting it cannot draw.

Cytoscape.js 3.x has no `shadow-*` style properties: the canvas stylesheet's
shadow mappers were ignored with one warning per element, and the "Node/edge
shadows" switch changed nothing on screen. Neither may come back.
"""

import re
from pathlib import Path

from devgraph.dashboard import app as dashboard_app

STATIC = Path(dashboard_app.__file__).resolve().parent / "static"


def _html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


def test_no_canvas_style_sets_a_shadow():
    assert not re.search(r"[\"']shadow-[a-z-]+[\"']\s*:", _html())


def test_there_is_no_shadow_switch():
    html = _html()
    assert "ctlShadowsEnabled" not in html
    assert "shadowsEnabled" not in html
    assert "Node/edge shadows" not in html


def test_there_is_no_hidden_glow_overlay():
    """The node-glow overlay canvas sat behind #cy, whose background is
    opaque, so its halos were never visible while every frame paid to draw
    them; it and its "Node glow" switch are gone. A query match still glows
    in through the edges' opacity."""
    html = _html()
    assert 'id="glowCanvas"' not in html
    assert "ctlGlowEnabled" not in html and "Node glow" not in html
    assert "function animateGlowTo(" in html
