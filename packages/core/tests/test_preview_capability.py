from __future__ import annotations

import hashlib
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Empty
from typing import Any

import pytest
from disco.core.auth import (
    ISOLATED_PATH_PREVIEW_PREFIX,
    PATH_PREVIEW_ORIGIN_DIGEST_HEX_CHARS,
    PREVIEW_APP_HTTP_METHODS,
    SESSION_COOKIE,
    AuthSession,
    PreviewCapabilitySigner,
    SessionSigner,
    cookie_header_from_headers,
    cookie_header_values,
    local_preview_origin_crosses_host,
    path_preview_cookie_name,
    path_preview_host_label,
)
from disco.core.store.sqlite import SqliteEventStore

_PATH_PREVIEW_HOST = path_preview_host_label("conv_a1b2c3d4selected", 8000)


def _session() -> AuthSession:
    return AuthSession("owner-a", "csrf", "session-a", 2**31)


class _RepeatedCookieHeaders:
    def __init__(self, *values: str) -> None:
        self._values = values

    def getlist(self, name: str) -> list[str]:
        return list(self._values) if name.lower() == "cookie" else []

    def get(self, name: str) -> str | None:
        return self._values[0] if name.lower() == "cookie" and self._values else None


def _redeem_intent_process(
    db_path: str,
    intent: str,
    secret: str,
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    """Spawn-safe worker for the shared-SQLite redemption boundary."""

    os.environ["DISCO_AUTH_SECRET"] = secret
    store = SqliteEventStore(db_path)
    try:
        ready.put(True)
        if not start.wait(20):
            return
        redeemed = PreviewCapabilitySigner(redemption_store=store).redeem_intent(
            intent,
            cid8="feedface",
            port=8000,
            path_scope="host",
        )
        results.put(redeemed is not None)
    finally:
        store.close()


def test_cookie_candidates_preserve_duplicate_order_and_repeated_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISCO_AUTH_SECRET", "duplicate-cookie-candidate-secret")
    session_signer = SessionSigner()
    session_token, session = session_signer.mint(owner_id="owner-a")
    for cookie_header in (
        f"{SESSION_COOKIE}=invalid; {SESSION_COOKIE}={session_token}",
        f"{SESSION_COOKIE}={session_token}; {SESSION_COOKIE}=invalid",
    ):
        assert cookie_header_values(cookie_header, SESSION_COOKIE) in (
            ("invalid", session_token),
            (session_token, "invalid"),
        )
        assert session_signer.verify_cookie_header(cookie_header) == session

    repeated = cookie_header_from_headers(
        _RepeatedCookieHeaders(
            f"{SESSION_COOKIE}=invalid",
            f"other=1; {SESSION_COOKIE}={session_token}",
        )
    )
    assert repeated == f"{SESSION_COOKIE}=invalid; other=1; {SESSION_COOKIE}={session_token}"
    assert session_signer.verify_cookie_header(repeated) == session

    store = SqliteEventStore(tmp_path / "cookie-candidates.sqlite3")
    capability_signer = PreviewCapabilitySigner(redemption_store=store)
    intent = capability_signer.mint_intent(
        session=session,
        conversation_id="conv_a1b2c3d4owner",
        port=8000,
        target_path="/",
    )
    redeemed = capability_signer.redeem_intent(
        intent, cid8="a1b2c3d4", port=8000, path_scope="host"
    )
    assert redeemed is not None
    capability_token, _target = redeemed
    cookie_name = path_preview_cookie_name("a1b2c3d4")
    for cookie_header in (
        f"{cookie_name}=invalid; {cookie_name}={capability_token}",
        f"{cookie_name}={capability_token}; {cookie_name}=invalid",
    ):
        assert (
            capability_signer.verify_cookie_header(
                cookie_header,
                cookie_name,
                cid8="a1b2c3d4",
                port=8000,
                method="GET",
                path="/",
            )
            is not None
        )
    store.close()


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
        (f"http://{_PATH_PREVIEW_HOST}.localhost:8088", "127.0.0.1:8000"),
        (f"http://{_PATH_PREVIEW_HOST}.ui.localhost:8088", "ui.localhost:8000"),
        ("http://p2-a1b2c3d4-8000.localhost:8088", "localhost:8000"),
        ("http://p2-a1b2c3d4-8000.ui.localhost:8088", "ui.localhost:8000"),
        ("http://a1b2c3d4-8000.localhost:8088", "localhost:8000"),
        ("http://a1b2c3d4-8000.ui.localhost:8088", "ui.localhost:8000"),
    ],
)
def test_local_preview_origin_cannot_escape_host_only_quarantine(origin: str, host: str) -> None:
    assert local_preview_origin_crosses_host(origin, host)


@pytest.mark.parametrize(
    ("origin", "host"),
    [
        ("http://localhost:5173", "localhost:8000"),
        ("http://127.0.0.1:5173", "127.0.0.1:8000"),
        ("http://[::1]:5173", "[::1]:8000"),
        ("https://ui.example", "api.example"),
        ("http://ui.localhost:5173", "localhost:8000"),
        ("http://p3s-a1b2c3d4-short-8000.localhost:5173", "localhost:8000"),
        ("http://p2-nothex123-8000.localhost:5173", "localhost:8000"),
        ("http://a1b2c3d4-1.ui.localhost:5173", "ui.localhost:8000"),
        ("http://p2-a1b2c3d4-0.ui.localhost:5173", "ui.localhost:8000"),
        ("http://p2-a1b2c3d4-99999.ui.localhost:5173", "ui.localhost:8000"),
        (
            f"http://{_PATH_PREVIEW_HOST.rsplit('-', 1)[0]}-123456.ui.localhost:5173",
            "ui.localhost:8000",
        ),
        (None, "localhost:8000"),
    ],
)
def test_normal_same_host_split_ui_is_not_mistaken_for_preview_escape(
    origin: str | None, host: str
) -> None:
    assert not local_preview_origin_crosses_host(origin, host)


