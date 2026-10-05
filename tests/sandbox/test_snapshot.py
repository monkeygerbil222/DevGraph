"""The provider snapshot and state (spec §3.2, §5.2, §5.5).

Each file is read once through the no-follow reader, and the digest, the scan
and the approval figures all come from those bytes. Nothing runs a script.
"""

import hashlib
import os
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from devgraph.config.project_schema import parse_project_schema
from devgraph.sandbox import snapshot
from devgraph.sandbox.digest import canonical_json, provider_digest
from devgraph.sandbox.gates import GateResult
from devgraph.sandbox.reader import InputError
from devgraph.sandbox.selection import git_binary
from devgraph.sandbox.snapshot import (
    ProviderSnapshot,
    provider_snapshot,
    provider_state,
    repo_snapshots,
)

GIT = git_binary()
pytestmark = pytest.mark.skipif(GIT is None, reason="git is not installed")

_SETUP_ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/nonexistent",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}

SCHEMA = b"""\
version: 1
custom_providers:
  - name: runbook_links
    inputs: ["docs/**/*.md"]
  - name: empty
    inputs: ["nothing/*.txt"]
node_types:
  - label: Runbook
    key: [slug]
    metadata: [{name: slug, required: true}]
    source: {provider: custom, name: runbook_links}
"""

SCRIPT = b"import re\r\n\r\ndef derive(ctx):\r\n    return []  # caf\xc3\xa9\r\n"
DOCS = {f"docs/r{i:02d}.md": b"# runbook %d\n" % i for i in range(25)}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [GIT, "-C", str(repo), *args], env=_SETUP_ENV, check=True, capture_output=True
    )


def _repo(path: Path, files: dict[str, bytes]) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    for rel, data in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_bytes(data)
    _git(path, "add", "-f", "--", *files)
    _git(path, "commit", "-q", "-m", "fixture")
    return path


def _files(**extra: bytes) -> dict[str, bytes]:
    return {
        "devgraph.schema.yaml": SCHEMA,
        ".devgraph/providers/runbook_links.py": SCRIPT,
        ".devgraph/providers/empty.py": b"def derive(ctx):\n    return []\n",
        **DOCS,
        **extra,
    }


@pytest.fixture
def repo(tmp_path):
    return _repo(
        tmp_path / "acme",
        _files(**{"docs/.env.md": b"SECRET=1\n", "docs/secret.md": b"x\n"}),
    )


def _counting(monkeypatch) -> Counter:
    calls = Counter()
    for name in ("read_repo_file", "read_schema_file", "read_provider_script"):
        real = getattr(snapshot, name)

        def wrapper(root, *args, _real=real, _name=name, **kwargs):
            calls[(_name, *map(str, args))] += 1
            return _real(root, *args, **kwargs)

        monkeypatch.setattr(snapshot, name, wrapper)
    return calls


def test_snapshot_reads_each_file_once(repo, monkeypatch):
    calls = _counting(monkeypatch)
    snap = provider_snapshot(repo, "runbook_links", git=GIT)
    assert calls[("read_schema_file",)] == 1
    assert calls[("read_provider_script", "runbook_links")] == 1
    inputs = {
        key[1]: count for key, count in calls.items() if key[0] == "read_repo_file"
    }
    assert inputs == {rel: 1 for rel in DOCS}
    assert sum(calls.values()) == 2 + len(DOCS)
    assert snap.total_bytes == sum(len(data) for data in DOCS.values())


def test_repo_snapshots_read_the_schema_once(repo, monkeypatch):
    calls = _counting(monkeypatch)
    snaps = repo_snapshots(repo, git=GIT)
    assert set(snaps) == {"runbook_links", "empty"}
    assert calls[("read_schema_file",)] == 1
    assert calls[("read_provider_script", "runbook_links")] == 1
    assert calls[("read_provider_script", "empty")] == 1
    assert all(isinstance(snap, ProviderSnapshot) for snap in snaps.values())


def test_digest_is_of_the_bytes_read(repo):
    snap = provider_snapshot(repo, "runbook_links", git=GIT)
    declaration = parse_project_schema(
        SCHEMA.decode(), Path("x")
    ).custom_declaration_set("runbook_links")
    text = SCRIPT.decode().replace("\r\n", "\n")
    assert snap.script_text == text
    assert snap.declaration_set == declaration
    assert snap.declaration_json == canonical_json(declaration).decode()
    assert snap.digest == provider_digest("runbook_links", declaration, text)
    assert snap.schema_hash == "sha256:" + hashlib.sha256(SCHEMA).hexdigest()
    assert snap.name == "runbook_links"


