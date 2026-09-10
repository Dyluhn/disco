"""Direct source reads through the existing retrieval and extraction boundary."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from .models import RetrievalRequest, SearchHit
from .url_policy import source_url_key, url_allowed


def _source_url(query: str) -> str | None:
    """Recognize a complete URL; extraction still owns egress validation."""
    value = query.strip()
    if any(char.isspace() for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            return value
    except ValueError:
        pass
    return None


def _direct_discovery(req: RetrievalRequest) -> tuple[list[SearchHit], object] | None:
    url = _source_url(req.query)
    if url is None:
        return None
    allowed = url_allowed(url, req.domains_allow, req.domains_deny)
    selected = req.selected_hit
    if allowed and selected is not None and source_url_key(selected.url) == source_url_key(url):
        hit = selected.model_copy(update={"url": url, "status": None})
    else:
        hit = SearchHit(url=url, title=url, source_engine="direct_url")
    hits = [hit] if allowed else []
    return hits, {"acquisition": "direct_url", "domain_policy": "allowed" if allowed else "denied"}


def _direct_trace(trace: dict[str, Any]) -> None:
    """A requested URL is not a search result or an issued search query."""
    trace.update(
        acquisition="direct_url",
        provider="direct_url",
        issued_queries=[],
        raw_discovered_hit_count=0,
        direct_sources=[hit for row in trace["queries"] for hit in row["hits"]],
        queries=[],
    )
