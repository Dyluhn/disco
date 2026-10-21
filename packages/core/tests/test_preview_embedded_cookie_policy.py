"""The embed cookie policy is a signed capability claim, separate from grants."""

from __future__ import annotations

from dataclasses import replace

import pytest
from disco.core.auth import AuthSession, PreviewCapabilitySigner, SignedTokenCodec
from disco.core.store.sqlite import SqliteEventStore


def _signer():
    store = SqliteEventStore(":memory:")
    codec = SignedTokenCodec("embedded-cookie-test-secret")
    return store, codec, PreviewCapabilitySigner(codec=codec, redemption_store=store)


def _intent(signer):
    return signer.mint_intent(
        session=AuthSession("owner", "csrf", "session", 2**31),
        conversation_id="conv_aaaaaaaaowner",
        port=8000,
        target_path="/app/",
        path_prefix="/",
        http_methods=("GET", "POST"),
        allow_websocket=True,
        authority_id="sealed-runtime:" + "a" * 64,
        capture_generation=17,
    )


def _verify(signer, token):
    return signer.verify(token, cid8="aaaaaaaa", port=8000, method="GET", path="/app/")


@pytest.mark.parametrize("embedded", [False, True])
def test_cookie_policy_survives_redeem_and_handoff_without_changing_grants(embedded):
    store, _codec, signer = _signer()
    try:
        redeemed = signer.redeem_intent(
            _intent(signer),
            cid8="aaaaaaaa",
            port=8000,
            path_scope="host",
            partitioned_cookies=embedded,
        )
        assert redeemed is not None
        token, target = redeemed
        cap = _verify(signer, token)
        assert cap is not None and cap.partitioned_cookies is embedded
        assert (
            cap.owner_id,
            cap.conversation_id,
            cap.port,
            cap.http_methods,
            cap.path_prefix,
            cap.allow_websocket,
            cap.authority_id,
            cap.capture_generation,
        ) == (
            "owner",
            "conv_aaaaaaaaowner",
            8000,
            ("GET", "POST"),
            "/",
            True,
            "sealed-runtime:" + "a" * 64,
            17,
        )
        handoff = signer.mint_storage_handoff(cap, target, partitioned=embedded)
        completed = signer.redeem_storage_handoff(
            handoff,
            cid8="aaaaaaaa",
            port=8000,
            expected_conversation_id=cap.conversation_id,
            expected_owner_id=cap.owner_id,
            expected_authority_id=cap.authority_id,
        )
        assert completed is not None and completed[1:] == (target, embedded)
        final = _verify(signer, completed[0])
        assert final is not None and replace(final, expires_at=cap.expires_at) == cap
        for changed in ({"port": 5173}, {"cid8": "bbbbbbbb"}, {"method": "DELETE"}):
            fields = {"cid8": "aaaaaaaa", "port": 8000, "method": "GET", "path": "/app/", **changed}
            assert signer.verify(completed[0], **fields) is None
    finally:
        store.close()


def test_old_cookie_claim_defaults_to_passthrough_and_tampering_is_rejected():
    store, codec, signer = _signer()
    try:
        redeemed = signer.redeem_intent(
            _intent(signer), cid8="aaaaaaaa", port=8000, path_scope="host"
        )
        assert redeemed is not None
        payload = codec.unsign(redeemed[0])
        assert payload is not None
        payload.pop("partitioned")
        old_token = codec.sign(payload)
        old_cap = _verify(signer, old_token)
        assert old_cap is not None and old_cap.partitioned_cookies is False
        payload["partitioned"] = True
        changed_body = codec.sign(payload).split(".")[0]
        original_signature = old_token.split(".")[1]
        assert _verify(signer, changed_body + "." + original_signature) is None
    finally:
        store.close()


@pytest.mark.parametrize("invalid", [None, 0, 1, "true", "false", {}, []])
def test_cookie_policy_requires_boolean_in_signed_payload_and_redemption(invalid):
    store, codec, signer = _signer()
    try:
        intent = _intent(signer)
        assert (
            signer.redeem_intent(
                intent, cid8="aaaaaaaa", port=8000, path_scope="host", partitioned_cookies=invalid
            )
            is None
        )
        redeemed = signer.redeem_intent(intent, cid8="aaaaaaaa", port=8000, path_scope="host")
        assert redeemed is not None  # Bad policy did not consume one-use intent.
        payload = codec.unsign(redeemed[0])
        assert payload is not None
        payload["partitioned"] = invalid
        assert _verify(signer, codec.sign(payload)) is None
    finally:
        store.close()
