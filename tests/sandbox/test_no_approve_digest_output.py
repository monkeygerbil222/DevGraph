"""No DevGraph output prints an approve command carrying a digest (spec §5.4).

Every line of every CLI, doctor, `add` and dashboard output in these scenarios is
scanned for `approve` followed anywhere on the line by 64 hex characters.
"""

from __future__ import annotations

import re

from devgraph.cli.main import app
from devgraph.registry.store import RepoRegistry
from devgraph.sandbox import consent
from devgraph.sandbox.snapshot import provider_snapshot

# The `config scripts` CLI tests' fixtures (autouse ones too: the CLI's registry is the
# tmp fixed-path registry, the schema is never pending, the platform is Linux).
from tests.cli.test_scripts_cli import (  # noqa: F401
    GIT,
    PROVIDER,
    _DownGraph,
    _NoGraph,
    files,
    home,
    linux,
    make_repo,
    no_approve_digest,
    not_pending,
    pytestmark,
    runner,
    settings,
)


def test_no_output_contains_approve_command_with_digest(runner, tmp_path, settings, home, monkeypatch):  # noqa: F811
    from devgraph.cli import main as main_module

    outputs: list[str] = []
    monkeypatch.setattr(main_module, "GraphEngine", _NoGraph)
    fresh = make_repo(tmp_path / "fresh", files())
    outputs.append(runner.invoke(app, ["add", str(fresh), ]).output)
    repo_id = re.search(r"Registered: (\S+)", " ".join(outputs[-1].split()))[1]
    current = provider_snapshot(fresh, PROVIDER, git=GIT).digest

    monkeypatch.setattr(consent, "require_tty", lambda *a, **k: None)
    scripts = ["config", "scripts"]
    for args, typed in [
        (["enable", repo_id], repo_id),
        (["list"], ""),
        (["show", repo_id, PROVIDER], ""),
        (["approve", repo_id, PROVIDER], "no"),  # abort
        (["approve", repo_id, PROVIDER, "--sha256", "0" * 64], ""),  # mismatch
        (["approve", repo_id, PROVIDER], PROVIDER),  # success
        (["approve", repo_id, PROVIDER, "--sha256", current], ""),
        (["list"], ""),
        (["show", repo_id, PROVIDER], ""),
        (["revoke", repo_id, PROVIDER], ""),
        (["disable", repo_id], ""),
    ]:
        outputs.append(runner.invoke(app, [*scripts, *args], input=f"{typed}\n").output)
    monkeypatch.setattr(consent, "require_tty", lambda *a, **k: (_ for _ in ()).throw(consent.ConsentError("x")))
    outputs.append(runner.invoke(app, [*scripts, "approve", repo_id]).output)
    outputs.append(runner.invoke(app, [*scripts, "enable", repo_id]).output)
    monkeypatch.setattr(main_module, "resolve_podman", lambda: None)
    monkeypatch.setattr(main_module, "GraphEngine", _DownGraph)
    outputs.append(runner.invoke(app, ["doctor"]).output)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from devgraph.dashboard import routes
    from devgraph.dashboard.events import EventBroadcaster

    class Engine:
        def read_applied_schema(self, repo_id):
            return None

    registry = RepoRegistry(settings.registry_db_path)
    try:
        api = FastAPI()
        api.include_router(routes.build_router(Engine(), registry, EventBroadcaster()))
        client = TestClient(api, base_url="http://127.0.0.1")
        outputs.append(client.get("/api/config").text)
        scope = client.get(f"/api/config/{repo_id}")
        outputs.append(scope.text)
        fingerprint = scope.json()["schema"]["fingerprint"]
        body = {"yaml": "label: Widget\nkey: [slug]\nmetadata: [{name: slug, required: true}]\n", "dry_run": True}
        outputs.append(client.post(f"/api/config/{repo_id}/schema/node_types", json=body,
                                   headers={"if-match": f'"{fingerprint}"'}).text)
    finally:
        registry.close()

    assert any(current in out for out in outputs)  # the digest is shown (by `show`) ...
    for out in outputs:  # ... but never in an approve command
        no_approve_digest(out)
