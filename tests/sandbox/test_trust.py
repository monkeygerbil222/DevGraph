"""The script trust store (spec §5.1, §5.6): fail closed, retire, cap, revoke."""

import os
import sqlite3
import stat

import pytest

from devgraph.sandbox import gates, limits, paths
from devgraph.sandbox.trust import Approval, TrustStore, TrustStoreError

REPO = "acme"
CANON = "/srv/code/acme"
PROVIDER = "routes"
DIGESTS = [f"{n:064x}" for n in range(1, 9)]


def _approve(store, digest, *, keep_previous=False, canon=CANON, provider=PROVIDER):
    return store.approve(
        REPO, canon, provider, digest,
        declaration_json='{"name":"routes"}', script_text="def derive(ctx):\n    return []\n",
        matched_count=3, keep_previous=keep_previous,
    )


@pytest.fixture
def store_path():
    return paths.trust_store_path(paths.sandbox_home())


@pytest.fixture
def store(store_path):
    s = TrustStore.open_write(store_path)
    yield s
    s.close()


def _gate2(store_path, canon=CANON):
    return gates.gate2_scripts_enabled(REPO, canon, store_path=store_path)


def _gate3(store_path, digest, canon=CANON):
    return gates.gate3_digest_active(REPO, canon, PROVIDER, digest, store_path=store_path)


