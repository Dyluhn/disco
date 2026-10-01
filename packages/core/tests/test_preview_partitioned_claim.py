from __future__ import annotations

import time

import pytest
from disco.core.auth import (
    ISOLATED_PATH_PREVIEW_PREFIX,
    AuthSession,
    PreviewCapability,
    PreviewCapabilitySigner,
    SignedTokenCodec,
    path_preview_host_label,
)
from disco.core.store.sqlite import SqliteEventStore

_SECRET = "ordinary-hermetic-test-secret-at-least32bytes"
_OWNER = "owner_7f3a9c2e1b4d6a8f2c5e"
_CID = "conv_9d3f7a1c5e8b42f0a6d4c9e2b7a1f0c3"
_PORT = 18731
_AUTH = "auth_4f7a2c9e1b8d653a0c7e5f2a"


def _setup():
    codec = SignedTokenCodec(secret=_SECRET)
    store = SqliteEventStore(":memory:")
    signer = PreviewCapabilitySigner(codec=codec, redemption_store=store)
    return codec, store, signer


def _session(owner: str = _OWNER) -> AuthSession:
    return AuthSession(
        owner, "csrf_8f2a4c9e1b7d653a0", "sess_3c9e1b7d2a4f653a0", int(time.time()) + 3600
    )


def _cid8(cid: str = _CID) -> str:
    return cid.removeprefix("conv_")[:8]


@pytest.mark.parametrize("partitioned", [True, False])
def test_intent_redeem_verify_preserves_policy_and_rights(partitioned: bool) -> None:
    _, store, signer = _setup()
    try:
        intent = signer.mint_intent(
            session=_session(),
            conversation_id=_CID,
            port=_PORT,
            target_path="/",
            path_prefix="/",
            http_methods=("GET",),
            authority_id=_AUTH,
        )
        out = signer.redeem_intent(
            intent,
            cid8=_cid8(),
            port=_PORT,
            path_scope="host",
            expected_conversation_id=_CID,
            expected_owner_id=_OWNER,
            expected_authority_id=_AUTH,
            partitioned_cookies=partitioned,
        )
        assert out is not None
        token, _target = out
        cap = signer.verify(token, cid8=_cid8(), port=_PORT, method="GET", path="/")
        assert cap is not None
        assert cap.partitioned_cookies is partitioned
        assert isinstance(cap.partitioned_cookies, bool)
        assert (cap.owner_id, cap.conversation_id, cap.authority_id) == (_OWNER, _CID, _AUTH)
        assert cap.port == _PORT and cap.path_prefix == "/" and "GET" in cap.http_methods
    finally:
        store.close()


def test_cap_legacy_missing_partitioned_accepted_as_false() -> None:
    codec, store, signer = _setup()
    try:
        intent = signer.mint_intent(
            session=_session(),
            conversation_id=_CID,
            port=_PORT,
            target_path="/",
            path_prefix="/",
            http_methods=("GET",),
            authority_id=_AUTH,
        )
        out = signer.redeem_intent(
            intent,
            cid8=_cid8(),
            port=_PORT,
            path_scope="host",
            expected_conversation_id=_CID,
            expected_owner_id=_OWNER,
            expected_authority_id=_AUTH,
            partitioned_cookies=True,
        )
        assert out is not None
        payload = codec.unsign(out[0])
        assert payload is not None
        keys = [k for k in payload if "partition" in k.lower()]
        assert keys
        legacy = codec.sign({k: v for k, v in payload.items() if k not in keys})
        cap = signer.verify(legacy, cid8=_cid8(), port=_PORT, method="GET", path="/")
        assert cap is not None
        assert cap.partitioned_cookies is False
    finally:
        store.close()


@pytest.mark.parametrize("bad", [None, 0, 1, "true", {}, []])
def test_cap_malformed_partitioned_rejected(bad) -> None:  # type: ignore[no-untyped-def]
    codec, store, signer = _setup()
    try:
        intent = signer.mint_intent(
            session=_session(),
            conversation_id=_CID,
            port=_PORT,
            target_path="/",
            path_prefix="/",
            http_methods=("GET",),
            authority_id=_AUTH,
        )
        out = signer.redeem_intent(
            intent,
            cid8=_cid8(),
            port=_PORT,
            path_scope="host",
            expected_conversation_id=_CID,
            expected_owner_id=_OWNER,
            expected_authority_id=_AUTH,
            partitioned_cookies=True,
        )
        assert out is not None
        payload = codec.unsign(out[0])
        assert payload is not None
        keys = [k for k in payload if "partition" in k.lower()]
        assert keys
        tampered = dict(payload)
        for k in keys:
            tampered[k] = bad
        assert (
            signer.verify(codec.sign(tampered), cid8=_cid8(), port=_PORT, method="GET", path="/")
            is None
        )
    finally:
        store.close()


