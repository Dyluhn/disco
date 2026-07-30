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
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from ._auth_origin_policy import _PREVIEW_HTTP_METHODS
from ._auth_origin_policy import (
    DEFAULT_LOCAL_PREVIEW_PORT_COUNT as DEFAULT_LOCAL_PREVIEW_PORT_COUNT,
)
from ._auth_origin_policy import (
    DEFAULT_LOCAL_PREVIEW_PORT_START as DEFAULT_LOCAL_PREVIEW_PORT_START,
)
from ._auth_origin_policy import (
    ISOLATED_PATH_PREVIEW_PREFIX as ISOLATED_PATH_PREVIEW_PREFIX,
)
from ._auth_origin_policy import (
    LOCAL_PREVIEW_COOKIE_PREFIX as LOCAL_PREVIEW_COOKIE_PREFIX,
)
from ._auth_origin_policy import (
    MAX_PREVIEW_TARGET_PATH_CHARS as MAX_PREVIEW_TARGET_PATH_CHARS,
)
from ._auth_origin_policy import (
    PATH_PREVIEW_BOOTSTRAP_PATH as PATH_PREVIEW_BOOTSTRAP_PATH,
)
from ._auth_origin_policy import (
    PATH_PREVIEW_HOST_PREFIX as PATH_PREVIEW_HOST_PREFIX,
)
from ._auth_origin_policy import (
    PATH_PREVIEW_ORIGIN_DIGEST_HEX_CHARS as PATH_PREVIEW_ORIGIN_DIGEST_HEX_CHARS,
)
from ._auth_origin_policy import PREVIEW_APP_HTTP_METHODS as PREVIEW_APP_HTTP_METHODS
from ._auth_origin_policy import PREVIEW_BOOTSTRAP_PATH as PREVIEW_BOOTSTRAP_PATH
from ._auth_origin_policy import PREVIEW_COOKIE as PREVIEW_COOKIE
from ._auth_origin_policy import allowed_frontend_origins as allowed_frontend_origins
from ._auth_origin_policy import cookie_header_from_headers as cookie_header_from_headers
from ._auth_origin_policy import cookie_header_values as cookie_header_values
from ._auth_origin_policy import (
    generated_preview_request_host as generated_preview_request_host,
)
from ._auth_origin_policy import (
    local_preview_cookie_name as local_preview_cookie_name,
)
from ._auth_origin_policy import (
    local_preview_gateway_host as local_preview_gateway_host,
)
from ._auth_origin_policy import (
    local_preview_gateway_ports as local_preview_gateway_ports,
)
from ._auth_origin_policy import (
    local_preview_origin_crosses_host as local_preview_origin_crosses_host,
)
from ._auth_origin_policy import (
    localhost_auto_pair_allowed as localhost_auto_pair_allowed,
)
from ._auth_origin_policy import origin_allowed as origin_allowed
from ._auth_origin_policy import (
    origin_matches_request_host as origin_matches_request_host,
)
from ._auth_origin_policy import origin_permitted as origin_permitted
from ._auth_origin_policy import (
    path_preview_cookie_name as path_preview_cookie_name,
)
from ._auth_origin_policy import path_preview_host_label as path_preview_host_label
from ._auth_origin_policy import request_traversed_proxy as request_traversed_proxy
from ._auth_origin_policy import (
    validated_canonical_preview_url as validated_canonical_preview_url,
)
from ._auth_origin_policy import (
    validated_isolated_path_preview_url as validated_isolated_path_preview_url,
)
from .env import disco_env
from .store.sqlite import DEFAULT_OWNER_ID

# Preserve the historical public function identity across the private extraction.
for _public_origin_policy in (
    allowed_frontend_origins,
    cookie_header_from_headers,
    cookie_header_values,
    generated_preview_request_host,
    local_preview_cookie_name,
    local_preview_gateway_host,
    local_preview_gateway_ports,
    local_preview_origin_crosses_host,
    localhost_auto_pair_allowed,
    origin_allowed,
    origin_matches_request_host,
    origin_permitted,
    path_preview_cookie_name,
    path_preview_host_label,
    request_traversed_proxy,
    validated_canonical_preview_url,
    validated_isolated_path_preview_url,
):
    _public_origin_policy.__module__ = __name__
del _public_origin_policy

SESSION_COOKIE = "disco_session"
CSRF_HEADER = "X-Disco-CSRF"

_DEFAULT_SESSION_TTL_S = 12 * 60 * 60
_DEFAULT_PREVIEW_TTL_S = 15 * 60
_DEFAULT_INTENT_TTL_S = 30
_DEV_SECRET = secrets.token_urlsafe(48)


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
    http_methods: tuple[str, ...]
    path_prefix: str
    expires_at: int
    allow_websocket: bool = False
    authority_id: str | None = None
    immutable_version: int | None = None


