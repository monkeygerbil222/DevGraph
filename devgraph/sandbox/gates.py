"""The three script gates (spec §5.2), each failing closed.

1. Project config is on, read from the registry at its fixed default path
   (never through `Settings` or `project_switch`, which fails open).
2. Scripts are enabled for `(repo_id, canonical path)` in the trust store.
3. The provider's current digest is approved and active.

Every function takes its file paths as arguments; callers pass
`paths.fixed_registry_path(paths.sandbox_home())` and
`paths.trust_store_path(paths.sandbox_home())`. Any error means False, and is logged.
A store or registry inside the repository being decided reads off: a repository
(a dotfiles repository at the home directory, say) cannot vouch for itself.
"""

from __future__ import annotations

import logging
import sqlite3
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from devgraph.sandbox.paths import inside_repo, private_file_stat, same_file
from devgraph.sandbox.trust import TrustStore

logger = logging.getLogger(__name__)

_REGISTRY_TIMEOUT = 0.5


class GateResult(NamedTuple):
    project_config: bool
    scripts_enabled: bool
    digest_active: bool


def gate1_project_config(repo_id: str, canon: str, *, registry_path: Path) -> bool:
    """True only for exactly one registry row with this id whose stored path, NFC-normalised,
    equals `canon` and whose `project_config_enabled` is 1. The stored path is not re-resolved.
    The registry must be private to the user, not a symlink, and outside the repository."""
    conn = None
    try:
        registry_path = Path(registry_path)
        if not registry_path.exists() or inside_repo(registry_path, canon):
            return False
        before = private_file_stat(registry_path)
        conn = sqlite3.connect(f"{registry_path.parent.resolve().joinpath(registry_path.name).as_uri()}?mode=ro",
                               uri=True, timeout=_REGISTRY_TIMEOUT)
        rows = conn.execute(
            "SELECT path, project_config_enabled FROM repos WHERE repo_id = ?", (repo_id,)
        ).fetchall()
        if not same_file(before, private_file_stat(registry_path)) or len(rows) != 1:
            return False
        path, enabled = rows[0]
        return isinstance(path, str) and unicodedata.normalize("NFC", path) == canon and enabled == 1
    except Exception as exc:  # any error deciding means off
        logger.warning("script gate 1 (project config) reads off for %r: %r", repo_id, exc)
        return False
    finally:
        if conn is not None:
            conn.close()


def _store_gate(name: str, repo_id: str, canon: str, store_path: Path, read: Callable[[TrustStore], bool]) -> bool:
    store = None
    try:
        if inside_repo(Path(store_path), canon):
            return False
        store = TrustStore.open_read(store_path)
        return store is not None and read(store)
    except Exception as exc:  # any error deciding means off
        logger.warning("script gate %s reads off for %r: %r", name, repo_id, exc)
        return False
    finally:
        if store is not None:
            store.close()


def gate2_scripts_enabled(repo_id: str, canon: str, *, store_path: Path) -> bool:
    return _store_gate("2 (scripts enabled)", repo_id, canon, store_path,
                       lambda store: store.scripts_enabled(repo_id, canon))


def gate3_digest_active(repo_id: str, canon: str, provider: str, digest: str, *, store_path: Path) -> bool:
    return _store_gate("3 (digest active)", repo_id, canon, store_path,
                       lambda store: any(a.digest == digest for a in store.active_digests(repo_id, canon, provider)))


def evaluate_gates(
    repo_id: str, canon: str, provider: str, digest: str, *, registry_path: Path, store_path: Path
) -> GateResult:
    return GateResult(
        gate1_project_config(repo_id, canon, registry_path=registry_path),
        gate2_scripts_enabled(repo_id, canon, store_path=store_path),
        gate3_digest_active(repo_id, canon, provider, digest, store_path=store_path),
    )