def test_invalid_partitioned_does_not_consume_intent() -> None:
    _, store, signer = _setup()
    try:
        intent = signer.mint_intent(
            session=_session(),
            conversation_id=_CID,
            port=_PORT,
            target_path="/",
            path_prefix="/",
            http_methods=("GET",),
            authority_id=_AUTH,
        )
        kwargs = dict(
            cid8=_cid8(),
            port=_PORT,
            path_scope="host",
            expected_conversation_id=_CID,
            expected_owner_id=_OWNER,
            expected_authority_id=_AUTH,
        )
        assert signer.redeem_intent(intent, partitioned_cookies=None, **kwargs) is None  # type: ignore[arg-type]
        out = signer.redeem_intent(intent, partitioned_cookies=True, **kwargs)
        assert out is not None
        assert signer.redeem_intent(intent, partitioned_cookies=True, **kwargs) is None
    finally:
        store.close()


@pytest.mark.parametrize("partitioned", [True, False])
def test_storage_handoff_persists_partitioned(partitioned: bool) -> None:
    codec, store, signer = _setup()
    try:
        cap0 = PreviewCapability(
            owner_id=_OWNER,
            conversation_id=_CID,
            port=_PORT,
            http_methods=("GET",),
            path_prefix="/",
            expires_at=int(time.time()) + 3600,
            authority_id=_AUTH,
        )
        handoff = signer.mint_storage_handoff(cap0, "/", partitioned=partitioned)
        payload = codec.unsign(handoff)
        assert payload is not None and payload.get("partitioned") is partitioned
        legacy_payload = dict(payload)
        legacy_payload.pop("partitioned_cookies", None)
        legacy_handoff = codec.sign(legacy_payload)
        out = signer.redeem_storage_handoff(
            legacy_handoff,
            cid8=_cid8(),
            port=_PORT,
            expected_conversation_id=_CID,
            expected_owner_id=_OWNER,
            expected_authority_id=_AUTH,
        )
        assert out is not None
        token, _target, echoed = out
        assert echoed is partitioned
        cap = signer.verify(token, cid8=_cid8(), port=_PORT, method="GET", path="/")
        assert cap is not None and cap.partitioned_cookies is partitioned
        assert (cap.owner_id, cap.conversation_id, cap.authority_id) == (_OWNER, _CID, _AUTH)
        assert (
            signer.redeem_storage_handoff(
                legacy_handoff,
                cid8=_cid8(),
                port=_PORT,
                expected_conversation_id=_CID,
                expected_owner_id=_OWNER,
                expected_authority_id=_AUTH,
            )
            is None
        )
    finally:
        store.close()


def test_policy_true_cap_rejects_mismatched_cid_method_path() -> None:
    _, store, signer = _setup()
    prefix = f"{ISOLATED_PATH_PREVIEW_PREFIX}/{_CID}/"
    try:
        intent = signer.mint_intent(
            session=_session(),
            conversation_id=_CID,
            port=_PORT,
            target_path=prefix + "view",
            path_prefix=prefix,
            http_methods=("GET",),
            authority_id=_AUTH,
        )
        out = signer.redeem_intent(
            intent,
            cid8=_cid8(),
            port=_PORT,
            path_scope="static",
            request_host_label=path_preview_host_label(_CID, _PORT),
            expected_conversation_id=_CID,
            expected_owner_id=_OWNER,
            expected_authority_id=_AUTH,
            partitioned_cookies=True,
        )
        assert out is not None
        token, _target = out
        assert (
            signer.verify(token, cid8=_cid8(), port=_PORT, method="GET", path=prefix + "view")
            is not None
        )
        assert (
            signer.verify(token, cid8="00000000", port=_PORT, method="GET", path=prefix + "view")
            is None
        )
        assert (
            signer.verify(token, cid8=_cid8(), port=_PORT, method="POST", path=prefix + "view")
            is None
        )
        assert signer.verify(token, cid8=_cid8(), port=_PORT, method="GET", path="/other") is None
    finally:
        store.close()
