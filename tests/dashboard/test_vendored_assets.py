"""The dashboard runs offline: every script it loads ships in the package."""

import hashlib
import re
import tomllib
from pathlib import Path

from devgraph.dashboard import app as dashboard_app

STATIC = Path(dashboard_app.__file__).resolve().parent / "static"
ROOT = Path(__file__).resolve().parents[2]

# file -> (sha256 of the vendored file, the heading its notice sits under)
VENDORED = {
    "graphology-0.26.0.umd.min.js": (
        "e65803eba4826a52a84ec3fedebda3e2434ff25d86b3e2309bfe6575141ab2ff", "graphology 0.26.0"),
    "sigma-4.0.3.min.js": (
        "694694451bff0ebb4301768e14344c4601de2e52e180a655e4ceaf1644cf43b2", "sigma.js 4.0.3"),
}


def _html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


def test_index_html_loads_no_script_from_another_host():
    html = _html()
    sources = re.findall(r"<script\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)", html, flags=re.IGNORECASE)
    assert sources, "expected the vendored script tags"
    for src in sources:
        assert "//" not in src and not src.lower().startswith(("http:", "https:", "data:")), src
    assert "unpkg.com" not in html and "cdn.jsdelivr" not in html and "cdnjs" not in html


def test_the_canvas_libraries_are_vendored_at_their_pinned_versions():
    html = _html()
    for name in VENDORED:
        assert f'<script src="/static/vendor/{name}"></script>' in html
        assert (STATIC / "vendor" / name).stat().st_size > 50_000
    # graphology first: sigma draws a graphology graph
    assert html.index("graphology-0.26.0") < html.index("sigma-4.0.3")


def test_the_layout_worker_is_served_from_the_package():
    html = _html()
    assert 'new Worker("/static/graph-layout-worker.js")' in html
    assert (STATIC / "graph-layout-worker.js").is_file()


def test_nothing_vendored_is_left_unused():
    html = _html()
    for path in (STATIC / "vendor").iterdir():
        assert f"/static/vendor/{path.name}" in html, f"{path.name} is vendored but nothing loads it"


def test_the_vendored_files_are_package_data():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = pyproject["tool"]["setuptools"]["package-data"]["devgraph.dashboard"]
    shipped = {p for pattern in patterns for p in (STATIC.parent).glob(pattern)}
    assert STATIC / "index.html" in shipped
    assert STATIC / "graph-layout-worker.js" in shipped
    for name in VENDORED:
        assert STATIC / "vendor" / name in shipped


def test_the_vendored_licenses_are_noted():
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "Alexis Jacomy, Guillaume Plique, Benoît Simard" in notices  # sigma.js
    assert "Guillaume Plique (Yomguithereal)" in notices  # graphology
    for name, (_, heading) in VENDORED.items():
        assert f"## {heading}" in notices
        assert f"devgraph/dashboard/static/vendor/{name}" in notices


def test_no_vendored_file_names_a_source_map_that_is_not_shipped():
    for path in (STATIC / "vendor").iterdir():
        assert "sourceMappingURL" not in path.read_text(encoding="utf-8"), path.name


def test_the_vendored_files_are_the_pinned_builds():
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "npm tarball integrity" in notices
    for name, (sha256, _) in VENDORED.items():
        data = (STATIC / "vendor" / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == sha256, name
        assert f"sha256 `{sha256}`" in notices
