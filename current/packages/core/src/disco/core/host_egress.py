"""Host-side egress policy helpers.

Class-1 URLs are model-authored/untrusted: public web is allowed, private and
metadata ranges are never allowed, and redirects are revalidated hop-by-hop.
Class-2 URLs are operator-configured: private origins may be used only after the
exact origin is trusted in RouterConfig.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse


class EgressDenied(RuntimeError):
    """Raised before any wire traffic when host egress policy denies a URL."""


@dataclass(frozen=True)
class GuardedResponse:
    url: str
    status_code: int
    headers: dict[str, str]
    content: bytes

    @property
    def text(self) -> str:
        return self.content.decode(_charset(self.headers.get("content-type", "")), "replace")


def origin_for_url(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    port = parsed.port
    default = 443 if scheme == "https" else 80
    suffix = "" if port in (None, default) else f":{port}"
    return f"{scheme}://{host}{suffix}"


def trusted_origin(url: str, trusted_origins: object) -> bool:
    origin = origin_for_url(url)
    if not origin:
        return False
    if isinstance(trusted_origins, (set, frozenset, list, tuple)):
        return origin in {str(v).strip().lower() for v in trusted_origins}
    return False


def add_trusted_origins(existing: tuple[str, ...], *urls: str) -> tuple[str, ...]:
    values = {str(v).strip().lower() for v in existing if str(v).strip()}
    for url in urls:
        origin = origin_for_url(url)
        if origin:
            values.add(origin)
    return tuple(sorted(values))


def host_allowed(host: str, entries: frozenset[str] | None) -> bool:
    if entries is None:
        return True
    h = host.lower().strip().strip("[]")
    for entry in entries:
        e = entry.lower().strip()
        if not e:
            continue
        if e.startswith(".") and (h == e[1:] or h.endswith(e)):
            return True
        if h == e:
            return True
    return False


def validate_untrusted_url(url: str, allow_hosts: frozenset[str] | None = None) -> None:
    parsed = _parse_http_url(url)
    if not host_allowed(parsed.hostname or "", allow_hosts):
        raise EgressDenied(f"host {(parsed.hostname or '')!r} is not in the egress allowlist")
    _resolve_public_addrs(parsed.hostname or "", _port(parsed))


async def guarded_get(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout_s: float = 10.0,
    allow_hosts: frozenset[str] | None = None,
    max_redirects: int = 5,
    max_bytes: int = 5_000_000,
) -> GuardedResponse:
    import asyncio

    return await asyncio.to_thread(
        guarded_request,
        "GET",
        url,
        headers=headers,
        timeout_s=timeout_s,
        allow_hosts=allow_hosts,
        max_redirects=max_redirects,
        max_bytes=max_bytes,
    )


def guarded_request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    timeout_s: float = 10.0,
    allow_hosts: frozenset[str] | None = None,
    max_redirects: int = 5,
    max_bytes: int = 5_000_000,
) -> GuardedResponse:
    current = url
    req_headers = dict(headers or {})
    for _ in range(max_redirects + 1):
        resp = _single_request(
            method,
            current,
            headers=req_headers,
            body=body,
            timeout_s=timeout_s,
            allow_hosts=allow_hosts,
            max_bytes=max_bytes,
        )
        if resp.status_code not in {301, 302, 303, 307, 308}:
            return resp
        location = resp.headers.get("location")
        if not location:
            return resp
        current = urljoin(current, location)
        method = "GET" if resp.status_code in {301, 302, 303} else method
        body = None if method == "GET" else body
        req_headers = {
            k: v
            for k, v in req_headers.items()
            if k.lower() not in {"authorization", "cookie", "proxy-authorization"}
        }
    raise EgressDenied("too many redirects")


def _single_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: bytes | None,
    timeout_s: float,
    allow_hosts: frozenset[str] | None,
    max_bytes: int,
) -> GuardedResponse:
    parsed = _parse_http_url(url)
    host = parsed.hostname or ""
    if not host_allowed(host, allow_hosts):
        raise EgressDenied(f"host {host!r} is not in the egress allowlist")
    sock = _connect_validated(host, _port(parsed), parsed.scheme, timeout_s)
    try:
        target = parsed.path or "/"
        if parsed.query:
            target += f"?{parsed.query}"
        request_headers = {
            "Host": host if parsed.port is None else f"{host}:{parsed.port}",
            "Connection": "close",
            "User-Agent": "disco-host-egress/1.0",
            **headers,
        }
        payload = body or b""
        if payload:
            request_headers["Content-Length"] = str(len(payload))
        wire = [f"{method.upper()} {target} HTTP/1.1"]
        wire.extend(f"{k}: {v}" for k, v in request_headers.items())
        sock.sendall(("\r\n".join(wire) + "\r\n\r\n").encode("latin1") + payload)
        resp = http.client.HTTPResponse(sock)
        resp.begin()
        data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise EgressDenied("response exceeded host fetch byte limit")
        return GuardedResponse(
            url=url,
            status_code=int(resp.status),
            headers={k.lower(): v for k, v in resp.getheaders()},
            content=data,
        )
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _parse_http_url(url: str):
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise EgressDenied(f"malformed URL: {exc}") from exc
    if parsed.scheme not in {"http", "https"}:
        raise EgressDenied(f"scheme {parsed.scheme!r} not allowed")
    if not parsed.hostname:
        raise EgressDenied("URL has no host")
    return parsed


def _port(parsed) -> int:
    return int(parsed.port or (443 if parsed.scheme == "https" else 80))


def _connect_validated(host: str, port: int, scheme: str, timeout_s: float) -> socket.socket:
    infos = _resolve_public_addrs(host, port)
    last_exc: OSError | None = None
    for family, socktype, proto, _canon, sockaddr in infos:
        raw = socket.socket(family, socktype, proto)
        raw.settimeout(timeout_s)
        try:
            raw.connect(sockaddr)
            peer = raw.getpeername()[0]
            if _blocked_ip(peer):
                raw.close()
                raise EgressDenied(f"connected address {peer} is denied")
            if scheme == "https":
                ctx = ssl.create_default_context()
                return ctx.wrap_socket(raw, server_hostname=host)
            return raw
        except OSError as exc:
            last_exc = exc
            raw.close()
    raise OSError(f"could not connect to {host}:{port}: {last_exc}")


def _resolve_public_addrs(host: str, port: int):
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise EgressDenied(f"DNS resolution failed for {host!r}: {exc}") from exc
    if not infos:
        raise EgressDenied(f"DNS resolution returned no addresses for {host!r}")
    denied = [str(info[4][0]) for info in infos if _blocked_ip(str(info[4][0]))]
    if denied:
        raise EgressDenied(f"resolved address {denied[0]} is denied")
    return infos


def _blocked_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return True
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or getattr(ip, "is_site_local", False)
        or not ip.is_global
    )


def _charset(content_type: str) -> str:
    for part in content_type.split(";"):
        key, sep, val = part.strip().partition("=")
        if sep and key.lower() == "charset" and val.strip():
            return val.strip()
    return "utf-8"