def test_open_write_creates_private_store_with_schema_version(store_path, store):
    assert stat.S_IMODE(os.stat(store_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store_path.parent).st_mode) == 0o700
    conn = sqlite3.connect(store_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == limits.TRUST_SCHEMA_VERSION
    finally:
        conn.close()


def test_enable_and_approve_round_trip(store_path, store):
    store.set_scripts_enabled(REPO, CANON, True)
    approval = _approve(store, DIGESTS[0])
    assert isinstance(approval, Approval)
    assert approval.state == "active"
    assert approval.approved_at.endswith("+00:00")
    assert approval.declaration_json == '{"name":"routes"}'
    assert approval.matched_count == 3
    assert _gate2(store_path) and _gate3(store_path, DIGESTS[0])
    store.set_scripts_enabled(REPO, CANON, False)
    assert not _gate2(store_path)


def test_trust_store_fails_closed(store_path, tmp_path):
    # Missing file.
    assert TrustStore.open_read(store_path) is None
    assert not _gate2(store_path) and not _gate3(store_path, DIGESTS[0])

    with TrustStore.open_write(store_path) as s:
        s.set_scripts_enabled(REPO, CANON, True)
        _approve(s, DIGESTS[0])
    assert _gate2(store_path) and _gate3(store_path, DIGESTS[0])

    # Moved repository: same id, different canonical path.
    assert not _gate2(store_path, canon="/srv/code/acme-moved")
    assert not _gate3(store_path, DIGESTS[0], canon="/srv/code/acme-moved")

    # Locked by an exclusive transaction.
    locker = sqlite3.connect(store_path, isolation_level=None)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        assert TrustStore.open_read(store_path) is None
        assert not _gate2(store_path) and not _gate3(store_path, DIGESTS[0])
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    assert _gate2(store_path)

    # Wrong schema version.
    conn = sqlite3.connect(store_path)
    conn.execute(f"PRAGMA user_version = {limits.TRUST_SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    assert TrustStore.open_read(store_path) is None
    assert not _gate2(store_path) and not _gate3(store_path, DIGESTS[0])
    with pytest.raises(TrustStoreError):
        TrustStore.open_write(store_path)

    # Corrupt file.
    store_path.write_bytes(b"this is not a sqlite database" * 100)
    assert TrustStore.open_read(store_path) is None
    assert not _gate2(store_path) and not _gate3(store_path, DIGESTS[0])
    with pytest.raises(TrustStoreError):
        TrustStore.open_write(store_path)


def test_right_version_without_tables_fails_closed(store_path):
    store_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(store_path)
    conn.execute(f"PRAGMA user_version = {limits.TRUST_SCHEMA_VERSION}")
    conn.commit()
    conn.close()
    assert not _gate2(store_path) and not _gate3(store_path, DIGESTS[0])


def test_approve_retires_previous_digests(store_path, store):
    _approve(store, DIGESTS[0])
    _approve(store, DIGESTS[1], keep_previous=True)
    _approve(store, DIGESTS[2])
    assert [a.digest for a in store.active_digests(REPO, CANON, PROVIDER)] == [DIGESTS[2]]
    states = {a.digest: a.state for a in store.approvals(REPO, CANON, PROVIDER)}
    assert states == {DIGESTS[0]: "retired", DIGESTS[1]: "retired", DIGESTS[2]: "active"}
    # Another provider and another checkout are untouched.
    _approve(store, DIGESTS[3], provider="other")
    _approve(store, DIGESTS[4], canon="/srv/code/acme-2")
    _approve(store, DIGESTS[5])
    assert [a.digest for a in store.active_digests(REPO, CANON, "other")] == [DIGESTS[3]]
    assert [a.digest for a in store.active_digests(REPO, "/srv/code/acme-2", PROVIDER)] == [DIGESTS[4]]


def test_keep_previous_caps_at_five(store_path, store):
    for digest in DIGESTS[:7]:
        _approve(store, digest, keep_previous=True)
    active = [a.digest for a in store.active_digests(REPO, CANON, PROVIDER)]
    assert len(active) == limits.MAX_ACTIVE_DIGESTS == 5
    assert active == DIGESTS[2:7]  # the two oldest were retired
    assert {a.digest for a in store.approvals(REPO, CANON, PROVIDER) if a.state == "retired"} == set(DIGESTS[:2])


def test_retired_digest_reprompts(store_path, store):
    _approve(store, DIGESTS[0])
    _approve(store, DIGESTS[1])
    assert not _gate3(store_path, DIGESTS[0])
    assert _gate3(store_path, DIGESTS[1])
    # Only a fresh approval reactivates it.
    again = _approve(store, DIGESTS[0])
    assert again.state == "active"
    assert _gate3(store_path, DIGESTS[0]) and not _gate3(store_path, DIGESTS[1])


def test_revoke_stops_active_digest(store_path, store):
    _approve(store, DIGESTS[0])
    _approve(store, DIGESTS[1], keep_previous=True)
    assert store.revoke(REPO, CANON, PROVIDER, DIGESTS[1]) == 1
    assert not _gate3(store_path, DIGESTS[1])
    assert _gate3(store_path, DIGESTS[0])
    assert [a.digest for a in store.approvals(REPO, CANON, PROVIDER)] == [DIGESTS[0]]
    # Without a digest, every digest of the provider is removed.
    _approve(store, DIGESTS[2], keep_previous=True)
    assert store.revoke(REPO, CANON, PROVIDER, None) == 2
    assert store.approvals(REPO, CANON, PROVIDER) == []
    assert store.revoke(REPO, CANON, PROVIDER, None) == 0


def test_forget_repo_removes_every_row_for_the_id(store_path, store):
    store.set_scripts_enabled(REPO, CANON, True)
    _approve(store, DIGESTS[0])
    _approve(store, DIGESTS[1], canon="/srv/code/acme-2")
    store.set_scripts_enabled("other-repo", "/srv/code/other", True)
    assert store.forget_repo(REPO) == 3
    assert not _gate2(store_path) and not _gate3(store_path, DIGESTS[0])
    assert store.scripts_enabled("other-repo", "/srv/code/other")


# --- fix round 1: ownership, modes, no-follow, read-only, empty store ---------


def _good_store(store_path):
    with TrustStore.open_write(store_path) as s:
        s.set_scripts_enabled(REPO, CANON, True)
        _approve(s, DIGESTS[0])
    assert _gate2(store_path) and _gate3(store_path, DIGESTS[0])


def test_normal_modes_work(store_path):
    _good_store(store_path)
    assert stat.S_IMODE(os.stat(store_path.parent).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(store_path).st_mode) == 0o600


def test_world_writable_parent_turns_gates_off(store_path):
    _good_store(store_path)
    os.chmod(store_path.parent, 0o777)
    try:
        assert TrustStore.open_read(store_path) is None
        assert not _gate2(store_path) and not _gate3(store_path, DIGESTS[0])
        with pytest.raises(TrustStoreError):
            TrustStore.open_write(store_path)
    finally:
        os.chmod(store_path.parent, 0o700)
    assert _gate2(store_path)


def test_group_or_world_writable_store_turns_gates_off(store_path):
    _good_store(store_path)
    for mode in (0o666, 0o620):
        os.chmod(store_path, mode)
        assert TrustStore.open_read(store_path) is None
        assert not _gate2(store_path) and not _gate3(store_path, DIGESTS[0])
    os.chmod(store_path, 0o600)
    assert _gate2(store_path)


def test_open_write_refuses_writable_store_and_never_widens(store_path):
    _good_store(store_path)
    os.chmod(store_path, 0o666)
    with pytest.raises(TrustStoreError):
        TrustStore.open_write(store_path)
    os.chmod(store_path, 0o640)  # not writable by others: accepted and tightened
    TrustStore.open_write(store_path).close()
    assert stat.S_IMODE(os.stat(store_path).st_mode) == 0o600


def test_store_owned_by_another_user_turns_gates_off(store_path, monkeypatch):
    _good_store(store_path)
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    assert TrustStore.open_read(store_path) is None
    assert not _gate2(store_path) and not _gate3(store_path, DIGESTS[0])
    with pytest.raises(TrustStoreError):
        TrustStore.open_write(store_path)


def test_symlinked_store_is_refused(store_path, tmp_path):
    real = tmp_path / "real" / "script_trust.sqlite3"
    _good_store(real)
    store_path.parent.mkdir(mode=0o700, parents=True)
    store_path.symlink_to(real)
    assert TrustStore.open_read(store_path) is None
    assert not _gate2(store_path) and not _gate3(store_path, DIGESTS[0])
    with pytest.raises(TrustStoreError):
        TrustStore.open_write(store_path)


def test_open_write_on_empty_version_one_store_raises_trust_store_error(store_path):
    store_path.parent.mkdir(mode=0o700, parents=True)
    conn = sqlite3.connect(store_path)
    conn.execute(f"PRAGMA user_version = {limits.TRUST_SCHEMA_VERSION}")
    conn.commit()
    conn.close()
    os.chmod(store_path, 0o600)
    with pytest.raises(TrustStoreError):
        TrustStore.open_write(store_path)


def test_open_read_connection_is_read_only(store_path):
    _good_store(store_path)
    store = TrustStore.open_read(store_path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            store._conn.execute("DELETE FROM approvals")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            store.set_scripts_enabled(REPO, CANON, False)
    finally:
        store.close()
    assert _gate2(store_path) and _gate3(store_path, DIGESTS[0])


def test_non_utf8_canonical_path_fails_closed(store_path):
    _good_store(store_path)
    bad = "/srv/code/caf\udcff"
    assert not _gate2(store_path, canon=bad)
    assert not _gate3(store_path, DIGESTS[0], canon=bad)
    assert not gates.gate2_scripts_enabled("caf\udcff", CANON, store_path=store_path)
