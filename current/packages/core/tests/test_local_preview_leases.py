from __future__ import annotations

import time
from pathlib import Path

from disco.core.auth import AuthSession, PreviewCapabilitySigner
from disco.core.store.sqlite import SqliteEventStore


def test_local_preview_lease_reuses_only_same_authority_and_rotates_generation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.sqlite3"
    store = SqliteEventStore(path)
    cid = "conv_a1b2c3d4owner"
    store.create_conversation(cid, owner_id="owner-a")
    ports = (19120, 19121, 19122)
    now = int(time.time())

    first = store.acquire_local_preview_lease(
        conversation_id=cid,
        owner_id="owner-a",
        target_port=8000,
        authority_id="live:generation-one",
        now=now,
        expires_at=now + 3_600,
        listener_ports=ports,
    )
    assert first is not None
    assert first.storage_reset_required is True
    assert store.complete_local_preview_storage_reset(
        first.listener_port,
        authority_id="live:generation-one",
        now=now + 1,
    )
    refreshed = store.acquire_local_preview_lease(
        conversation_id=cid,
        owner_id="owner-a",
        target_port=8000,
        authority_id="live:generation-one",
        now=now + 10,
        expires_at=now + 3_610,
        listener_ports=ports,
    )
    assert refreshed is not None
    assert refreshed.listener_port == first.listener_port
    assert refreshed.storage_reset_required is False

    rotated = store.acquire_local_preview_lease(
        conversation_id=cid,
        owner_id="owner-a",
        target_port=8000,
        authority_id="live:generation-two",
        now=now + 20,
        expires_at=now + 3_620,
        listener_ports=ports,
    )
    assert rotated is not None
    assert rotated.listener_port != first.listener_port
    assert rotated.storage_reset_required is True
    assert store.resolve_local_preview_lease(first.listener_port, now=now + 20) is None
    assert store.resolve_local_preview_lease(rotated.listener_port, now=now + 20) == rotated
    store.close()

    reopened = SqliteEventStore(path)
    assert reopened.resolve_local_preview_lease(rotated.listener_port, now=now + 20) == rotated
    reopened.close()


def test_store_startup_purges_expired_lease_but_keeps_origin_fence(tmp_path: Path) -> None:
    path = tmp_path / "startup-purge.sqlite3"
    store = SqliteEventStore(path)
    lease = store.acquire_local_preview_lease(
        conversation_id="conv_expired_lease",
        owner_id="owner-a",
        target_port=8000,
        authority_id="live:expired",
        now=0,
        expires_at=1,
        listener_ports=(19120,),
    )
    assert lease is not None
    assert store.complete_local_preview_storage_reset(
        lease.listener_port,
        authority_id="live:expired",
        now=0,
    )
    store.close()

    reopened = SqliteEventStore(path)
    lease_rows = reopened._conn.execute("SELECT COUNT(*) FROM local_preview_leases").fetchone()
    fence_rows = reopened._conn.execute(
        "SELECT authority_id FROM local_preview_origin_state WHERE listener_port = ?",
        (lease.listener_port,),
    ).fetchone()
    assert lease_rows[0] == 0
    assert fence_rows[0] == "live:expired"
    reopened.close()


def test_recycled_origin_requires_reset_for_new_conversation_and_authority(tmp_path: Path) -> None:
    store = SqliteEventStore(tmp_path / "recycled.sqlite3")
    ports = (19120, 19121)
    first_cid = "conv_a1b2c3d4first"
    second_cid = "conv_b1c2d3e4second"
    store.create_conversation(first_cid, owner_id="owner-a")
    store.create_conversation(second_cid, owner_id="owner-b")
    first = store.acquire_local_preview_lease(
        conversation_id=first_cid,
        owner_id="owner-a",
        target_port=8000,
        authority_id="live:first",
        now=100,
        expires_at=110,
        listener_ports=ports,
    )
    assert first is not None
    assert store.complete_local_preview_storage_reset(
        first.listener_port,
        authority_id="live:first",
        now=101,
    )

    second = store.acquire_local_preview_lease(
        conversation_id=second_cid,
        owner_id="owner-b",
        target_port=8000,
        authority_id="live:second",
        now=111,
        expires_at=200,
        listener_ports=(first.listener_port, ports[1]),
    )
    assert second is not None
    assert second.listener_port == first.listener_port
    assert second.storage_reset_required is True
    assert not store.complete_local_preview_storage_reset(
        second.listener_port,
        authority_id="live:first",
        now=112,
    )
    assert store.complete_local_preview_storage_reset(
        second.listener_port,
        authority_id="live:second",
        now=112,
    )
    assert store.resolve_local_preview_lease(second.listener_port, now=113) is not None
    assert not store.resolve_local_preview_lease(
        second.listener_port, now=113
    ).storage_reset_required
    store.close()


