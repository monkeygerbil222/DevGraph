"""Sandbox home, fixed paths and the fail-closed gates (spec §5.1, §5.2)."""

import importlib
import os
import sqlite3
import subprocess
import time
import types
import unicodedata
from pathlib import Path

import pytest

from devgraph.registry.store import RepoRegistry
from devgraph.sandbox import gates, limits, paths
from devgraph.sandbox.paths import SandboxPathError
from devgraph.sandbox.paths import sandbox_home as real_sandbox_home  # bound before the autouse patch
from devgraph.sandbox.trust import TrustStore

PROVIDER = "routes"
DIGEST = "ab" * 32


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


def _register(registry: Path, repo: Path, *, project_config=True) -> str:
    reg = RepoRegistry(registry)
    try:
        repo_id = reg.add_repo(repo).repo_id
        reg.set_project_config_enabled(repo_id, project_config)
        return repo_id
    finally:
        reg.close()


def _gate1(repo_id, canon, registry):
    return gates.gate1_project_config(repo_id, canon, registry_path=registry)


@pytest.fixture
def fixed_registry():
    return paths.fixed_registry_path(paths.sandbox_home())


@pytest.fixture
def repo(tmp_path):
    return _git_repo(tmp_path / "work" / "acme")


# --- paths -----------------------------------------------------------------


def test_fixture_routes_sandbox_home_to_tmp_path(tmp_path):
    home = paths.sandbox_home()
    assert home.is_relative_to(tmp_path)
    assert paths.trust_store_path(home) == home / ".devgraph" / "script_trust.sqlite3"
    assert paths.fixed_registry_path(home) == home / ".devgraph" / "registry.sqlite3"
    assert paths.sandbox_lock_path(home) == home / ".devgraph" / "sandbox.lock"


def test_canonical_repo_path_is_nfc_real_path(tmp_path):
    decomposed = unicodedata.normalize("NFD", "café")
    real = tmp_path / decomposed
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    canon = paths.canonical_repo_path(link / ".." / "link")
    assert canon == unicodedata.normalize("NFC", str(real.resolve()))
    assert canon != str(real.resolve())  # the NFD spelling was normalised
    with pytest.raises(SandboxPathError):
        paths.canonical_repo_path(tmp_path / "missing")


def test_platform_supported_is_linux_only():
    assert paths.platform_supported("linux")
    assert not paths.platform_supported("darwin")
    assert not paths.platform_supported("win32")


# --- gate 1 -----------------------------------------------------------------


def test_gate1_reads_fixed_registry_and_fails_closed(tmp_path, repo, fixed_registry, monkeypatch):
    canon = paths.canonical_repo_path(repo)

    # Missing registry.
    assert not _gate1("acme", canon, fixed_registry)

    # A correct row in a registry that Settings points at, while the fixed path has none.
    elsewhere = tmp_path / "elsewhere" / "registry.sqlite3"
    repo_id = _register(elsewhere, repo)
    monkeypatch.setenv("DEVGRAPH_REGISTRY_DB_PATH", str(elsewhere))
    from devgraph.config.settings import Settings

    assert Settings().registry_db_path == elsewhere
    assert _gate1(repo_id, canon, elsewhere)  # sanity: the row itself would read on
    assert not _gate1(repo_id, canon, fixed_registry)
    RepoRegistry(fixed_registry).close()  # the fixed registry exists but has no row
    assert not _gate1(repo_id, canon, fixed_registry)

    # Positive case.
    assert _register(fixed_registry, repo) == repo_id
    assert _gate1(repo_id, canon, fixed_registry)

    # Unknown repo_id; repo_id match with a path mismatch.
    assert not _gate1("nope", canon, fixed_registry)
    other = paths.canonical_repo_path(_git_repo(tmp_path / "work" / "acme-other"))
    assert not _gate1(repo_id, other, fixed_registry)

    # Project config off.
    reg = RepoRegistry(fixed_registry)
    reg.set_project_config_enabled(repo_id, False)
    assert not _gate1(repo_id, canon, fixed_registry)
    reg.set_project_config_enabled(repo_id, True)
    reg.close()
    assert _gate1(repo_id, canon, fixed_registry)

    # Locked. The registry runs in WAL mode, where readers see the last commit
    # through an ordinary exclusive transaction; exclusive locking mode shuts them out.
    locker = sqlite3.connect(fixed_registry, isolation_level=None)
    locker.execute("PRAGMA locking_mode=EXCLUSIVE")
    locker.execute("BEGIN EXCLUSIVE")
    locker.execute("UPDATE repos SET active = active")
    try:
        assert not _gate1(repo_id, canon, fixed_registry)
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    assert _gate1(repo_id, canon, fixed_registry)


