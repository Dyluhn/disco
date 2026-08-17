"""Return-origin canonicalization/validation for host-service credentials."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from disco.core.host_egress import origin_for_url


def _canonical_origins(origins: frozenset[str]) -> frozenset[str]:
    canonical: set[str] = set()
    for value in origins:
        try:
            parsed = urlsplit(value)
            origin = origin_for_url(value)
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("invalid host-service return origin") from exc
        if (
            origin is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid host-service return origin")
        if parsed.scheme == "http":
            host = parsed.hostname or ""
            try:
                loopback = host.lower() == "localhost" or ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host.lower() == "localhost"
            if not loopback:
                raise ValueError("plaintext host-service return origin must be loopback")
        canonical.add(origin)
    return frozenset(canonical)
