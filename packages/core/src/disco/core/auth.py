"""Shared local-session signing primitives.

The HTTP/ASGI adapters live in the server packages; this module stays framework
free so core remains the bottom layer. Tokens are signed, not encrypted: they
carry only owner/session metadata and CSRF material, never provider secrets.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import time
from dataclasses import dataclass
from typing import Any

from .env import disco_env
from .store.sqlite import DEFAULT_OWNER_ID

SESSION_COOKIE = "disco_session"
CSRF_HEADER = "X-Disco-CSRF"
PREVIEW_COOKIE = "disco_preview_cap"
PREVIEW_BOOTSTRAP_PATH = "/__disco/preview-auth"

_DEFAULT_SESSION_TTL_S = 12 * 60 * 60
_DEFAULT_PREVIEW_TTL_S = 15 * 60
_DEFAULT_INTENT_TTL_S = 30
_DEV_SECRET = secrets.token_urlsafe(48)
_DEFAULT_ALLOWED_ORIGINS = (
    "http://localhost",
    "http://127.0.0.1",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:8800",
    "http://127.0.0.1:8800",
)


@dataclass(frozen=True)
class AuthSession:
    owner_id: str
    csrf_token: str
    session_id: str
    expires_at: int
    is_admin: bool = False


@dataclass(frozen=True)
class PreviewCapability:
    owner_id: str
    conversation_id: str
    port: int
    method: str
    path_prefix: str
    expires_at: int


def allowed_frontend_origins() -> tuple[str, ...]:
    raw = disco_env("FRONTEND_ORIGINS", "")
    if not raw:
        return _DEFAULT_ALLOWED_ORIGINS
    origins = [part.strip().rstrip("/") for part in raw.replace(",", " ").split()]
    return tuple(origin for origin in origins if origin)


def origin_allowed(origin: str | None) -> bool:
    return bool(origin and origin.rstrip("/") in allowed_frontend_origins())


def session_secret() -> str:
    configured = disco_env("AUTH_SECRET") or disco_env("SECRET_KEY")
    if configured:
        return configured
    return _load_or_create_install_secret()


def _load_or_create_install_secret() -> str:
    explicit = disco_env("AUTH_SECRET_FILE", "")
    data_dir = disco_env("DATA_DIR", "")
    path = (
        Path(explicit).expanduser()
        if explicit
        else Path(data_dir or "~/.local/share/disco").expanduser() / "auth_secret"
    )
    try:
        if path.exists():
            secret = path.read_text(encoding="utf-8").strip()
            if secret:
                return secret
        path.parent.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_urlsafe(48)
        path.write_text(secret + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return secret
    except OSError:
        return _DEV_SECRET


def configured_admin_token() -> str | None:
    token = disco_env("ADMIN_TOKEN")
    return token.strip() if token and token.strip() else None


def session_ttl_s() -> int:
    raw = disco_env("AUTH_SESSION_TTL_S", str(_DEFAULT_SESSION_TTL_S))
    try:
        return max(60, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_SESSION_TTL_S


def preview_ttl_s() -> int:
    raw = disco_env("AUTH_PREVIEW_TTL_S", str(_DEFAULT_PREVIEW_TTL_S))
    try:
        return max(30, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_PREVIEW_TTL_S


def intent_ttl_s() -> int:
    raw = disco_env("AUTH_PREVIEW_INTENT_TTL_S", str(_DEFAULT_INTENT_TTL_S))
    try:
        return max(5, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_INTENT_TTL_S


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(data: str) -> bytes:
    padded = data + ("=" * (-len(data) % 4))
    return base64.urlsafe_b64decode(padded.encode("ascii"))


class SignedTokenCodec:
    def __init__(self, secret: str | None = None) -> None:
        self._secret = (secret or session_secret()).encode("utf-8")

    def sign(self, payload: dict[str, Any]) -> str:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        body_b64 = _b64(body)
        sig = hmac.new(self._secret, body_b64.encode("ascii"), hashlib.sha256).digest()
        return f"{body_b64}.{_b64(sig)}"

    def unsign(self, token: str) -> dict[str, Any] | None:
        try:
            body_b64, sig_b64 = token.split(".", 1)
            expected = hmac.new(
                self._secret, body_b64.encode("ascii"), hashlib.sha256
            ).digest()
            if not hmac.compare_digest(expected, _unb64(sig_b64)):
                return None
            payload = json.loads(_unb64(body_b64).decode("utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        exp = payload.get("exp")
        if not isinstance(exp, int) or exp < int(time.time()):
            return None
        return payload


class SessionSigner:
    def __init__(self, codec: SignedTokenCodec | None = None) -> None:
        self._codec = codec or SignedTokenCodec()

    def mint(self, *, owner_id: str = DEFAULT_OWNER_ID, is_admin: bool = False) -> tuple[str, AuthSession]:
        now = int(time.time())
        session = AuthSession(
            owner_id=(owner_id.strip() or DEFAULT_OWNER_ID),
            csrf_token=secrets.token_urlsafe(32),
            session_id=secrets.token_urlsafe(24),
            expires_at=now + session_ttl_s(),
            is_admin=is_admin,
        )
        token = self._codec.sign(
            {
                "v": 1,
                "kind": "session",
                "owner": session.owner_id,
                "csrf": session.csrf_token,
                "sid": session.session_id,
                "admin": session.is_admin,
                "exp": session.expires_at,
            }
        )
        return token, session

    def verify(self, token: str | None) -> AuthSession | None:
        if not token:
            return None
        payload = self._codec.unsign(token)
        if payload is None or payload.get("kind") != "session":
            return None
        owner = payload.get("owner")
        csrf = payload.get("csrf")
        sid = payload.get("sid")
        exp = payload.get("exp")
        if not isinstance(owner, str) or not owner:
            return None
        if not isinstance(csrf, str) or not csrf:
            return None
        if not isinstance(sid, str) or not sid:
            return None
        if not isinstance(exp, int):
            return None
        return AuthSession(
            owner_id=owner,
            csrf_token=csrf,
            session_id=sid,
            expires_at=exp,
            is_admin=bool(payload.get("admin")),
        )

    @staticmethod
    def csrf_valid(session: AuthSession, presented: str | None) -> bool:
        return bool(
            presented
            and hmac.compare_digest(session.csrf_token, presented.strip())
        )


class PreviewCapabilitySigner:
    def __init__(self, codec: SignedTokenCodec | None = None) -> None:
        self._codec = codec or SignedTokenCodec()

    def mint_intent(
        self, *, session: AuthSession, conversation_id: str, port: int, target_path: str = "/"
    ) -> str:
        now = int(time.time())
        return self._codec.sign(
            {
                "v": 1,
                "kind": "preview_intent",
                "owner": session.owner_id,
                "cid": conversation_id,
                "port": int(port),
                "method": "WEBSOCKET" if target_path.startswith("ws:") else "GET",
                "prefix": "/",
                "target": target_path if target_path.startswith("/") else f"/{target_path}",
                "jti": secrets.token_urlsafe(18),
                "exp": now + intent_ttl_s(),
            }
        )

    def mint_cookie_from_intent(self, intent: str) -> tuple[str, str] | None:
        payload = self._codec.unsign(intent)
        if payload is None or payload.get("kind") != "preview_intent":
            return None
        cap = self._cap_from_payload(payload, kind="preview_intent")
        target = payload.get("target")
        if cap is None or not isinstance(target, str) or not target.startswith("/"):
            return None
        token = self._codec.sign(
            {
                "v": 1,
                "kind": "preview_cap",
                "owner": cap.owner_id,
                "cid": cap.conversation_id,
                "port": cap.port,
                "method": cap.method,
                "prefix": cap.path_prefix,
                "exp": int(time.time()) + preview_ttl_s(),
            }
        )
        return token, target

    def verify(
        self,
        token: str | None,
        *,
        cid8: str,
        port: int,
        method: str,
        path: str,
    ) -> PreviewCapability | None:
        payload = self._codec.unsign(token or "")
        cap = self._cap_from_payload(payload, kind="preview_cap") if payload else None
        if cap is None:
            return None
        if cap.conversation_id.removeprefix("conv_")[:8] != cid8:
            return None
        if cap.port != int(port) or cap.method != method.upper():
            return None
        if not path.startswith(cap.path_prefix):
            return None
        return cap

    @staticmethod
    def _cap_from_payload(payload: dict[str, Any], *, kind: str) -> PreviewCapability | None:
        if payload.get("kind") != kind:
            return None
        owner = payload.get("owner")
        cid = payload.get("cid")
        port = payload.get("port")
        method = payload.get("method")
        prefix = payload.get("prefix")
        exp = payload.get("exp")
        if not isinstance(owner, str) or not isinstance(cid, str):
            return None
        if not isinstance(method, str) or not isinstance(prefix, str):
            return None
        if not isinstance(port, int) or not isinstance(exp, int):
            return None
        return PreviewCapability(owner, cid, port, method.upper(), prefix, exp)
