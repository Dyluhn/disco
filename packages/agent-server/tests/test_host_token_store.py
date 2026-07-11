"""Unit tests for the WO-A2.2 durable host-service token store."""

from __future__ import annotations

import secrets
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from disco.agent_server.host_token_store import HostTokenStore, TokenStoreClosed


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "tokens.db"
    with HostTokenStore(path) as s:
        yield s


@pytest.fixture
def in_memory_store():
    with HostTokenStore(":memory:") as s:
        yield s


def _parts(token: str) -> tuple[str, str, str]:
    parts = token.split(".")
    assert len(parts) == 3
    return parts[0], parts[1], parts[2]


def test_mint_returns_opaque_split_token(store):
    token = store.mint("conv_1", "owner_a", "app:demo")
    prefix, selector, verifier = _parts(token)
    assert prefix == "a4v1"
    assert selector
    assert verifier
    # Verifier is 32 random bytes encoded urlsafe-base64 (43 chars).
    assert len(verifier) == 43


def test_verify_valid_token(store):
    token = store.mint("conv_1", "owner_a", "app:demo")
    record = store.verify(token)
    assert record is not None
    assert record.conversation_id == "conv_1"
    assert record.owner_id == "owner_a"
    assert record.audience == "app:demo"
    assert record.version == 1
    assert record.is_active is True


def test_verify_malformed_token(store):
    assert store.verify("") is None
    assert store.verify("a2v0.short") is None
    assert store.verify("a2v0..verifier") is None
    assert store.verify("wrongprefix.selector.verifier") is None
    assert store.verify("a2v0.selector!not_b64") is None


def test_verify_unknown_selector(store):
    # Same prefix/verifier shape, but the selector was never minted.
    token = "a2v0.AAAAAAAAAAAAAAAAAAAAAA.BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    assert store.verify(token) is None


def test_verify_wrong_verifier(store):
    token = store.mint("conv_1", "owner_a", "app:demo")
    prefix, selector, _verifier = _parts(token)
    # Keep the same selector but replace the verifier with a fresh random one.
    wrong = f"{prefix}.{selector}.CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    assert store.verify(wrong) is None


def test_verify_revoked_token(store):
    token = store.mint("conv_1", "owner_a", "app:demo")
    record = store.verify(token)
    assert record is not None
    assert store.revoke(record.selector) is True
    assert store.verify(token) is None


def test_revoke_idempotent(store):
    token = store.mint("conv_1", "owner_a", "app:demo")
    record = store.verify(token)
    assert store.revoke(record.selector) is True
    assert store.revoke(record.selector) is False


def test_verify_expired_token(store):
    token = store.mint(
        "conv_1",
        "owner_a",
        "app:demo",
        expires_in=timedelta(seconds=-1),
    )
    assert store.verify(token) is None


def test_revoke_for_conversation(store):
    t1 = store.mint("conv_1", "owner_a", "app:demo")
    t2 = store.mint("conv_1", "owner_a", "app:other")
    t3 = store.mint("conv_2", "owner_b", "app:demo")
    assert store.revoke_for_conversation("conv_1") == 2
    assert store.verify(t1) is None
    assert store.verify(t2) is None
    assert store.verify(t3) is not None


def test_rotation_keeps_old_until_candidate_is_confirmed(store):
    old = store.mint("conv_1", "owner_a", "app:demo")
    new = store.rotate("conv_1", "owner_a", "app:demo")
    assert new != old
    assert store.verify(old) is not None
    assert store.verify(new) is not None
    assert store.verify(new).generation == 1
    new_selector = store.verify(new).selector
    assert store.finish_rotation("conv_1", "app:demo", keep_selector=new_selector) == 1
    assert store.verify(old) is None
    # Rotation is scoped to (conversation, audience).
    other = store.mint("conv_1", "owner_a", "app:other")
    assert store.verify(other) is not None


def test_restart_durability(tmp_path):
    path = tmp_path / "tokens.db"
    token = HostTokenStore(path).mint("conv_1", "owner_a", "app:demo")

    store2 = HostTokenStore(path)
    record = store2.verify(token)
    assert record is not None
    assert record.conversation_id == "conv_1"
    store2.close()