def test_gate1_fails_closed_on_corrupt_or_columnless_registry(repo, fixed_registry):
    canon = paths.canonical_repo_path(repo)
    fixed_registry.parent.mkdir(parents=True, exist_ok=True)
    fixed_registry.write_bytes(b"\x00garbage bytes, not sqlite" * 200)
    assert not _gate1("acme", canon, fixed_registry)

    fixed_registry.unlink()
    conn = sqlite3.connect(fixed_registry)
    conn.execute("CREATE TABLE repos (repo_id TEXT PRIMARY KEY, path TEXT NOT NULL)")
    conn.execute("INSERT INTO repos VALUES ('acme', ?)", (canon,))
    conn.commit()
    conn.close()
    assert not _gate1("acme", canon, fixed_registry)


def test_gate1_compares_the_stored_path_nfc_normalised(tmp_path, fixed_registry):
    repo = _git_repo(tmp_path / unicodedata.normalize("NFD", "café"))
    repo_id = _register(fixed_registry, repo)
    assert _gate1(repo_id, paths.canonical_repo_path(repo), fixed_registry)


def test_gate1_refuses_two_rows_for_one_id(repo, fixed_registry):
    canon = paths.canonical_repo_path(repo)
    fixed_registry.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(fixed_registry)
    conn.execute("CREATE TABLE repos (repo_id TEXT, path TEXT, project_config_enabled INTEGER)")
    conn.executemany("INSERT INTO repos VALUES ('acme', ?, 1)", [(canon,), (canon,)])
    conn.commit()
    conn.close()
    assert not _gate1("acme", canon, fixed_registry)


# --- environment independence -----------------------------------------------


def _limits_snapshot(module) -> dict:
    return {name: getattr(module, name) for name in dir(module) if name.isupper()}


def _decide(repo_id, canon):
    home = paths.sandbox_home()
    return gates.evaluate_gates(
        repo_id, canon, PROVIDER, DIGEST,
        registry_path=paths.fixed_registry_path(home),
        store_path=paths.trust_store_path(home),
    )


def test_sandbox_decisions_ignore_env_and_dotenv(tmp_path, monkeypatch):
    fixture_home = tmp_path / "pw-home"
    fixture_home.mkdir()
    monkeypatch.setattr(paths, "sandbox_home", real_sandbox_home)
    pwd = pytest.importorskip("pwd")  # Unix password database; Windows reads the profile folder
    monkeypatch.setattr(
        pwd, "getpwuid",
        lambda uid: types.SimpleNamespace(pw_dir=str(fixture_home), pw_uid=uid, pw_name="tester"),
    )

    repo = _git_repo(tmp_path / "work" / "acme")
    canon = paths.canonical_repo_path(repo)
    repo_id = _register(paths.fixed_registry_path(fixture_home), repo)
    with TrustStore.open_write(paths.trust_store_path(fixture_home)) as store:
        store.set_scripts_enabled(repo_id, canon, True)
        store.approve(repo_id, canon, PROVIDER, DIGEST, declaration_json="{}", script_text="",
                      matched_count=0, keep_previous=False)

    before = {
        "home": paths.sandbox_home(),
        "store": paths.trust_store_path(paths.sandbox_home()),
        "registry": paths.fixed_registry_path(paths.sandbox_home()),
        "limits": _limits_snapshot(limits),
        "runtime": limits.SANDBOX_RUNTIME,
        "gates": _decide(repo_id, canon),
    }
    assert before["home"] == fixture_home
    assert before["gates"] == gates.GateResult(True, True, True)

    # Hostile environment: .env files in the working directory and the repository,
    # DEVGRAPH_* and HOME/USERPROFILE all pointing elsewhere.
    hostile = tmp_path / "hostile"
    (hostile / ".devgraph").mkdir(parents=True)
    hostile_registry = hostile / ".devgraph" / "registry.sqlite3"
    RepoRegistry(hostile_registry).close()
    dotenv = (
        f"DEVGRAPH_REGISTRY_DB_PATH={hostile_registry}\n"
        "DEVGRAPH_SANDBOX_RUNTIME=docker\nDEVGRAPH_SCRIPT_MAX_BYTES=999999999\n"
    )
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".env").write_text(dotenv)
    (repo / ".env").write_text(dotenv)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("DEVGRAPH_REGISTRY_DB_PATH", str(hostile_registry))
    monkeypatch.setenv("DEVGRAPH_SANDBOX_RUNTIME", "docker")
    monkeypatch.setenv("DEVGRAPH_SCRIPT_MAX_BYTES", "999999999")
    monkeypatch.setenv("DEVGRAPH_MAX_ACTIVE_DIGESTS", "500")
    monkeypatch.setenv("HOME", str(hostile))
    monkeypatch.setenv("USERPROFILE", str(hostile))

    from devgraph.config.settings import Settings

    assert Settings().registry_db_path == hostile_registry  # Settings did move

    reloaded = importlib.reload(limits)
    after = {
        "home": paths.sandbox_home(),
        "store": paths.trust_store_path(paths.sandbox_home()),
        "registry": paths.fixed_registry_path(paths.sandbox_home()),
        "limits": _limits_snapshot(reloaded),
        "runtime": reloaded.SANDBOX_RUNTIME,
        "gates": _decide(repo_id, canon),
    }
    assert after == before
    assert after["runtime"] == "podman"


# --- fix round 1 --------------------------------------------------------------


