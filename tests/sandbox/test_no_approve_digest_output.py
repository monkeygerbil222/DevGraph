"""No DevGraph output prints an approve command carrying a digest (spec §5.4).

Every CLI, doctor, `add`, `remove` and dashboard output in these scenarios is scanned
(`no_approve_digest`) for `approve` followed anywhere on the line by 64 hex characters,
for `--sha256` followed by 64 hex characters (across line breaks too), and for a bare
64-hex string on a line next to one that says approve.
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
    SCRIPT,
    _DownGraph,
    _NoGraph,
    _StubGraph,
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

    # Approved again, then project config off and on: the resume lines.
    monkeypatch.setattr(consent, "require_tty", lambda *a, **k: None)
    outputs.append(runner.invoke(app, [*scripts, "enable", repo_id], input=f"{repo_id}\n").output)
    outputs.append(runner.invoke(app, [*scripts, "approve", repo_id], input=f"{PROVIDER}\n").output)
    outputs.append(runner.invoke(app, ["config", "disable", repo_id]).output)
    resumed = runner.invoke(app, ["config", "enable", repo_id]).output
    assert "remains approved" in " ".join(resumed.split())
    outputs.append(resumed)
    outputs.append(runner.invoke(app, ["doctor"]).output)  # approved
    for status in ("pending", "unreachable"):
        monkeypatch.setattr(main_module, "_applied_schema_status", lambda repo_id, schema_hash, s=status: s)
        outputs.append(runner.invoke(app, ["doctor"]).output)
        outputs.append(runner.invoke(app, [*scripts, "list"]).output)
        outputs.append(runner.invoke(app, [*scripts, "show", repo_id, PROVIDER]).output)
    monkeypatch.setattr(main_module, "_applied_schema_status", lambda repo_id, schema_hash: "applied")
    (fresh / ".devgraph" / "providers" / f"{PROVIDER}.py").write_text(SCRIPT + "# changed\n")
    awaiting = runner.invoke(app, ["doctor"]).output
    assert "awaiting_approval" in awaiting
    outputs.append(awaiting)

    # A repository whose walk skips a provider with findings and a rejected one.
    schema = files()["devgraph.schema.yaml"].replace(
        "node_types:", '  - name: bad\n    inputs: ["docs/*.md"]\n  - name: broken\n    inputs: ["docs/*.md"]\nnode_types:')
    mixed = make_repo(tmp_path / "mixed", files(schema=schema, **{
        ".devgraph/providers/bad.py": "import socket\n",
        ".devgraph/providers/broken.py": "def derive(ctx):\n  return (\n",
    }))
    monkeypatch.setattr(main_module, "GraphEngine", _NoGraph)
    added = runner.invoke(app, ["add", str(mixed)]).output
    outputs.append(added)
    mixed_id = re.search(r"Registered: (\S+)", " ".join(added.split()))[1]
    for args, typed in [
        (["enable", mixed_id], mixed_id),
        (["approve", mixed_id], PROVIDER),  # walk: approves one, skips two
        (["approve", mixed_id], ""),  # nothing awaiting, still skips
        (["approve", mixed_id, "bad"], "bad"),  # findings
        (["approve", mixed_id, "broken"], "broken"),  # rejected
        (["approve", mixed_id, "bad", "--sha256", current], ""),
        (["revoke", mixed_id, PROVIDER, "--digest", "zz"], ""),
        (["revoke", mixed_id, PROVIDER, "--digest", current], ""),
        (["revoke", mixed_id, PROVIDER, "--digest", "0" * 12], ""),
        (["show", mixed_id, "bad"], ""),
        (["show", mixed_id, "broken"], ""),
        (["list"], ""),
    ]:
        outputs.append(runner.invoke(app, [*scripts, *args], input=f"{typed}\n").output)
    monkeypatch.setattr(main_module, "GraphEngine", _StubGraph)
    outputs.append(runner.invoke(app, ["remove", mixed_id]).output)

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
        # The Q13 refusals: adding a custom node type, and editing the existing one (dry run and real).
        headers = {"if-match": f'"{fingerprint}"'}
        custom = "label: Playbook\nkey: [slug]\nmetadata: [{name: slug, required: true}]\nsource: {provider: custom, name: runbook_links}\n"
        for dry_run in (True, False):
            refused = [
                client.post(f"/api/config/{repo_id}/schema/node_types", json={"yaml": custom, "dry_run": dry_run}, headers=headers),
                client.put(f"/api/config/{repo_id}/schema/node_types/Runbook",
                           json={"yaml": custom.replace("Playbook", "Runbook"), "dry_run": dry_run}, headers=headers),
            ]
            assert all(r.status_code == 422 for r in refused), [r.text for r in refused]
            outputs.extend(r.text for r in refused)
    finally:
        registry.close()

    assert any(current in out for out in outputs)  # the digest is shown (by `show`) ...
    for out in outputs:  # ... but never in an approve command
        no_approve_digest(out)
