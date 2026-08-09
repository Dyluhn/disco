"""Bounded HTTP transport extracted behind the disco_api compatibility facade."""

from __future__ import annotations

import asyncio
import json
import os
import urllib.parse
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import httpx  # the adapter MAY import an http client (oracle path stays disco/http-free)
from disco.core.auth import SESSION_COOKIE, validated_canonical_preview_url
from disco.core.loop.preview_target import is_managed_host_preview_port
from disco.tools.sandbox._container import NOVNC_PORT, USER_PORTS

from ._api_types import (
    _MAX_PREVIEW_HANDOFF_CHARS,
    _PREVIEW_RESET_BODY_RE,
)


def _classify_conn_error(exc: Exception) -> str:
    text = f"{type(exc).__name__} {exc}".lower()
    if "name" in text or "resolve" in text or "dns" in text:
        return "dns"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    return "connection_error"


@dataclass(frozen=True)
class _PreviewCapability:
    bootstrap_url: str
    intent: str
    isolated_url: str
    target_path: str


def _validated_preview_capability(
    body: dict[str, Any],
    *,
    base_url: str,
    conversation_id: str,
) -> _PreviewCapability | None:
    selected_port = body.get("port")
    if not isinstance(selected_port, int) or isinstance(selected_port, bool):
        return None
    if (
        body.get("transport") != "canonical"
        or body.get("target_path") != "/"
        or not isinstance(body.get("preview_authority"), str)
        or not body.get("preview_authority")
        or not (selected_port in USER_PORTS or is_managed_host_preview_port(selected_port))
        or selected_port == NOVNC_PORT
    ):
        return None
    port = selected_port
    bootstrap_url = str(body.get("bootstrap_url") or "")
    intent = str(body.get("bootstrap_intent") or "")
    isolated_url = validated_canonical_preview_url(
        base_url,
        conversation_id,
        port,
        bootstrap_url,
    )
    if not intent or isolated_url is None:
        return None
    return _PreviewCapability(
        bootstrap_url=bootstrap_url,
        intent=intent,
        isolated_url=isolated_url,
        target_path=str(body["target_path"]),
    )


async def _complete_preview_storage_reset(
    client: httpx.AsyncClient,
    capability: _PreviewCapability,
    *,
    origin: str,
    document: str,
) -> tuple[int, str, dict[str, str]] | None:
    if tuple(client.cookies.jar):
        return 502, "preview storage reset installed a cookie too early", {}
    handoff = _preview_storage_handoff(document)
    if handoff is None:
        return 502, "invalid preview storage reset response", {}
    completed = await client.post(
        capability.bootstrap_url,
        data={"handoff": handoff},
        headers={"Origin": origin},
    )
    if completed.status_code != 200:
        return completed.status_code, "preview storage handoff failed", {}
    if _safe_json(completed).get("target") != capability.target_path:
        return 502, "invalid preview storage handoff response", {}
    return None


async def _redeem_isolated_preview(
    client: httpx.AsyncClient,
    capability: _PreviewCapability,
    *,
    origin: str,
    failure_stage: list[str] | None = None,
) -> tuple[int, str, dict[str, str]]:
    redeemed = await client.post(
        capability.bootstrap_url,
        data={"intent": capability.intent},
        headers={"Origin": origin},
    )
    if redeemed.status_code != 200:
        if failure_stage is not None:
            failure_stage[:] = ["redemption"]
        return redeemed.status_code, "preview capability redemption failed", {}
    if "cookies" in redeemed.headers.get("clear-site-data", "").lower():
        failure = await _complete_preview_storage_reset(
            client,
            capability,
            origin=origin,
            document=redeemed.text,
        )
        if failure is not None:
            if failure_stage is not None:
                failure_stage[:] = ["redemption"]
            return failure
    if any(cookie.name == SESSION_COOKIE for cookie in client.cookies.jar):
        if failure_stage is not None:
            failure_stage[:] = ["redemption"]
        return 502, "preview bootstrap crossed application session", {}
    if failure_stage is not None:
        failure_stage[:] = ["fetch"]
    preview = await client.get(capability.isolated_url)
    if preview.status_code >= 400 and failure_stage is not None:
        failure_stage[:] = ["fetch"]
    return preview.status_code, preview.text, dict(preview.headers)