def test_canonical_repo_path_refuses_non_utf8(tmp_path):
    bad = Path(os.fsdecode(bytes(tmp_path) + b"/caf\xff"))
    bad.mkdir()
    with pytest.raises(SandboxPathError):
        paths.canonical_repo_path(bad)


def test_gate1_fails_closed_on_non_utf8_input(repo, fixed_registry):
    repo_id = _register(fixed_registry, repo)
    canon = paths.canonical_repo_path(repo)
    assert _gate1(repo_id, canon, fixed_registry)
    assert not _gate1(repo_id, canon + "\udcff", fixed_registry)
    assert not _gate1("acme\udcff", canon, fixed_registry)


def test_gates_log_and_swallow_any_exception(repo, fixed_registry, monkeypatch, caplog):
    repo_id = _register(fixed_registry, repo)
    canon = paths.canonical_repo_path(repo)
    store_path = paths.trust_store_path(paths.sandbox_home())
    with TrustStore.open_write(store_path) as store:
        store.set_scripts_enabled(repo_id, canon, True)
        store.approve(repo_id, canon, PROVIDER, DIGEST, declaration_json="{}", script_text="",
                      matched_count=0, keep_previous=False)

    def boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(TrustStore, "scripts_enabled", boom)
    monkeypatch.setattr(TrustStore, "active_digests", boom)
    with caplog.at_level("WARNING", logger="devgraph.sandbox.gates"):
        result = gates.evaluate_gates(repo_id, canon, PROVIDER, DIGEST,
                                      registry_path=object(), store_path=store_path)
    assert result == gates.GateResult(False, False, False)
    assert caplog.text.count("unexpected") == 2
    assert "TypeError" in caplog.text


def test_gate1_strict_modes_on_registry(repo, fixed_registry, monkeypatch):
    repo_id = _register(fixed_registry, repo)
    canon = paths.canonical_repo_path(repo)
    os.chmod(fixed_registry.parent, 0o700)
    assert _gate1(repo_id, canon, fixed_registry)

    os.chmod(fixed_registry.parent, 0o777)  # a pre-existing world-writable ~/.devgraph
    assert not _gate1(repo_id, canon, fixed_registry)
    os.chmod(fixed_registry.parent, 0o755)  # what a default umask gives: fine
    assert _gate1(repo_id, canon, fixed_registry)

    os.chmod(fixed_registry, 0o666)
    assert not _gate1(repo_id, canon, fixed_registry)
    os.chmod(fixed_registry, 0o644)
    assert _gate1(repo_id, canon, fixed_registry)

    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    assert not _gate1(repo_id, canon, fixed_registry)


def test_gate1_refuses_symlinked_registry(tmp_path, repo, fixed_registry):
    real = tmp_path / "real" / "registry.sqlite3"
    repo_id = _register(real, repo)
    canon = paths.canonical_repo_path(repo)
    assert _gate1(repo_id, canon, real)
    fixed_registry.parent.mkdir(mode=0o700, parents=True)
    fixed_registry.symlink_to(real)
    assert not _gate1(repo_id, canon, fixed_registry)


def test_gates_off_when_fixed_paths_lie_inside_the_repo(tmp_path, monkeypatch):
    # A dotfiles repository at the home directory holds ~/.devgraph itself.
    home = _git_repo(tmp_path / "dotfiles-home")
    monkeypatch.setattr(paths, "sandbox_home", lambda: home)
    registry = paths.fixed_registry_path(home)
    store_path = paths.trust_store_path(home)
    registry.parent.mkdir(mode=0o700)
    repo_id = _register(registry, home)
    canon = paths.canonical_repo_path(home)
    with TrustStore.open_write(store_path) as store:
        store.set_scripts_enabled(repo_id, canon, True)
        store.approve(repo_id, canon, PROVIDER, DIGEST, declaration_json="{}", script_text="",
                      matched_count=0, keep_previous=False)
    assert _decide(repo_id, canon) == gates.GateResult(False, False, False)
    # The same files read on for a repository elsewhere.
    other = _git_repo(tmp_path / "work" / "other")
    other_canon = paths.canonical_repo_path(other)
    other_id = _register(registry, other)
    with TrustStore.open_write(store_path) as store:
        store.set_scripts_enabled(other_id, other_canon, True)
        store.approve(other_id, other_canon, PROVIDER, DIGEST, declaration_json="{}", script_text="",
                      matched_count=0, keep_previous=False)
    assert _decide(other_id, other_canon) == gates.GateResult(True, True, True)


def test_locked_registry_reads_off_quickly(repo, fixed_registry):
    repo_id = _register(fixed_registry, repo)
    canon = paths.canonical_repo_path(repo)
    locker = sqlite3.connect(fixed_registry, isolation_level=None)
    locker.execute("PRAGMA locking_mode=EXCLUSIVE")
    locker.execute("BEGIN EXCLUSIVE")
    locker.execute("UPDATE repos SET active = active")
    try:
        started = time.monotonic()
        assert not _gate1(repo_id, canon, fixed_registry)
        assert time.monotonic() - started < 2
    finally:
        locker.execute("ROLLBACK")
        locker.close()
