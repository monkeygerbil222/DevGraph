"""The script trust store: which repositories run scripts and which digests are approved.

A dedicated SQLite file at the fixed path `paths.trust_store_path(<home>)`
(spec §5.1), separate from the registry, so `Settings` cannot move it. Every row
is keyed by `(repo_id, canonical repository path)`: a moved repository, a
re-registered id or another checkout reusing an id inherits nothing.

Readers use `open_read`, which returns None on anything wrong; callers treat
None as "scripts off" and "not approved". Writes take `BEGIN IMMEDIATE`.
Timestamps are ISO-8601 UTC, like the registry's.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from devgraph.sandbox.limits import MAX_ACTIVE_DIGESTS, TRUST_SCHEMA_VERSION

_READ_TIMEOUT = 0.5
_WRITE_TIMEOUT = 5.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repo_scripts (
    repo_id TEXT NOT NULL,
    path TEXT NOT NULL,
    scripts_enabled INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo_id, path)
);
CREATE TABLE IF NOT EXISTS approvals (
    repo_id TEXT NOT NULL,
    path TEXT NOT NULL,
    provider TEXT NOT NULL,
    digest TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'retired')),
    declaration_json TEXT NOT NULL,
    script_text TEXT NOT NULL,
    matched_count INTEGER NOT NULL,
    PRIMARY KEY (repo_id, path, provider, digest)
);
"""

# rowid order is approval order: approve() replaces the row, so a re-approval is newest.
_APPROVAL_COLUMNS = "digest, approved_at, state, declaration_json, script_text, matched_count"


class TrustStoreError(Exception):
    """The trust store cannot be opened for writing (corrupt, or another schema version)."""


@dataclass(frozen=True)
class Approval:
    digest: str
    approved_at: str
    state: str  # 'active' | 'retired'
    declaration_json: str  # kept so the next approval can show a diff (§5.4)
    script_text: str
    matched_count: int


