from __future__ import annotations

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

    first = store.acquire_local_preview_lease(
        conversation_id=cid,
        owner_id="owner-a",
        target_port=8000,
        authority_id="live:generation-one",
        now=100,
        expires_at=200,
        listener_ports=ports,
    )
    assert first is not None
    assert first.storage_reset_required is True
    assert store.complete_local_preview_storage_reset(
        first.listener_port,
        authority_id="live:generation-one",
        now=101,
    )
    refreshed = store.acquire_local_preview_lease(
        conversation_id=cid,
        owner_id="owner-a",
        target_port=8000,
        authority_id="live:generation-one",
        now=110,
        expires_at=220,
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
        now=120,
        expires_at=230,
        listener_ports=ports,
    )
    assert rotated is not None
    assert rotated.listener_port != first.listener_port
    assert rotated.storage_reset_required is True
    assert store.resolve_local_preview_lease(first.listener_port, now=120) is None
    assert store.resolve_local_preview_lease(rotated.listener_port, now=120) == rotated
    store.close()

    reopened = SqliteEventStore(path)
    assert reopened.resolve_local_preview_lease(rotated.listener_port, now=120) == rotated
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
