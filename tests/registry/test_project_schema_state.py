"""Tests for per-repository project schema state: project_config_enabled and
schema_hash registry columns.
"""

import sqlite3
import subprocess
import tempfile
from pathlib import Path

import pytest

from devgraph.registry.store import RepoRegistry


@pytest.fixture
def temp_git_repo():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        subprocess.run(["git", "init"], cwd=str(repo_path), capture_output=True, check=True)
        yield repo_path


@pytest.fixture
def second_git_repo():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir) / "second"
        repo_path.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo_path), capture_output=True, check=True)
        yield repo_path


@pytest.fixture
def registry():
    with tempfile.TemporaryDirectory() as tmpdir:
        reg = RepoRegistry(Path(tmpdir) / "registry.db")
        yield reg
        reg.close()


# Column set of the repos table before project schema state was added.
_OLD_SCHEMA = """
CREATE TABLE repos (
    repo_id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1,
    watch_enabled INTEGER NOT NULL DEFAULT 1,
    last_indexed TEXT,
    docs_path TEXT,
    pr_source_enabled INTEGER NOT NULL DEFAULT 0,
    issue_source_enabled INTEGER NOT NULL DEFAULT 0,
    last_indexed_commit TEXT,
    mentions_enabled INTEGER NOT NULL DEFAULT 0
);
"""


def test_new_repo_defaults(registry, temp_git_repo):
    record = registry.add_repo(temp_git_repo)
    assert record.project_config_enabled is True
    assert record.schema_hash is None

    fetched = registry.get(record.repo_id)
    assert fetched.project_config_enabled is True
    assert fetched.schema_hash is None


def test_existing_registry_migrates_without_losing_rows(temp_git_repo):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "registry.db"
        conn = sqlite3.connect(db_path)
        conn.executescript(_OLD_SCHEMA)
        conn.execute(
            "INSERT INTO repos (repo_id, path, active, watch_enabled, last_indexed, "
            "docs_path, pr_source_enabled, issue_source_enabled, last_indexed_commit, "
            "mentions_enabled) VALUES (?, ?, 1, 0, '2026-01-01T00:00:00', 'docs', 1, 0, "
            "'abc', 1)",
            ("legacy", str(temp_git_repo)),
        )
        conn.commit()
        conn.close()

        reg = RepoRegistry(db_path)
        try:
            cols = {row[1] for row in reg._conn.execute("PRAGMA table_info(repos)")}
            assert {"project_config_enabled", "schema_hash"} <= cols

            fetched = reg.get("legacy")
            assert fetched is not None
            assert fetched.path == Path(str(temp_git_repo))
            assert fetched.active is True
            assert fetched.watch_enabled is False
            assert fetched.last_indexed == "2026-01-01T00:00:00"
            assert fetched.docs_path == "docs"
            assert fetched.pr_source_enabled is True
            assert fetched.issue_source_enabled is False
            assert fetched.last_indexed_commit == "abc"
            assert fetched.mentions_enabled is True
            assert fetched.project_config_enabled is True
            assert fetched.schema_hash is None
            reg.set_schema_hash("legacy", "h1")
        finally:
            reg.close()

        # Reopening an already-migrated registry is idempotent.
        reg = RepoRegistry(db_path)
        try:
            repos = reg.list_repos()
            assert len(repos) == 1
            assert repos[0].repo_id == "legacy"
            assert repos[0].schema_hash == "h1"
            assert repos[0].project_config_enabled is True
        finally:
            reg.close()


def test_roundtrip_through_get_and_list(registry, temp_git_repo, second_git_repo):
    first = registry.add_repo(temp_git_repo, repo_id="first")
    second = registry.add_repo(second_git_repo, repo_id="second")

    registry.set_project_config_enabled(first.repo_id, False)
    registry.set_schema_hash(first.repo_id, "hash-a")

    fetched = registry.get(first.repo_id)
    assert fetched.project_config_enabled is False
    assert fetched.schema_hash == "hash-a"

    by_id = {r.repo_id: r for r in registry.list_repos()}
    assert by_id["first"].project_config_enabled is False
    assert by_id["first"].schema_hash == "hash-a"
    assert by_id["second"].project_config_enabled is True
    assert by_id["second"].schema_hash is None
    assert second.repo_id == "second"


def test_schema_hash_set_replace_clear(registry, temp_git_repo):
    record = registry.add_repo(temp_git_repo)

    registry.set_schema_hash(record.repo_id, "hash-1")
    assert registry.get(record.repo_id).schema_hash == "hash-1"

    registry.set_schema_hash(record.repo_id, "hash-2")
    assert registry.get(record.repo_id).schema_hash == "hash-2"

    registry.set_schema_hash(record.repo_id, None)
    assert registry.get(record.repo_id).schema_hash is None


def test_unknown_repo_raises(registry):
    with pytest.raises(ValueError):
        registry.set_schema_hash("nonexistent", "hash")
    with pytest.raises(ValueError):
        registry.set_project_config_enabled("nonexistent", False)


def test_project_config_enabled_persists_and_touches_marker(registry, temp_git_repo):
    record = registry.add_repo(temp_git_repo)
    registry._change_marker_path.unlink()
    assert registry.last_changed_at() is None

    registry.set_project_config_enabled(record.repo_id, False)
    assert registry.last_changed_at() is not None
    assert registry.get(record.repo_id).project_config_enabled is False

    registry._change_marker_path.unlink()
    registry.set_project_config_enabled(record.repo_id, True)
    assert registry.last_changed_at() is not None
    assert registry.get(record.repo_id).project_config_enabled is True
