"""API session and driver configuration for the fresh-device runner."""

from __future__ import annotations

import http.cookiejar
import json
import os
import urllib.error
import urllib.request
from typing import Any

from .fresh_device_host import ProductError


class ApiSession:
    def __init__(self, *, app_base: str, agent_base: str, origin: str) -> None:
        self.app_base = app_base.rstrip("/")
        self.agent_base = agent_base.rstrip("/")
        self.origin = origin.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = ""

    def use_packaged_front_door(self, front: str) -> None:
        """Retarget an established host-only session after the source upgrade."""
        front = front.rstrip("/")
        self.app_base = f"{front}/svc/app"
        self.agent_base = f"{front}/svc/agent"
        self.origin = front

    def _request(
        self,
        method: str,
        url: str,
        *,
        data: Any | None = None,
        timeout: float = 60,
    ) -> tuple[int, bytes, dict[str, str]]:
        body = None if data is None else json.dumps(data).encode("utf-8")
        headers = {"Accept": "application/json", "Origin": self.origin}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if method in {"POST", "PUT", "PATCH", "DELETE"} and self.csrf:
            headers["X-Disco-CSRF"] = self.csrf
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return response.status, response.read(), dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers.items())

    def pair(self) -> None:
        status, raw, _ = self._request("POST", f"{self.app_base}/api/auth/mint", data={})
        if status == 401:
            token_status, token_raw, _ = self._request(
                "GET", f"{self.app_base}/api/auth/pairing-token"
            )
            if token_status != 200:
                raise ProductError(
                    "pairing is required and the pairing-token endpoint returned "
                    f"HTTP {token_status}"
                )
            token = str(json.loads(token_raw).get("pairing_token") or "")
            status, raw, _ = self._request(
                "POST", f"{self.app_base}/api/auth/mint", data={"pairing_token": token}
            )
        if status != 200:
            raise ProductError(f"first pairing returned HTTP {status}: {raw[:500]!r}")
        self.csrf = str(json.loads(raw).get("csrf_token") or "")
        if not self.csrf or not any(cookie.name == "disco_session" for cookie in self.jar):
            raise ProductError("pairing returned no CSRF token/session cookie")

    def json(self, method: str, base: str, path: str, *, data: Any | None = None) -> Any:
        status, raw, _ = self._request(method, f"{base.rstrip('/')}{path}", data=data)
        if not 200 <= status < 300:
            raise ProductError(f"{method} {path} returned HTTP {status}: {raw[:800]!r}")
        return json.loads(raw) if raw else None

    def bytes(self, base: str, path: str) -> bytes:
        status, raw, _ = self._request("GET", f"{base.rstrip('/')}{path}", timeout=120)
        if status != 200:
            raise ProductError(f"GET {path} returned HTTP {status}: {raw[:800]!r}")
        return raw


def _configure_driver(session: ApiSession, *, base_url: str, model: str, api_key: str) -> None:
    model_id = "fresh-device-driver"
    key_name = model_id
    if api_key:
        session.json(
            "PUT",
            session.app_base,
            f"/api/secrets/{key_name}",
            data={"value": api_key},
        )
    payload = {
        "id": model_id,
        "model_id": model,
        "base_url": base_url,
        "api_key_env": key_name if api_key else None,
        "context_window": int(os.environ.get("DISCO_FRESH_DRIVER_CONTEXT", "32768")),
        "quantization": None,
        "capabilities": ["tool_calling", "json_mode", "long_context"],
        "price_in_per_m": 0,
        "price_out_per_m": 0,
        "pricing_mode": "unknown",
    }
    status, raw, _ = session._request("POST", f"{session.app_base}/api/models", data=payload)
    if status == 400:
        status, raw, _ = session._request(
            "PUT", f"{session.app_base}/api/models/{model_id}", data=payload
        )
    if not 200 <= status < 300:
        raise ProductError(f"driver model configuration failed HTTP {status}: {raw[:800]!r}")
    session.json(
        "PUT",
        session.app_base,
        "/api/models/assignments",
        data={
            "default_model": model_id,
            "roles": {
                "rag_answerer": model_id,
                "query_rewriter": model_id,
                "summarizer": model_id,
            },
        },
    )
