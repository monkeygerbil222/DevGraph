"""The three script gates (spec §5.2), each failing closed.

1. Project config is on, read from the registry at its fixed default path
   (never through `Settings` or `project_switch`, which fails open).
2. Scripts are enabled for `(repo_id, canonical path)` in the trust store.
3. The provider's current digest is approved and active.

Every function takes its file paths as arguments; callers pass
`paths.fixed_registry_path(paths.sandbox_home())` and
`paths.trust_store_path(paths.sandbox_home())`. Any error means False.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from pathlib import Path
from typing import NamedTuple

from devgraph.sandbox.trust import TrustStore

_REGISTRY_TIMEOUT = 0.5


class GateResult(NamedTuple):
    project_config: bool
    scripts_enabled: bool
    digest_active: bool


def gate1_project_config(repo_id: str, canon: str, *, registry_path: Path) -> bool:
    """True only for exactly one registry row with this id whose stored path, NFC-normalised,
    equals `canon` and whose `project_config_enabled` is 1. The stored path is not re-resolved."""
    conn = None
    try:
        if not Path(registry_path).is_file():
            return False
        conn = sqlite3.connect(f"{Path(registry_path).resolve().as_uri()}?mode=ro", uri=True,
                               timeout=_REGISTRY_TIMEOUT)
        rows = conn.execute(
            "SELECT path, project_config_enabled FROM repos WHERE repo_id = ?", (repo_id,)
        ).fetchall()
    except (sqlite3.Error, OSError, ValueError):
        return False
    finally:
        if conn is not None:
            conn.close()
    if len(rows) != 1:
        return False
    path, enabled = rows[0]
    return isinstance(path, str) and unicodedata.normalize("NFC", path) == canon and enabled == 1


def gate2_scripts_enabled(repo_id: str, canon: str, *, store_path: Path) -> bool:
    store = TrustStore.open_read(store_path)
    if store is None:
        return False
    try:
        return store.scripts_enabled(repo_id, canon)
    except sqlite3.Error:
        return False
    finally:
        store.close()


def gate3_digest_active(repo_id: str, canon: str, provider: str, digest: str, *, store_path: Path) -> bool:
    store = TrustStore.open_read(store_path)
    if store is None:
        return False
    try:
        return any(a.digest == digest for a in store.active_digests(repo_id, canon, provider))
    except sqlite3.Error:
        return False
    finally:
        store.close()


def evaluate_gates(
    repo_id: str, canon: str, provider: str, digest: str, *, registry_path: Path, store_path: Path
) -> GateResult:
    return GateResult(
        gate1_project_config(repo_id, canon, registry_path=registry_path),
        gate2_scripts_enabled(repo_id, canon, store_path=store_path),
        gate3_digest_active(repo_id, canon, provider, digest, store_path=store_path),
    )