class PreviewIntentRedemptionStore(Protocol):
    """Durable atomic replay fence shared by server restarts and workers."""

    def register_preview_intent(self, jti: str, expires_at: int) -> None: ...

    def consume_preview_intent(self, jti: str, *, now: int) -> bool: ...


def session_secret() -> str:
    configured = disco_env("AUTH_SECRET") or disco_env("SECRET_KEY")
    if configured:
        return configured
    return _load_or_create_install_secret()


def pairing_token() -> str:
    """The first-run admin pairing token — DERIVED from the shared session secret,
    not random per process.

    Two properties this buys, both load-bearing for containerized self-host
    (found live 2026-07-09):

      1. IDENTICAL across the app-server and agent-server. Each used to mint its
         OWN random ``secrets.token_urlsafe`` token, so a browser pairing both
         origins would need TWO different tokens from TWO different container
         logs. Deriving from the secret both servers already share (DISCO_SECRET_KEY)
         means ONE token pairs everything — and since the session-signing secret
         is shared too, one server's cookie is already valid at the other.

      2. STABLE across restarts. The operator (who set the secret, or can read it
         from the logs) can always re-pair a new browser / cleared cookies without
         hunting a freshly-randomised token. Possession of the token is the
         operator proof; the secret is the root of trust, exactly like the session
         signer. It is NEVER the secret itself — it's an HMAC tag over a fixed
         label, so leaking the pairing token does not leak the signing secret."""
    tag = hmac.new(
        session_secret().encode("utf-8"),
        b"disco-pairing-token-v1",
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(tag).decode("ascii").rstrip("=")


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
            expected = hmac.new(self._secret, body_b64.encode("ascii"), hashlib.sha256).digest()
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

    def mint(
        self, *, owner_id: str = DEFAULT_OWNER_ID, is_admin: bool = False
    ) -> tuple[str, AuthSession]:
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

    def verify_cookie_header(self, cookie_header: str | None) -> AuthSession | None:
        """Return the first cryptographically valid session among duplicates."""

        for token in cookie_header_values(cookie_header, SESSION_COOKIE):
            session = self.verify(token)
            if session is not None:
                return session
        return None

    @staticmethod
    def csrf_valid(session: AuthSession, presented: str | None) -> bool:
        return bool(presented and hmac.compare_digest(session.csrf_token, presented.strip()))


def _validated_preview_target(target_path: str, path_prefix: str) -> tuple[str, str]:
    target = target_path if target_path.startswith("/") else f"/{target_path}"
    prefix = path_prefix if path_prefix.startswith("/") else f"/{path_prefix}"
    if len(target) > MAX_PREVIEW_TARGET_PATH_CHARS:
        raise ValueError("preview target path is too long")
    if not urlsplit(target).path.startswith(prefix):
        raise ValueError("preview target must be inside its capability prefix")
    return target, prefix


def _validated_preview_methods(http_methods: tuple[str, ...]) -> tuple[str, ...]:
    methods = tuple(dict.fromkeys(method.upper() for method in http_methods))
    if not methods or any(method not in _PREVIEW_HTTP_METHODS for method in methods):
        raise ValueError("invalid preview HTTP method scope")
    return methods


def _validate_preview_identity(
    authority_id: str | None,
    immutable_version: int | None,
) -> None:
    if authority_id is not None and (not authority_id or len(authority_id) > 256):
        raise ValueError("invalid preview authority")
    if immutable_version is not None and (
        not isinstance(immutable_version, int)
        or isinstance(immutable_version, bool)
        or immutable_version < 1
    ):
        raise ValueError("invalid immutable preview version")


def _intent_target_path(target: str) -> str:
    try:
        return urlsplit(target).path
    except ValueError:
        return ""


def _intent_scope_matches(
    cap: PreviewCapability,
    target: str,
    *,
    cid8: str,
    port: int,
    expected_prefix: str,
) -> bool:
    return (
        cap.conversation_id.removeprefix("conv_")[:8] == cid8
        and cap.port == int(port)
        and target.startswith("/")
        and cap.path_prefix == expected_prefix
        and _intent_target_path(target).startswith(expected_prefix)
    )


def _intent_static_host_matches(
    cap: PreviewCapability,
    *,
    path_scope: Literal["host", "static"],
    request_host_label: str | None,
) -> bool:
    if path_scope != "static":
        return True
    try:
        expected_label = path_preview_host_label(cap.conversation_id, cap.port)
    except ValueError:
        return False
    return bool(
        request_host_label
        and hmac.compare_digest(expected_label, request_host_label.strip().lower())
    )


def _intent_identity_matches(
    cap: PreviewCapability,
    *,
    expected_conversation_id: str | None,
    expected_owner_id: str | None,
    expected_authority_id: str | None,
) -> bool:
    if expected_conversation_id is not None and cap.conversation_id != expected_conversation_id:
        return False
    if expected_owner_id is not None and cap.owner_id != expected_owner_id:
        return False
    return expected_authority_id is None or cap.authority_id == expected_authority_id


def _valid_preview_authority(value: Any) -> bool:
    return value is None or isinstance(value, str) and bool(value) and len(value) <= 256


def _valid_immutable_version(value: Any) -> bool:
    return value is None or isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _normalized_capability_methods(payload: dict[str, Any]) -> tuple[str, ...] | None:
    methods = payload.get("methods")
    if methods is None and isinstance(payload.get("method"), str):
        methods = [payload["method"]]
    if (
        not isinstance(methods, list)
        or not methods
        or not all(isinstance(method, str) for method in methods)
    ):
        return None
    normalized = tuple(dict.fromkeys(method.upper() for method in methods))
    if len(normalized) != len(methods):
        return None
    if any(method not in _PREVIEW_HTTP_METHODS for method in normalized):
        return None
    return normalized


class PreviewCapabilitySigner:
    def __init__(
        self,
        codec: SignedTokenCodec | None = None,
        redemption_store: PreviewIntentRedemptionStore | None = None,
    ) -> None:
        self._codec = codec or SignedTokenCodec()
        self._redemption_store = redemption_store

    def mint_intent(
        self,
        *,
        session: AuthSession,
        conversation_id: str,
        port: int,
        target_path: str = "/",
        path_prefix: str = "/",
        allow_websocket: bool = False,
        http_methods: tuple[str, ...] = ("GET",),
        authority_id: str | None = None,
        immutable_version: int | None = None,
    ) -> str:
        target, prefix = _validated_preview_target(target_path, path_prefix)
        methods = _validated_preview_methods(http_methods)
        _validate_preview_identity(authority_id, immutable_version)
        now = int(time.time())
        store = self._redemption_store
        if store is None:
            raise RuntimeError("preview intent minting requires a durable redemption store")
        jti = secrets.token_urlsafe(18)
        expires_at = now + intent_ttl_s()
        token = self._codec.sign(
            {
                "v": 1,
                "kind": "preview_intent",
                "owner": session.owner_id,
                "cid": conversation_id,
                "port": int(port),
                "methods": list(methods),
                "prefix": prefix,
                "target": target,
                "ws": bool(allow_websocket),
                "authority": authority_id,
                "version": immutable_version,
                "jti": jti,
                "exp": expires_at,
            }
        )
        store.register_preview_intent(jti, expires_at)
        return token

    def redeem_intent(
        self,
        intent: str,
        *,
        cid8: str,
        port: int,
        path_scope: Literal["host", "static"],
        request_host_label: str | None = None,
        expected_conversation_id: str | None = None,
        expected_owner_id: str | None = None,
        expected_authority_id: str | None = None,
    ) -> tuple[str, str] | None:
        """Validate an endpoint-specific intent, then atomically exchange it once.

        Static intent redemption is also bound to the full-CID p3s request
        label here, before JTI consumption. Keeping that check inside the
        exchange prevents a wrong-host handoff from burning the correct launch.
        """

        intent_payload = self._codec.unsign(intent)
        if intent_payload is None or intent_payload.get("kind") != "preview_intent":
            return None
        target = intent_payload.get("target")
        cap = self._cap_from_payload(intent_payload, kind="preview_intent")
        if cap is None or not isinstance(target, str):
            return None
        expected_prefix = (
            "/"
            if path_scope == "host"
            else f"{ISOLATED_PATH_PREVIEW_PREFIX}/{cap.conversation_id}/"
        )
        if not _intent_scope_matches(
            cap,
            target,
            cid8=cid8,
            port=port,
            expected_prefix=expected_prefix,
        ):
            return None
        if not _intent_static_host_matches(
            cap,
            path_scope=path_scope,
            request_host_label=request_host_label,
        ):
            return None
        if not _intent_identity_matches(
            cap,
            expected_conversation_id=expected_conversation_id,
            expected_owner_id=expected_owner_id,
            expected_authority_id=expected_authority_id,
        ):
            return None
        if not self._consume_intent(intent_payload):
            return None
        return self._mint_cookie(cap, target)

    def _consume_intent(self, payload: dict[str, Any]) -> bool:
        store = self._redemption_store
        jti = payload.get("jti")
        if store is None or not isinstance(jti, str) or not jti:
            return False
        return store.consume_preview_intent(jti, now=int(time.time()))

    def _mint_cookie(self, cap: PreviewCapability, target: str) -> tuple[str, str]:
        token = self._codec.sign(
            {
                "v": 1,
                "kind": "preview_cap",
                "owner": cap.owner_id,
                "cid": cap.conversation_id,
                "port": cap.port,
                "methods": list(cap.http_methods),
                "prefix": cap.path_prefix,
                "ws": cap.allow_websocket,
                "authority": cap.authority_id,
                "version": cap.immutable_version,
                "exp": int(time.time()) + preview_ttl_s(),
            }
        )
        return token, target

    def mint_storage_handoff(
        self,
        cap: PreviewCapability,
        target: str,
        *,
        partitioned: bool,
    ) -> str:
        """Mint a one-use post-reset exchange without exposing a capability cookie."""

        target_path = urlsplit(target).path
        if not target.startswith("/") or not target_path.startswith(cap.path_prefix):
            raise ValueError("preview reset target is outside its capability prefix")
        store = self._redemption_store
        if store is None:
            raise RuntimeError("preview handoff minting requires a durable redemption store")
        now = int(time.time())
        jti = secrets.token_urlsafe(18)
        expires_at = now + intent_ttl_s()
        handoff = self._codec.sign(
            {
                "v": 1,
                "kind": "preview_storage_handoff",
                "owner": cap.owner_id,
                "cid": cap.conversation_id,
                "port": cap.port,
                "methods": list(cap.http_methods),
                "prefix": cap.path_prefix,
                "target": target,
                "ws": cap.allow_websocket,
                "authority": cap.authority_id,
                "version": cap.immutable_version,
                "partitioned": partitioned,
                "jti": jti,
                "exp": expires_at,
            }
        )
        store.register_preview_intent(jti, expires_at)
        return handoff

    def redeem_storage_handoff(
        self,
        handoff: str,
        *,
        cid8: str,
        port: int,
        expected_conversation_id: str,
        expected_owner_id: str,
        expected_authority_id: str,
    ) -> tuple[str, str, bool] | None:
        """Atomically exchange a reset handoff for the final capability cookie."""

        payload = self._codec.unsign(handoff)
        if payload is None:
            return None
        cap = self._cap_from_payload(payload, kind="preview_storage_handoff")
        target = payload.get("target")
        partitioned = payload.get("partitioned")
        try:
            target_path = urlsplit(target).path if isinstance(target, str) else ""
        except ValueError:
            target_path = ""
        if (
            cap is None
            or cap.conversation_id.removeprefix("conv_")[:8] != cid8
            or cap.port != int(port)
            or cap.conversation_id != expected_conversation_id
            or cap.owner_id != expected_owner_id
            or cap.authority_id != expected_authority_id
            or not isinstance(target, str)
            or not target.startswith("/")
            or not target_path.startswith(cap.path_prefix)
            or not isinstance(partitioned, bool)
            or not self._consume_intent(payload)
        ):
            return None
        token, verified_target = self._mint_cookie(cap, target)
        return token, verified_target, partitioned

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
        requested_method = method.upper()
        method_allowed = requested_method in cap.http_methods or (
            requested_method == "WEBSOCKET" and cap.allow_websocket
        )
        if cap.port != int(port) or not method_allowed:
            return None
        if not path.startswith(cap.path_prefix):
            return None
        return cap

    def verify_cookie_header(
        self,
        cookie_header: str | None,
        cookie_name: str,
        *,
        cid8: str,
        port: int,
        method: str,
        path: str,
    ) -> PreviewCapability | None:
        """Return the first valid capability among duplicate named cookies."""

        for token in cookie_header_values(cookie_header, cookie_name):
            cap = self.verify(token, cid8=cid8, port=port, method=method, path=path)
            if cap is not None:
                return cap
        return None

    @staticmethod
    def _cap_from_payload(payload: dict[str, Any], *, kind: str) -> PreviewCapability | None:
        if payload.get("kind") != kind:
            return None
        owner = payload.get("owner")
        cid = payload.get("cid")
        port = payload.get("port")
        prefix = payload.get("prefix")
        exp = payload.get("exp")
        allow_websocket = payload.get("ws", False)
        authority_id = payload.get("authority")
        immutable_version = payload.get("version")
        if not isinstance(owner, str) or not isinstance(cid, str):
            return None
        if not isinstance(prefix, str) or not isinstance(port, int) or not isinstance(exp, int):
            return None
        if not isinstance(allow_websocket, bool):
            return None
        if not _valid_preview_authority(authority_id):
            return None
        if not _valid_immutable_version(immutable_version):
            return None
        normalized_methods = _normalized_capability_methods(payload)
        if normalized_methods is None:
            return None
        # Short-lived pre-upgrade GET-only cookies remain readable during a
        # rolling deploy, but newly minted tokens always carry the explicit,
        # bounded method list. No legacy token can gain mutation authority.
        return PreviewCapability(
            owner,
            cid,
            port,
            normalized_methods,
            prefix,
            exp,
            allow_websocket=allow_websocket,
            authority_id=authority_id,
            immutable_version=immutable_version,
        )
