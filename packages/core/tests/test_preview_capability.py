from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from disco.core.auth import (
    ISOLATED_PATH_PREVIEW_PREFIX,
    PREVIEW_APP_HTTP_METHODS,
    AuthSession,
    PreviewCapabilitySigner,
    local_preview_origin_crosses_host,
)
from disco.core.store.sqlite import SqliteEventStore


def _session() -> AuthSession:
    return AuthSession("owner-a", "csrf", "session-a", 2**31)


@pytest.mark.parametrize(
    ("origin", "host"),
    [
        ("http://localhost:8088", "127.0.0.1:8000"),
        ("http://127.0.0.1:8088", "localhost:8000"),
        ("http://127.0.0.2:8088", "127.0.0.1:8000"),
        ("http://[::1]:8088", "localhost:8000"),
        ("http://localhost:8088", "lvh.me:8000"),
        ("http://localhost:8088", "localhost.:8000"),
        ("http://127.0.0.1:8088", "127.0.0.1.:8000"),
    ],
)
def test_local_preview_origin_cannot_escape_host_only_quarantine(
    origin: str, host: str
) -> None:
    assert local_preview_origin_crosses_host(origin, host)


@pytest.mark.parametrize(
    ("origin", "host"),
    [
        ("http://localhost:5173", "localhost:8000"),
        ("http://127.0.0.1:5173", "127.0.0.1:8000"),
        ("http://[::1]:5173", "[::1]:8000"),
        ("https://ui.example", "api.example"),
        (None, "localhost:8000"),
    ],
)
def test_normal_same_host_split_ui_is_not_mistaken_for_preview_escape(
    origin: str | None, host: str
) -> None:
    assert not local_preview_origin_crosses_host(origin, host)


def test_preview_intent_survives_restart_and_wrong_route_does_not_burn_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISCO_AUTH_SECRET", "durable-preview-redemption-secret")
    db = tmp_path / "events.sqlite3"
    first = SqliteEventStore(db)
    intent = PreviewCapabilitySigner(redemption_store=first).mint_intent(
        session=_session(),
        conversation_id="conv_deadbeefowner",
        port=8000,
        target_path=f"{ISOLATED_PATH_PREVIEW_PREFIX}/conv_deadbeefowner/",
        path_prefix=f"{ISOLATED_PATH_PREVIEW_PREFIX}/conv_deadbeefowner/",
    )
    first.close()

    restarted = SqliteEventStore(db)
    signer = PreviewCapabilitySigner(redemption_store=restarted)
    assert signer.redeem_intent(
        intent, cid8="deadbeef", port=8000, path_scope="host"
    ) is None
    redeemed = signer.redeem_intent(
        intent, cid8="deadbeef", port=8000, path_scope="static"
    )
    assert redeemed is not None
    token, target = redeemed
    assert target == f"{ISOLATED_PATH_PREVIEW_PREFIX}/conv_deadbeefowner/"
    assert (
        signer.verify(
            token,
            cid8="deadbeef",
            port=8000,
            method="GET",
            path=target,
        )
        is not None
    )
    restarted.close()

    reopened = SqliteEventStore(db)
    assert (
        PreviewCapabilitySigner(redemption_store=reopened).redeem_intent(
            intent, cid8="deadbeef", port=8000, path_scope="static"
        )
        is None
    )
    reopened.close()


def test_only_one_sqlite_connection_can_redeem_an_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISCO_AUTH_SECRET", "concurrent-preview-redemption-secret")
    db = tmp_path / "events.sqlite3"
    mint_store = SqliteEventStore(db)
    intent = PreviewCapabilitySigner(redemption_store=mint_store).mint_intent(
        session=_session(),
        conversation_id="conv_cafebabeowner",
        port=8899,
        target_path="/",
    )
    mint_store.close()

    stores = [SqliteEventStore(db), SqliteEventStore(db)]
    signers = [PreviewCapabilitySigner(redemption_store=store) for store in stores]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda signer: signer.redeem_intent(
                    intent, cid8="cafebabe", port=8899, path_scope="host"
                ),
                signers,
            )
        )
    assert sum(result is not None for result in results) == 1
    for store in stores:
        store.close()


def test_preview_get_capability_allows_only_matching_websocket_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISCO_AUTH_SECRET", "preview-websocket-secret")
    store = SqliteEventStore(tmp_path / "events.sqlite3")
    signer = PreviewCapabilitySigner(redemption_store=store)
    intent = signer.mint_intent(
        session=_session(),
        conversation_id="conv_a1b2c3d4owner",
        port=8000,
        target_path="/hmr",
        allow_websocket=True,
        http_methods=PREVIEW_APP_HTTP_METHODS,
    )
    redeemed = signer.redeem_intent(
        intent, cid8="a1b2c3d4", port=8000, path_scope="host"
    )
    assert redeemed is not None
    token, _ = redeemed
    assert signer.verify(
        token, cid8="a1b2c3d4", port=8000, method="WEBSOCKET", path="/hmr"
    )
    assert signer.verify(
        token, cid8="deadbeef", port=8000, method="WEBSOCKET", path="/hmr"
    ) is None
    assert signer.verify(
        token, cid8="a1b2c3d4", port=8899, method="WEBSOCKET", path="/hmr"
    ) is None
    for method in PREVIEW_APP_HTTP_METHODS:
        assert signer.verify(
            token, cid8="a1b2c3d4", port=8000, method=method, path="/hmr"
        )
    assert signer.verify(
        token, cid8="a1b2c3d4", port=8000, method="TRACE", path="/hmr"
    ) is None

    static_prefix = f"{ISOLATED_PATH_PREVIEW_PREFIX}/conv_a1b2c3d4owner/"
    static_intent = signer.mint_intent(
        session=_session(),
        conversation_id="conv_a1b2c3d4owner",
        port=8000,
        target_path=f"{static_prefix}hmr",
        path_prefix=static_prefix,
    )
    static_redeemed = signer.redeem_intent(
        static_intent, cid8="a1b2c3d4", port=8000, path_scope="static"
    )
    assert static_redeemed is not None
    static_token, _ = static_redeemed
    assert signer.verify(
        static_token,
        cid8="a1b2c3d4",
        port=8000,
        method="GET",
        path=f"{static_prefix}hmr",
    )
    assert (
        signer.verify(
            static_token,
            cid8="a1b2c3d4",
            port=8000,
            method="WEBSOCKET",
            path=f"{static_prefix}hmr",
        )
        is None
    )
    assert (
        signer.verify(
            static_token,
            cid8="a1b2c3d4",
            port=8000,
            method="GET",
            path="/outside-static-prefix",
        )
        is None
    )
    store.close()