class TrustStore:
    """Open with `open_read` (fail closed) or `open_write`; use as a context manager."""

    def __init__(self, path: Path, conn: sqlite3.Connection) -> None:
        self.path = path
        self._conn = conn

    @classmethod
    def open_read(cls, path: Path) -> TrustStore | None:
        """The store, read-only; None if it is missing, locked, corrupt or another version."""
        conn = None
        try:
            if not Path(path).is_file():
                return None
            conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True,
                                   timeout=_READ_TIMEOUT, isolation_level=None)
            if conn.execute("PRAGMA user_version").fetchone()[0] != TRUST_SCHEMA_VERSION:
                conn.close()
                return None
        except (sqlite3.Error, OSError, ValueError):
            if conn is not None:
                conn.close()
            return None
        return cls(Path(path), conn)

    @classmethod
    def open_write(cls, path: Path) -> TrustStore:
        """The store for writing, created (directory 0700, file 0600) if absent."""
        path = Path(path)
        conn = None
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.close(os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600))
            conn = sqlite3.connect(path, timeout=_WRITE_TIMEOUT, isolation_level=None)
            store = cls(path, conn)
            with store._transaction():
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    for statement in _SCHEMA.split(";"):
                        if statement.strip():
                            conn.execute(statement)
                    conn.execute(f"PRAGMA user_version = {TRUST_SCHEMA_VERSION}")
        except (sqlite3.Error, OSError) as exc:
            if conn is not None:
                conn.close()
            raise TrustStoreError(f"cannot open the trust store {path}: {exc}") from exc
        if version not in (0, TRUST_SCHEMA_VERSION):
            conn.close()
            raise TrustStoreError(f"{path} has trust schema version {version}, expected {TRUST_SCHEMA_VERSION}")
        return store

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> TrustStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def _write(self, statements: list[tuple[str, tuple]]) -> int:
        """Run the statements in one `BEGIN IMMEDIATE` transaction; the total rows changed."""
        with self._transaction():
            return sum(self._conn.execute(sql, params).rowcount for sql, params in statements)

    # --- scripts enabled (gate 2) ---------------------------------------------

    def scripts_enabled(self, repo_id: str, canon: str) -> bool:
        row = self._conn.execute(
            "SELECT scripts_enabled FROM repo_scripts WHERE repo_id = ? AND path = ?", (repo_id, canon)
        ).fetchone()
        return row is not None and row[0] == 1

    def set_scripts_enabled(self, repo_id: str, canon: str, enabled: bool) -> None:
        self._write([(
            "INSERT INTO repo_scripts (repo_id, path, scripts_enabled) VALUES (?, ?, ?) "
            "ON CONFLICT (repo_id, path) DO UPDATE SET scripts_enabled = excluded.scripts_enabled",
            (repo_id, canon, int(enabled)),
        )])

    # --- approvals (gate 3) ----------------------------------------------------

    def approvals(self, repo_id: str, canon: str, provider: str) -> list[Approval]:
        """Every approval for the provider, oldest first."""
        rows = self._conn.execute(
            f"SELECT {_APPROVAL_COLUMNS} FROM approvals WHERE repo_id = ? AND path = ? AND provider = ? "
            "ORDER BY rowid",
            (repo_id, canon, provider),
        ).fetchall()
        return [Approval(*row) for row in rows]

    def active_digests(self, repo_id: str, canon: str, provider: str) -> list[Approval]:
        """The provider's active approvals, oldest first."""
        return [a for a in self.approvals(repo_id, canon, provider) if a.state == "active"]

    def approve(
        self,
        repo_id: str,
        canon: str,
        provider: str,
        digest: str,
        *,
        declaration_json: str,
        script_text: str,
        matched_count: int,
        keep_previous: bool,
    ) -> Approval:
        """Approve `digest` now (§5.6). Retires every other digest of the provider, or with
        `keep_previous` only the oldest active ones beyond `MAX_ACTIVE_DIGESTS`.
        Approving a retired digest again is a fresh approval and reactivates it."""
        approval = Approval(digest, datetime.now(timezone.utc).isoformat(), "active",
                            declaration_json, script_text, matched_count)
        key = (repo_id, canon, provider)
        with self._transaction():
            self._conn.execute(
                "DELETE FROM approvals WHERE repo_id = ? AND path = ? AND provider = ? AND digest = ?",
                (*key, digest),
            )
            self._conn.execute(
                f"INSERT INTO approvals (repo_id, path, provider, {_APPROVAL_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*key, approval.digest, approval.approved_at, approval.state,
                 approval.declaration_json, approval.script_text, approval.matched_count),
            )
            others = [row[0] for row in self._conn.execute(
                "SELECT digest FROM approvals WHERE repo_id = ? AND path = ? AND provider = ? "
                "AND state = 'active' AND digest != ? ORDER BY rowid",
                (*key, digest),
            )]
            retire = others[: max(0, len(others) - (MAX_ACTIVE_DIGESTS - 1))] if keep_previous else others
            self._conn.executemany(
                "UPDATE approvals SET state = 'retired' "
                "WHERE repo_id = ? AND path = ? AND provider = ? AND digest = ?",
                [(*key, old) for old in retire],
            )
        return approval

    def revoke(self, repo_id: str, canon: str, provider: str, digest: str | None) -> int:
        """Delete one approval, or with None every approval of the provider; the rows removed."""
        if digest is None:
            return self._write([(
                "DELETE FROM approvals WHERE repo_id = ? AND path = ? AND provider = ?",
                (repo_id, canon, provider),
            )])
        return self._write([(
            "DELETE FROM approvals WHERE repo_id = ? AND path = ? AND provider = ? AND digest = ?",
            (repo_id, canon, provider, digest),
        )])

    def forget_repo(self, repo_id: str) -> int:
        """Delete every row for `repo_id` under any path (`devgraph remove`); the rows removed."""
        return self._write([
            ("DELETE FROM approvals WHERE repo_id = ?", (repo_id,)),
            ("DELETE FROM repo_scripts WHERE repo_id = ?", (repo_id,)),
        ])