def test_explicit_release_is_owner_scoped_and_preserves_storage_reset_fence(
    tmp_path: Path,
) -> None:
    store = SqliteEventStore(tmp_path / "released.sqlite3")
    ports = (19120, 19121)
    first_cid = "conv_a1b2c3d4released"
    second_cid = "conv_b1c2d3e4replacement"
    store.create_conversation(first_cid, owner_id="owner-a")
    store.create_conversation(second_cid, owner_id="owner-b")
    first = store.acquire_local_preview_lease(
        conversation_id=first_cid,
        owner_id="owner-a",
        target_port=8000,
        authority_id="live:first",
        now=100,
        expires_at=1_000,
        listener_ports=ports,
    )
    assert first is not None
    assert store.complete_local_preview_storage_reset(
        first.listener_port,
        authority_id="live:first",
        now=101,
    )

    assert not store.release_local_preview_lease(
        conversation_id=first_cid,
        owner_id="owner-b",
    )
    assert store.resolve_local_preview_lease(first.listener_port, now=102) is not None
    assert store.release_local_preview_lease(
        conversation_id=first_cid,
        owner_id="owner-a",
    )
    assert store.resolve_local_preview_lease(first.listener_port, now=102) is None
    assert not store.release_local_preview_lease(
        conversation_id=first_cid,
        owner_id="owner-a",
    )

    replacement = store.acquire_local_preview_lease(
        conversation_id=second_cid,
        owner_id="owner-b",
        target_port=8000,
        authority_id="live:replacement",
        now=103,
        expires_at=1_000,
        listener_ports=(first.listener_port,),
    )
    assert replacement is not None
    assert replacement.listener_port == first.listener_port
    assert replacement.storage_reset_required is True
    assert not store.complete_local_preview_storage_reset(
        replacement.listener_port,
        authority_id="live:first",
        now=104,
    )
    assert store.complete_local_preview_storage_reset(
        replacement.listener_port,
        authority_id="live:replacement",
        now=104,
    )
    store.close()


def test_explicit_teardown_recycles_bounded_pool_without_cross_authority_reuse(
    tmp_path: Path,
) -> None:
    store = SqliteEventStore(tmp_path / "bounded-churn.sqlite3")
    ports = (19120, 19121)

    for index in range(100):
        cid = f"conv_churn_{index:03d}"
        authority = f"live:generation-{index:03d}"
        store.create_conversation(cid, owner_id="owner-a")
        lease = store.acquire_local_preview_lease(
            conversation_id=cid,
            owner_id="owner-a",
            target_port=8000,
            authority_id=authority,
            now=100 + index,
            expires_at=10_000,
            listener_ports=ports,
        )
        assert lease is not None
        assert lease.storage_reset_required is True
        assert store.complete_local_preview_storage_reset(
            lease.listener_port,
            authority_id=authority,
            now=100 + index,
        )
        assert store.release_local_preview_lease(
            conversation_id=cid,
            owner_id="owner-a",
        )
        assert (
            store.resolve_local_preview_lease(
                lease.listener_port,
                now=100 + index,
            )
            is None
        )

    store.close()


def test_preview_capability_authority_survives_intent_exchange_and_is_checked_early(
    tmp_path: Path,
) -> None:
    store = SqliteEventStore(tmp_path / "authority.sqlite3")
    signer = PreviewCapabilitySigner(redemption_store=store)
    session = AuthSession("owner-a", "csrf", "session", 2**31)
    intent = signer.mint_intent(
        session=session,
        conversation_id="conv_a1b2c3d4owner",
        port=8000,
        authority_id="live:generation-one",
    )

    assert (
        signer.redeem_intent(
            intent,
            cid8="a1b2c3d4",
            port=8000,
            path_scope="host",
            expected_authority_id="live:generation-two",
        )
        is None
    )
    redeemed = signer.redeem_intent(
        intent,
        cid8="a1b2c3d4",
        port=8000,
        path_scope="host",
        expected_authority_id="live:generation-one",
    )
    assert redeemed is not None
    token, _target = redeemed
    capability = signer.verify(token, cid8="a1b2c3d4", port=8000, method="GET", path="/")
    assert capability is not None
    assert capability.authority_id == "live:generation-one"
    store.close()
