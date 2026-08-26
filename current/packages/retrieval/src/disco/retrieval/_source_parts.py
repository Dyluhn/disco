"""Shared parsing and transport helpers for source adapters."""

from __future__ import annotations

import datetime
import re
from collections.abc import Mapping
from html.parser import HTMLParser
from urllib.parse import urlencode, urlparse

import httpx
from disco.core.host_egress import GuardedResponse, guarded_get

from .url_policy import parse_source_date


def response_status(response: httpx.Response | GuardedResponse) -> int | None:
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def with_params(base: str, params: Mapping[str, object]) -> str:
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{urlencode(params)}"


async def fetch_get(
    url: str,
    *,
    timeout_s: float,
    headers: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.Response | GuardedResponse:
    if transport is not None:
        async with httpx.AsyncClient(
            transport=transport, timeout=timeout_s, follow_redirects=True, trust_env=False
        ) as client:
            return await client.get(url, headers=headers)
    return await guarded_get(url, timeout_s=timeout_s, headers=headers)


def collapse_ws(value: str | None) -> str:
    return " ".join((value or "").split())


def recency_start(time_filter: str | None) -> datetime.date | None:
    days = {"week": 7, "month": 31}.get(time_filter or "")
    return datetime.date.today() - datetime.timedelta(days=days) if days else None


def atom_date(value: str | None) -> datetime.date | None:
    return parse_source_date(value)


class HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def strip_html(value: str | None) -> str:
    parser = HTMLTextExtractor()
    parser.feed(value or "")
    parser.close()
    text = collapse_ws(" ".join(parser.parts))
    return re.sub(r"\s+([.,;:!?])", r"\1", text)


def parse_sites(sites: str) -> tuple[str, ...]:
    domains: list[str] = []
    seen: set[str] = set()
    for raw in re.split(r"[\s,]+", sites):
        token = raw.strip().lower().removeprefix("site:")
        token = (urlparse(token).hostname or "") if "://" in token else token.split("/", 1)[0]
        token = token.strip(".")
        if token and token not in seen:
            seen.add(token)
            domains.append(token)
    return tuple(domains)