# ---- real HTTP/WS transport (used by the live runner; not in CI) ------------


class HttpTransport:
    """httpx + websockets transport against a running agent-server. Imported lazily
    so the deterministic test path never needs the live deps loaded.

    AUTH (2026-07-09): the agent-server now requires a paired session on every
    non-public route (the fresh-install pairing work), so the transport bootstraps
    one lazily via POST /api/auth/mint. Against the loopback-bound dev posture the
    localhost auto-pair admits a tokenless mint (Origin == base_url == localhost);
    an exposed/remote target can supply DISCO_SOAK_PAIRING_TOKEN instead. The
    session cookie + CSRF header then ride every request (and the WS connect)."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        *,
        timeout_s: float = 120.0,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout_s
        self._transport = _transport
        self._cookie: str | None = None  # "disco_session=<value>"
        self._csrf: str | None = None
        # Safe, stage-only diagnostic retained for the dossier metadata.  Never
        # retain exception text, URLs, response bodies, or capability material.
        self._preview_failure_stage_var: ContextVar[str | None] = ContextVar(
            "preview_failure_stage", default=None
        )

    @property
    def preview_failure_stage(self) -> str | None:
        """Return the stage captured by this task's most recent preview request."""

        return self._preview_failure_stage_var.get()

    def _set_preview_failure_stage(self, stage: str | None) -> None:
        self._preview_failure_stage_var.set(stage)

    async def _ensure_session(self) -> None:
        if self._cookie is not None:
            return
        import httpx

        body: dict[str, Any] = {}
        token = os.environ.get("DISCO_SOAK_PAIRING_TOKEN")
        if token:
            body["pairing_token"] = token
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            r = await client.post(
                f"{self.base_url}/api/auth/mint",
                json=body,
                headers={"Origin": self.base_url},
            )
        if r.status_code != 200:
            raise RuntimeError(
                f"soak transport could not pair with the agent-server: HTTP {r.status_code} "
                f"{r.text[:200]} — on a non-loopback target set DISCO_SOAK_PAIRING_TOKEN"
            )
        data = _safe_json(r)
        self._csrf = str(data.get("csrf_token") or "")
        cookie = r.cookies.get("disco_session")
        if not cookie:
            raise RuntimeError("mint succeeded but no disco_session cookie was set")
        self._cookie = f"disco_session={cookie}"

    def _headers(self, *, unsafe: bool) -> dict[str, str]:
        h = {"Origin": self.base_url, "Cookie": self._cookie or ""}
        if unsafe and self._csrf:
            h["X-Disco-CSRF"] = self._csrf
        return h

    def _ws_url(self, conversation_id: str) -> str:
        scheme = "wss" if self.base_url.startswith("https") else "ws"
        host = self.base_url.split("://", 1)[1]
        return f"{scheme}://{host}/ws/conversations/{conversation_id}"

    async def post_json(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        import httpx

        await self._ensure_session()
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            r = await client.post(
                f"{self.base_url}{path}", json=body, headers=self._headers(unsafe=True)
            )
            return r.status_code, _safe_json(r)

    async def patch_json(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        import httpx

        await self._ensure_session()
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            r = await client.patch(
                f"{self.base_url}{path}", json=body, headers=self._headers(unsafe=True)
            )
            return r.status_code, _safe_json(r)

    async def get_json(self, path: str) -> tuple[int, dict[str, Any]]:
        import httpx

        await self._ensure_session()
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            r = await client.get(f"{self.base_url}{path}", headers=self._headers(unsafe=False))
            return r.status_code, _safe_json(r)

    async def get_text(self, path: str) -> tuple[int, str, dict[str, str]]:
        import httpx

        await self._ensure_session()
        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            transport=self._transport,
        ) as client:
            r = await client.get(f"{self.base_url}{path}", headers=self._headers(unsafe=False))
            return r.status_code, r.text, dict(r.headers)

    async def post_file(
        self,
        path: str,
        *,
        field: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> tuple[int, dict[str, Any]]:
        await self._ensure_session()
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            r = await client.post(
                f"{self.base_url}{path}",
                files={field: (filename, content, content_type)},
                headers=self._headers(unsafe=True),
            )
            return r.status_code, _safe_json(r)

    async def get_bytes(self, path: str) -> tuple[int, bytes, dict[str, str]]:
        await self._ensure_session()
        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            transport=self._transport,
        ) as client:
            r = await client.get(f"{self.base_url}{path}", headers=self._headers(unsafe=False))
            return r.status_code, r.content, dict(r.headers)

    async def fetch_isolated_preview(self, conversation_id: str) -> tuple[int, str, dict[str, str]]:
        """Mint/redeem the generation-bound canonical Preview without the app session.

        Capability response fields and the isolated origin are validated before
        the one-time bearer is redeemed.  Redemption and generated-content fetch
        use a fresh cookie jar, redirects and environment proxies are disabled,
        and an application session cookie in that jar fails closed.
        """

        self._set_preview_failure_stage(None)
        await self._ensure_session()
        failure_stage = ["mint"]
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as app_client:
                minted = await app_client.post(
                    f"{self.base_url}/conversations/{conversation_id}/preview/capability",
                    json={
                        "target_path": "/",
                        "transport": "canonical",
                    },
                    headers=self._headers(unsafe=True),
                )
            if minted.status_code != 200:
                self._set_preview_failure_stage("mint")
                return minted.status_code, minted.text, dict(minted.headers)
            failure_stage[:] = ["validation"]
            capability = _validated_preview_capability(
                _safe_json(minted),
                base_url=self.base_url,
                conversation_id=conversation_id,
            )
            if capability is None:
                self._set_preview_failure_stage("validation")
                return 502, "invalid preview capability response", {}

            failure_stage[:] = ["redemption"]
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as preview_client:
                status, text, headers = await _redeem_isolated_preview(
                    preview_client,
                    capability,
                    origin=self.base_url,
                    failure_stage=failure_stage,
                )
            self._set_preview_failure_stage(
                failure_stage[0] if status >= 400 and failure_stage else None
            )
            return status, text, headers
        except (httpx.HTTPError, OSError, ValueError):
            # Post-create boundary failure: retain a non-success status for the
            # output oracle, never a local snapshot PASS and never the bearer.
            self._set_preview_failure_stage(failure_stage[0])
            return 599, "isolated preview request failed", {}

    async def ws_control(self, conversation_id: str, frame: dict[str, Any]) -> None:
        import websockets

        await self._ensure_session()
        url = self._ws_url(conversation_id)
        # websockets>=12 renamed extra_headers → additional_headers.
        headers = [("Origin", self.base_url), ("Cookie", self._cookie or "")]
        async with websockets.connect(
            url, open_timeout=self._timeout, additional_headers=headers
        ) as ws:
            # Drain the initial state frame the server sends on connect, then send
            # the control frame. We do NOT wait for an ack — truth is the event log.
            try:
                await asyncio.wait_for(ws.recv(), timeout=self._timeout)
            except TimeoutError:
                pass
            await ws.send(json.dumps(frame))
            # Give the server a beat to process before the socket closes.
            await asyncio.sleep(0.2)

    async def health(self) -> tuple[int, dict[str, Any]]:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            r = await client.get(f"{self.base_url}/health")
            return r.status_code, _safe_json(r)


def _preview_storage_handoff(document: str) -> str | None:
    """Extract the body-only handoff from the product's locked reset document."""

    match = _PREVIEW_RESET_BODY_RE.search(document)
    if match is None:
        return None
    try:
        encoded = json.loads(match.group(1))
        parsed = urllib.parse.parse_qs(
            encoded,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=2,
        )
    except (TypeError, ValueError):
        return None
    if set(parsed) != {"handoff"} or len(parsed["handoff"]) != 1:
        return None
    handoff = parsed["handoff"][0]
    return handoff if 0 < len(handoff) <= _MAX_PREVIEW_HANDOFF_CHARS else None


def _safe_json(response: Any) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:  # noqa: BLE001 — non-JSON body
        return {}
    return data if isinstance(data, dict) else {"_": data}
