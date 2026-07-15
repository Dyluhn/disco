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
from typing import Any

from .env import disco_env
from .store.sqlite import DEFAULT_OWNER_ID

SESSION_COOKIE = "disco_session"
CSRF_HEADER = "X-Disco-CSRF"
PREVIEW_COOKIE = "disco_preview_cap"
PREVIEW_BOOTSTRAP_PATH = "/__disco/preview-auth"
PATH_PREVIEW_BOOTSTRAP_PATH = "/__disco/path-preview-auth"

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
    # The compose self-host frontend (nginx :8088). Missing from this list, even
    # the documented localhost quickstart had its API responses CORS-blocked —
    # found live 2026-07-09 on the first browser test of a fresh compose deploy.
    "http://localhost:8088",
    "http://127.0.0.1:8088",
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


def path_preview_cookie_name(cid8: str) -> str:
    """Per-conversation name so concurrent isolated previews cannot clobber each other."""
    normalized = "".join(ch for ch in cid8.lower() if ch in "0123456789abcdef")[:8]
    if len(normalized) != 8:
        raise ValueError("path preview cookie requires an eight-character hex id")
    return f"disco_path_preview_{normalized}"


def allowed_frontend_origins() -> tuple[str, ...]:
    raw = disco_env("FRONTEND_ORIGINS", "")
    if raw:
        origins = [part.strip().rstrip("/") for part in raw.replace(",", " ").split()]
        return tuple(origin for origin in origins if origin)
    # Zero-config remote access: when the deployment declares its public UI URL
    # (DISCO_PUBLIC_UI_URL — the same value the boot banner prints), that origin
    # is allowed alongside the localhost defaults. Without this, a LAN/tailnet
    # browser passes the env.js host derivation but then has every API response
    # CORS-blocked — the second layer of the 2026-07-09 remote fresh-install bug.
    public_ui = (disco_env("PUBLIC_UI_URL", "") or "").strip().rstrip("/")
    if public_ui:
        return (*_DEFAULT_ALLOWED_ORIGINS, public_ui)
    return _DEFAULT_ALLOWED_ORIGINS


def origin_allowed(origin: str | None) -> bool:
    return bool(origin and origin.rstrip("/") in allowed_frontend_origins())


def origin_matches_request_host(origin: str | None, request_host: str | None) -> bool:
    """Same-origin check for the single-front-door deploy: nginx proxies BOTH
    servers under the page's own origin and forwards the browser's Host header
    untouched, so a legitimate request's Origin netloc EQUALS the request Host.
    This is what makes any hostname (localhost, LAN IP, tailnet name, domain)
    work with zero origin config. A cross-site page can't match: the browser
    stamps ITS origin (attacker.example), which never equals the Host it posted
    to. Non-browser clients can forge both headers, but they always could —
    the session cookie remains the actual authentication; origin checks are the
    CSRF layer. Netloc comparison is case-insensitive, scheme-agnostic."""
    if not origin or not request_host:
        return False
    from urllib.parse import urlsplit

    try:
        netloc = urlsplit(origin.strip().rstrip("/")).netloc
    except ValueError:
        return False
    return bool(netloc) and netloc.lower() == request_host.strip().lower()


def origin_permitted(origin: str | None, request_host: str | None = None) -> bool:
    """The general request-gating check: the static allowlist (split-origin dev
    layout) OR the same-host front-door rule. NOT for the tokenless auto-pair /
    pairing-token-fetch conveniences — those keep the STRICT allowlist so a DNS-
    rebound page (whose Origin would match the rebound Host) can never reach the
    credential-granting shortcuts."""
    return origin_allowed(origin) or origin_matches_request_host(origin, request_host)


_LOCALHOST_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_localhost_origin(origin: str | None) -> bool:
    if not origin:
        return False
    from urllib.parse import urlsplit

    try:
        hostname = urlsplit(origin.strip().rstrip("/")).hostname or ""
    except ValueError:
        return False
    return hostname.lower() in _LOCALHOST_HOSTNAMES


_LOOPBACK_BINDS = ("", "127.0.0.1", "localhost", "::1")


def _local_bind_declared() -> bool:
    """True when the deployment publishes its ports on loopback ONLY. The kernel
    then guarantees no other machine can reach the ports at all, which is what
    makes the localhost auto-pair below sound; anything else (0.0.0.0, a LAN IP)
    is a DELIBERATE exposure and flips the token requirement back on.

    Two layers of truth: `DISCO_BIND` is the compose HOST-port publish address —
    when set (compose always passes it; default 127.0.0.1) it decides, because
    the container-internal `DISCO_HOST` must be 0.0.0.0 for nginx to reach the
    backend and says nothing about exposure. Without `DISCO_BIND` (a plain
    host-process run) the server's own bind address `DISCO_HOST` (default
    127.0.0.1) IS the exposure."""
    bind = (disco_env("BIND", "") or "").strip()
    if bind:
        return bind in _LOOPBACK_BINDS
    return (disco_env("HOST", "") or "").strip() in _LOOPBACK_BINDS