def test_path_preview_host_label_binds_full_conversation_identity() -> None:
    first_cid = "conv_a1b2c3d4first"
    first = path_preview_host_label(first_cid, 8000)
    second = path_preview_host_label("conv_a1b2c3d4second", 8000)
    assert first != second
    assert first.startswith("p3s-a1b2c3d4-")
    assert second.startswith("p3s-a1b2c3d4-")
    assert (
        first.split("-")[2]
        == hashlib.sha256(first_cid.encode()).hexdigest()[:PATH_PREVIEW_ORIGIN_DIGEST_HEX_CHARS]
    )
    assert len(first) <= 63
    assert len(second) <= 63


@pytest.mark.parametrize(
    ("conversation_id", "port"),
    [
        ("conv_short", 8000),
        ("conv_nothexzzowner", 8000),
        ("conv_a1b2c3d4owner", 0),
        ("conv_a1b2c3d4owner", 65536),
    ],
)
def test_path_preview_host_label_rejects_invalid_identity_or_port(
    conversation_id: str, port: int
) -> None:
    with pytest.raises(ValueError):
        path_preview_host_label(conversation_id, port)


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
    assert signer.redeem_intent(intent, cid8="deadbeef", port=8000, path_scope="host") is None
    redeemed = signer.redeem_intent(
        intent,
        cid8="deadbeef",
        port=8000,
        path_scope="static",
        request_host_label=path_preview_host_label("conv_deadbeefowner", 8000),
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
            intent,
            cid8="deadbeef",
            port=8000,
            path_scope="static",
            request_host_label=path_preview_host_label("conv_deadbeefowner", 8000),
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


def test_only_one_spawned_process_can_redeem_shared_sqlite_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "cross-process-preview-redemption-secret"
    monkeypatch.setenv("DISCO_AUTH_SECRET", secret)
    db = tmp_path / "events.sqlite3"
    mint_store = SqliteEventStore(db)
    intent = PreviewCapabilitySigner(redemption_store=mint_store).mint_intent(
        session=_session(),
        conversation_id="conv_feedfaceowner",
        port=8000,
        target_path="/",
    )
    mint_store.close()

    context = multiprocessing.get_context("spawn")
    start = context.Event()
    ready = context.Queue()
    results = context.Queue()
    workers = [
        context.Process(
            target=_redeem_intent_process,
            args=(str(db), intent, secret, ready, start, results),
        )
        for _ in range(2)
    ]
    started_workers: list[multiprocessing.Process] = []
    try:
        try:
            for worker in workers:
                worker.start()
                started_workers.append(worker)
            try:
                readiness = [ready.get(timeout=20) for _ in workers]
            except Empty as exc:
                raise AssertionError("spawned redemption worker missed the ready fence") from exc
            assert all(readiness)
            start.set()
            for worker in workers:
                worker.join(20)
            assert not [worker for worker in workers if worker.is_alive()], (
                "spawned preview redemption worker hung"
            )
            assert all(worker.exitcode == 0 for worker in workers)
            try:
                outcomes = [results.get(timeout=5) for _ in workers]
            except Empty as exc:
                raise AssertionError("spawned redemption worker omitted its result") from exc
        finally:
            start.set()
            for worker in started_workers:
                if worker.is_alive():
                    worker.terminate()
            for worker in started_workers:
                worker.join(5)
                if worker.is_alive():
                    worker.kill()
                    worker.join(5)
            for worker in started_workers:
                worker.close()
    finally:
        for queue in (ready, results):
            queue.close()
            queue.join_thread()
    assert sum(outcomes) == 1


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
    redeemed = signer.redeem_intent(intent, cid8="a1b2c3d4", port=8000, path_scope="host")
    assert redeemed is not None
    token, _ = redeemed
    assert signer.verify(token, cid8="a1b2c3d4", port=8000, method="WEBSOCKET", path="/hmr")
    assert signer.verify(token, cid8="deadbeef", port=8000, method="WEBSOCKET", path="/hmr") is None
    assert signer.verify(token, cid8="a1b2c3d4", port=8899, method="WEBSOCKET", path="/hmr") is None
    for method in PREVIEW_APP_HTTP_METHODS:
        assert signer.verify(token, cid8="a1b2c3d4", port=8000, method=method, path="/hmr")
    assert signer.verify(token, cid8="a1b2c3d4", port=8000, method="TRACE", path="/hmr") is None

    static_prefix = f"{ISOLATED_PATH_PREVIEW_PREFIX}/conv_a1b2c3d4owner/"
    static_intent = signer.mint_intent(
        session=_session(),
        conversation_id="conv_a1b2c3d4owner",
        port=8000,
        target_path=f"{static_prefix}hmr",
        path_prefix=static_prefix,
    )
    static_redeemed = signer.redeem_intent(
        static_intent,
        cid8="a1b2c3d4",
        port=8000,
        path_scope="static",
        request_host_label=path_preview_host_label("conv_a1b2c3d4owner", 8000),
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
