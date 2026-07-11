"""Unit tests for the WO-A2.2 durable host-service token store."""

from __future__ import annotations

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
    assert prefix == "a2v0"
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