# Headers that betray a reverse proxy / tunnel in front (ngrok, cloudflared,
# CF Access, a hand-rolled nginx-with-XFF). The front-door nginx deliberately
# forwards ONLY X-Forwarded-Proto — never these — so a genuinely-local browser
# load carries none of them. Their PRESENCE means the request is exposed.
_PROXY_HEADERS = (
    "x-forwarded-for",
    "x-forwarded-host",
    "forwarded",
    "cf-connecting-ip",
    "x-real-ip",
)


def request_traversed_proxy(headers: Any) -> bool:
    """True when a hop-revealing forwarding header is present — the signal that a
    reverse proxy / tunnel sits in front of the front-door nginx (i.e. the deploy
    is exposed, whatever its bind posture claims). Used to REFUSE the localhost
    auto-pair on any proxied request. `headers` is any case-insensitive mapping
    (Starlette Request/WebSocket .headers). A client that forges one of these
    only causes a REFUSAL — fail-safe."""
    get = getattr(headers, "get", None)
    if get is None:
        return False
    return any(get(h) for h in _PROXY_HEADERS)


def localhost_auto_pair_allowed(
    origin: str | None, request_host: str | None, *, via_proxy: bool = False
) -> bool:
    """Zero-friction first run on the operator's own machine: allow a TOKENLESS
    admin mint when ALL THREE hold —

      1. The deployment is loopback-bound (`_local_bind_declared`): the ports
         are kernel-unreachable from any other machine, so whoever reached them
         is on this host (or came through a tunnel the operator opened).
      2. The page is a localhost-family origin: the browser itself asserts it
         loaded the app from this machine. A cross-site page (or a DNS-rebound
         one) carries ITS OWN origin, never localhost.
      3. Origin == request Host: it is THIS app's own front-door page pairing,
         not some other local app on a different port riding the allowlist.

    The containerized deploy can never use the TCP-peer loopback check (the
    server sees the Docker gateway for everyone), so the bind posture stands in
    as the unforgeable signal: a non-browser client that forges a localhost
    Origin could only do so while already ON the machine — i.e. the operator.
    `DISCO_AUTH_LOCAL_AUTO_PAIR` overrides explicitly (1 forces on, 0 off).

    `via_proxy` (a forwarding header was present) hard-disables this: a tunnel /
    reverse proxy in front means the loopback-bind premise no longer holds, so a
    remote client forging a localhost Host must NOT slip through — require the
    token instead. Residual footgun: a RAW TCP port-forward (`ssh -R`) of a
    loopback-bound instance adds no headers and cannot be distinguished from a
    local browser; that is a deliberate exposure — set DISCO_BIND or
    DISCO_AUTH_LOCAL_AUTO_PAIR=0. (codex 2026-07-09)"""
    override = (disco_env("AUTH_LOCAL_AUTO_PAIR", "") or "").strip().lower()
    if override in {"0", "false", "no", "off"}:
        return False
    forced_on = override in {"1", "true", "yes", "on"}
    # The proxy guard applies EVEN to the explicit force-on: if you front it with
    # a proxy you are exposed, and the localhost shortcut has no meaning there.
    if via_proxy:
        return False
    if not forced_on and not _local_bind_declared():
        return False
    return _is_localhost_origin(origin) and origin_matches_request_host(origin, request_host)


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

    @staticmethod
    def csrf_valid(session: AuthSession, presented: str | None) -> bool:
        return bool(presented and hmac.compare_digest(session.csrf_token, presented.strip()))


class PreviewCapabilitySigner:
    def __init__(self, codec: SignedTokenCodec | None = None) -> None:
        self._codec = codec or SignedTokenCodec()

    def mint_intent(
        self,
        *,
        session: AuthSession,
        conversation_id: str,
        port: int,
        target_path: str = "/",
        path_prefix: str = "/",
    ) -> str:
        target = target_path if target_path.startswith("/") else f"/{target_path}"
        prefix = path_prefix if path_prefix.startswith("/") else f"/{path_prefix}"
        if not target.split("?", 1)[0].startswith(prefix):
            raise ValueError("preview target must be inside its capability prefix")
        now = int(time.time())
        return self._codec.sign(
            {
                "v": 1,
                "kind": "preview_intent",
                "owner": session.owner_id,
                "cid": conversation_id,
                "port": int(port),
                "method": "WEBSOCKET" if target_path.startswith("ws:") else "GET",
                "prefix": prefix,
                "target": target,
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