def test_scan_runs_on_the_snapshot_text(repo):
    snap = provider_snapshot(repo, "runbook_links", git=GIT)
    assert snap.findings == ()
    assert [snap.script_text[s:e] for s, e in snap.literal_spans] == ["# café"]


def test_findings_are_recorded_not_raised(tmp_path):
    root = _repo(
        tmp_path / "acme",
        _files(**{".devgraph/providers/runbook_links.py": b"import socket\n"}),
    )
    snap = provider_snapshot(root, "runbook_links", git=GIT)
    assert {f.rule for f in snap.findings} == {"import", "no_derive"}
    assert len(snap.digest) == 64


def test_approval_figures(repo):
    snap = provider_snapshot(repo, "runbook_links", git=GIT)
    assert snap.matched == tuple(sorted(DOCS))
    assert snap.matched_count == 25
    assert snap.sample == tuple(sorted(DOCS))[:20]
    assert snap.denied == 2  # docs/.env.md and docs/secret.md
    assert snap.errors == ()


def test_no_matches_still_has_a_digest(repo):
    snap = provider_snapshot(repo, "empty", git=GIT)
    assert snap.matched == () and snap.matched_count == 0 and snap.sample == ()
    assert snap.total_bytes == 0
    assert len(snap.digest) == 64


def test_oversize_input_is_counted_and_skipped(tmp_path):
    big = b"x" * (1024 * 1024 + 1)
    root = _repo(tmp_path / "acme", _files(**{"docs/zz-big.md": big}))
    snap = provider_snapshot(root, "runbook_links", git=GIT)
    assert snap.matched_count == 26
    assert [e.code for e in snap.errors] == ["input_cap"]
    assert snap.total_bytes == sum(len(data) for data in DOCS.values())


def test_run_total_over_the_cap_is_input_cap(repo, monkeypatch):
    monkeypatch.setattr(snapshot, "INPUT_MAX_RUN_BYTES", 100)
    with pytest.raises(InputError) as info:
        provider_snapshot(repo, "runbook_links", git=GIT)
    assert info.value.code == "input_cap"


def test_symlinked_input_is_an_error_not_a_read(tmp_path):
    root = _repo(tmp_path / "acme", _files())
    os.symlink("r00.md", root / "docs" / "link.md")
    _git(root, "add", "docs/link.md")
    _git(root, "commit", "-q", "-m", "link")
    snap = provider_snapshot(root, "runbook_links", git=GIT)
    assert "docs/link.md" in snap.matched
    assert [e.code for e in snap.errors] == ["input_unavailable"]


def test_symlinked_script_is_static_reject(tmp_path):
    root = _repo(tmp_path / "acme", _files())
    script = root / ".devgraph" / "providers" / "runbook_links.py"
    (root / "real.py").write_bytes(SCRIPT)
    script.unlink()
    os.symlink(root / "real.py", script)
    with pytest.raises(InputError) as info:
        provider_snapshot(root, "runbook_links", git=GIT)
    assert info.value.code == "static_reject"


def test_missing_script_is_static_reject(tmp_path):
    root = _repo(tmp_path / "acme", _files())
    (root / ".devgraph" / "providers" / "runbook_links.py").unlink()
    with pytest.raises(InputError) as info:
        provider_snapshot(root, "runbook_links", git=GIT)
    assert info.value.code == "static_reject"


@pytest.mark.parametrize(
    "raw, code",
    [
        (b"\xef\xbb\xbfdef derive(ctx):\n    pass\n", "static_reject"),
        (b"def derive(ctx):\n  return (\n", "static_reject"),
    ],
    ids=["bom", "syntax"],
)
def test_bad_script_is_static_reject(tmp_path, raw, code):
    root = _repo(
        tmp_path / "acme", _files(**{".devgraph/providers/runbook_links.py": raw})
    )
    with pytest.raises(InputError) as info:
        provider_snapshot(root, "runbook_links", git=GIT)
    assert info.value.code == code


