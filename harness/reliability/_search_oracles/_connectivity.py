"""Connectivity validation and probing for search oracle."""

from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ._validators import Findings, _list, _web_url


def _safe_public_host(url: str) -> tuple[bool, str | None]:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if not _web_url(url) or not parsed.hostname:
        return False, "not a public HTTP(S) URL"
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM
            )
        }
    except OSError as exc:
        return False, f"DNS failed: {exc}"
    if not addresses:
        return False, "DNS returned no addresses"
    if any(not ipaddress.ip_address(address).is_global for address in addresses):
        return False, "DNS resolved to a non-public address"
    return True, None


def _probe_one(url: str, timeout_s: float) -> dict[str, Any]:
    safe, error = _safe_public_host(url)
    if not safe:
        return {"url": url, "connected": False, "status": None, "error": error}
    headers = {"User-Agent": "Disco-Reliability-Harness/1.0"}
    for method in ("HEAD", "GET"):
        request = urllib.request.Request(url, headers=headers, method=method)
        if method == "GET":
            request.add_header("Range", "bytes=0-1023")
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
                return {
                    "url": url,
                    "connected": True,
                    "status": int(response.status),
                    "final_url": response.geturl(),
                    "error": None,
                }
        except urllib.error.HTTPError as exc:
            if method == "HEAD" and exc.code not in {401, 403, 429}:
                continue
            return {
                "url": url,
                "connected": True,
                "status": int(exc.code),
                "final_url": exc.geturl(),
                "error": str(exc),
            }
        except (OSError, urllib.error.URLError) as exc:
            if method == "HEAD":
                continue
            return {"url": url, "connected": False, "status": None, "error": str(exc)}
    return {"url": url, "connected": False, "status": None, "error": "probe exhausted"}


def probe_cited_sources(
    payload: dict[str, Any], *, timeout_s: float = 15.0
) -> list[dict[str, Any]]:
    from ._validators import _cited_passage_ids

    cited_ids = _cited_passage_ids(payload)
    cited_urls: set[str] = set()
    for passage in _list(payload.get("passages")):
        if not isinstance(passage, dict) or passage.get("id") not in cited_ids:
            continue
        source_url = passage.get("source_url")
        if isinstance(source_url, str):
            cited_urls.add(source_url)
    urls = sorted(cited_urls)
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(urls)))) as executor:
        return list(executor.map(lambda url: _probe_one(url, timeout_s), urls))


def validate_connectivity(
    connectivity: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    cited_ids: set[str],
    findings: Findings,
) -> None:
    by_url = {
        item.get("url"): item
        for item in connectivity
        if isinstance(item, dict) and isinstance(item.get("url"), str)
    }
    urls = {by_id[passage_id].get("source_url") for passage_id in cited_ids if passage_id in by_id}
    successful = 0
    for url in sorted(url for url in urls if isinstance(url, str)):
        probe = by_url.get(url)
        if not probe:
            findings.add("SOURCE_NOT_PROBED", "connectivity", url)
            continue
        status = probe.get("status")
        acceptable = (
            bool(probe.get("connected"))
            and isinstance(status, int)
            and (200 <= status < 400 or status in {401, 403, 429})
        )
        if acceptable:
            successful += 1
        else:
            findings.add(
                "CITED_SOURCE_UNREACHABLE",
                "connectivity",
                f"{url}: status={status!r} error={probe.get('error')!r}",
            )
    findings.require(
        successful > 0,
        "NO_REACHABLE_CITED_SOURCE",
        "connectivity",
        "no cited source accepted a fresh network connection",
    )