def test_schema_migration_accepts_persisted_active_a2v0_token(tmp_path):
    path = tmp_path / "legacy.db"
    selector = "A" * 22
    verifier = "B" * 43
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE host_service_tokens (
            selector TEXT PRIMARY KEY, verifier_digest BLOB NOT NULL,
            conversation_id TEXT NOT NULL, owner_id TEXT NOT NULL, audience TEXT NOT NULL,
            allowed_services TEXT NOT NULL, allowed_origins TEXT NOT NULL, kind TEXT NOT NULL,
            generation INTEGER NOT NULL, created_at TEXT NOT NULL, expires_at TEXT, revoked_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO host_service_tokens VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
        (
            selector,
            HostTokenStore._digest(verifier),
            "conv_1",
            "owner_a",
            "app:demo",
            '["svc.ping"]',
            "[]",
            "deployed",
            7,
            "2026-07-11T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    with HostTokenStore(path) as migrated:
        record = migrated.verify(f"a2v0.{selector}.{verifier}")
        assert record is not None
        assert record.version == 0
        assert record.generation == 7
        assert migrated.mint("conv_1", "owner_a", "app:new").startswith("a4v1.")


def test_wire_version_must_match_persisted_version(store):
    token = store.mint("conv_1", "owner_a", "app:demo")
    _prefix, selector, verifier = _parts(token)
    assert store.verify(f"a2v0.{selector}.{verifier}") is None


def test_no_plaintext_verifier_in_database(store):
    token = store.mint("conv_1", "owner_a", "app:demo")
    _prefix, selector, verifier = _parts(token)
    digest = HostTokenStore._digest(verifier)

    conn = sqlite3.connect(store.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM host_service_tokens WHERE selector = ?", (selector,)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["verifier_digest"] == digest
    assert verifier.encode("ascii") not in row["verifier_digest"]
    assert verifier not in str(row)


def test_metadata_round_trip(store):
    origins = frozenset({"https://example.com", "https://app.example.com"})
    token = store.mint(
        "conv_1",
        "owner_a",
        "app:demo",
        allowed_origins=origins,
        expires_in=timedelta(hours=1),
    )
    record = store.verify(token)
    assert record is not None
    assert record.allowed_origins == origins
    assert record.expires_at is not None
    assert record.revoked_at is None


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.com",
        "https://user@example.com",
        "https://example.com/path",
        "https://example.com?query=1",
        "not-a-url",
    ],
)
def test_mint_rejects_non_origin_or_plaintext_remote_return_origin(store, origin):
    with pytest.raises(ValueError, match="return origin"):
        store.mint(
            "conv_1",
            "owner_a",
            "app:demo",
            allowed_origins=frozenset({origin}),
        )


def test_mint_rejects_invalid_service_scope(store):
    with pytest.raises(ValueError, match="invalid service"):
        store.mint(
            "conv_1",
            "owner_a",
            "app:demo",
            allowed_services=frozenset({"Svc-Ping"}),
        )


def test_rotation_generation_is_automatic_monotonic_and_owner_scoped(store):
    initial = store.mint("conv_1", "owner_a", "app:demo", generation=40)
    rotated = store.rotate("conv_1", "owner_a", "app:demo")
    assert store.verify(initial).generation == 40
    assert store.verify(rotated).generation == 41
    next_rotated = store.rotate("conv_1", "owner_a", "app:demo")
    assert store.verify(next_rotated).generation == 42
    other_owner = store.rotate("conv_1", "owner_b", "app:demo")
    assert store.verify(other_owner).generation == 0


def test_finish_rotation_does_not_revoke_other_owner(store):
    owner_a_old = store.mint("conv_1", "owner_a", "app:demo")
    owner_b = store.mint("conv_1", "owner_b", "app:demo")
    owner_a_new = store.rotate("conv_1", "owner_a", "app:demo")
    selector = store.verify(owner_a_new).selector
    assert store.finish_rotation("conv_1", "app:demo", keep_selector=selector) == 1
    assert store.verify(owner_a_old) is None
    assert store.verify(owner_b) is not None


def test_unknown_selector_uses_digest_comparison(store, monkeypatch):
    calls = 0
    real_compare = secrets.compare_digest

    def observed_compare(left, right):
        nonlocal calls
        calls += 1
        return real_compare(left, right)

    monkeypatch.setattr("secrets.compare_digest", observed_compare)
    unknown = "a4v1.AAAAAAAAAAAAAAAAAAAAAA.BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    assert store.verify(unknown) is None
    assert calls == 1


def test_concurrent_rotations_allocate_distinct_increasing_generations(tmp_path):
    path = tmp_path / "generation.db"
    first = HostTokenStore(path)
    second = HostTokenStore(path)
    first.mint("conv_1", "owner_a", "app:demo")
    barrier = threading.Barrier(2)

    def rotate(store):
        barrier.wait()
        token = store.rotate("conv_1", "owner_a", "app:demo")
        return store.verify(token).generation

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(rotate, first), pool.submit(rotate, second)]
        generations = sorted(future.result() for future in futures)
    assert generations == [1, 2]
    first.close()
    second.close()


def test_concurrent_rotation_cannot_revoke_both_candidates(tmp_path):
    path = tmp_path / "rotation.db"
    first = HostTokenStore(path)
    second = HostTokenStore(path)
    token_a = first.mint("conv_1", "owner_a", "app:demo")
    token_b = first.rotate("conv_1", "owner_a", "app:demo")
    selector_a = first.verify(token_a).selector
    selector_b = first.verify(token_b).selector
    barrier = threading.Barrier(2)

    def finish(store, selector):
        barrier.wait()
        return store.finish_rotation("conv_1", "app:demo", keep_selector=selector)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(finish, first, selector_a),
            pool.submit(finish, second, selector_b),
        ]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except ValueError:
                outcomes.append("lost")
    active = [record for record in first.list_for_conversation("conv_1") if record.is_active]
    assert len(active) == 1
    assert outcomes.count("lost") == 1
    first.close()
    second.close()


def test_list_for_conversation(store):
    store.mint("conv_1", "owner_a", "app:demo")
    t2 = store.rotate("conv_1", "owner_a", "app:demo")
    store.finish_rotation("conv_1", "app:demo", keep_selector=store.verify(t2).selector)
    records = store.list_for_conversation("conv_1")
    assert len(records) == 2
    assert all(r.conversation_id == "conv_1" for r in records)
    by_token = {r.selector: r for r in records}
    assert by_token[store.verify(t2).selector].revoked_at is None
    # t1 was revoked by rotate; verify returns None, but the record still exists.
    revoked_record = next(r for r in records if r.selector != store.verify(t2).selector)
    assert revoked_record.revoked_at is not None


def test_closed_store_raises(store):
    store.close()
    with pytest.raises(TokenStoreClosed):
        store.mint("conv_1", "owner_a", "app:demo")
    with pytest.raises(TokenStoreClosed):
        store.verify("a2v0.x.y")