def test_undeclared_provider_is_unavailable(repo):
    with pytest.raises(InputError) as info:
        provider_snapshot(repo, "nope", git=GIT)
    assert info.value.code == "input_unavailable"


def test_invalid_schema_is_static_reject(tmp_path):
    root = _repo(
        tmp_path / "acme",
        _files(
            **{"devgraph.schema.yaml": b"version: 1\ncustom_providers: [{name: x}]\n"}
        ),
    )
    with pytest.raises(InputError) as info:
        repo_snapshots(root, git=GIT)
    assert info.value.code == "static_reject"


def test_no_schema_file_means_no_providers(tmp_path):
    root = _repo(tmp_path / "acme", {"README.md": b"x\n"})
    assert repo_snapshots(root, git=GIT) == {}


def test_one_bad_provider_does_not_hide_the_others(tmp_path):
    root = _repo(tmp_path / "acme", _files())
    (root / ".devgraph" / "providers" / "empty.py").unlink()
    snaps = repo_snapshots(root, git=GIT)
    assert isinstance(snaps["runbook_links"], ProviderSnapshot)
    assert (
        isinstance(snaps["empty"], InputError)
        and snaps["empty"].code == "static_reject"
    )


def test_no_work_tree_is_input_unavailable(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    for rel, data in _files().items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    with pytest.raises(InputError) as info:
        provider_snapshot(root, "runbook_links", git=GIT)
    assert info.value.code == "input_unavailable"


def test_snapshot_uses_the_real_path(tmp_path):
    root = _repo(tmp_path / "acme", _files())
    link = tmp_path / "link"
    os.symlink(root, link)
    assert provider_snapshot(link, "runbook_links", git=GIT).matched_count == 25


def test_snapshot_is_frozen(repo):
    snap = provider_snapshot(repo, "runbook_links", git=GIT)
    with pytest.raises(AttributeError):
        snap.digest = "x"


# --- provider_state -------------------------------------------------------------


@pytest.mark.parametrize(
    "gates_, pending, platform, expected",
    [
        ((True, True, True), False, "linux", "approved"),
        ((True, True, False), False, "linux", "awaiting_approval"),
        ((False, True, True), False, "linux", "disabled"),
        ((True, False, True), False, "linux", "disabled"),
        ((False, False, False), False, "linux", "disabled"),
        ((True, True, True), True, "linux", "pending"),
        ((False, False, False), True, "linux", "pending"),
        ((True, True, True), False, "darwin", "unavailable"),
        ((True, True, True), True, "win32", "unavailable"),
    ],
)
def test_provider_state_table(repo, monkeypatch, gates_, pending, platform, expected):
    snap = provider_snapshot(repo, "runbook_links", git=GIT)
    seen = {}

    def fake_gates(repo_id, canon, provider, digest, *, registry_path, store_path):
        seen.update(
            repo_id=repo_id,
            canon=canon,
            provider=provider,
            digest=digest,
            registry_path=registry_path,
            store_path=store_path,
        )
        return GateResult(*gates_)

    monkeypatch.setattr(snapshot, "evaluate_gates", fake_gates)
    state = provider_state(
        "rid",
        "/canon",
        snap,
        platform=platform,
        pending=pending,
        registry_path=Path("/reg"),
        store_path=Path("/store"),
    )
    assert state == expected
    if expected in ("approved", "awaiting_approval", "disabled"):
        assert seen == dict(
            repo_id="rid",
            canon="/canon",
            provider="runbook_links",
            digest=snap.digest,
            registry_path=Path("/reg"),
            store_path=Path("/store"),
        )


def test_provider_state_of_an_input_error_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        snapshot, "evaluate_gates", lambda *a, **k: GateResult(True, True, True)
    )
    error = InputError("input_unavailable", "not a git work tree")
    assert (
        provider_state(
            "rid",
            "/canon",
            error,
            platform="linux",
            pending=False,
            registry_path=Path("/reg"),
            store_path=Path("/store"),
        )
        == "unavailable"
    )


def test_provider_state_with_real_gates_fails_closed(repo, tmp_path):
    snap = provider_snapshot(repo, "runbook_links", git=GIT)
    state = provider_state(
        "rid",
        str(repo),
        snap,
        platform="linux",
        pending=False,
        registry_path=tmp_path / "none.db",
        store_path=tmp_path / "none.sqlite3",
    )
    assert state == "disabled"
