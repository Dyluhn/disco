"""A small on-disk memory for retrieval's two expensive fetches.

The same eight questions are re-run every batch with near-identical queries and
near-identical pages. Over eight measured batches (2026-09-03) that came to
6,501 page fetches at a 48% success rate, and every re-run paid for the same
pages again. This module remembers the answers that were worth remembering — a
search that actually answered, and a page that actually read — so a re-run
spends its budget on what changed.

Two things are deliberately NOT stored: a degraded or empty search answer, and a
failed extraction. Caching either one freezes an outage — a five-minute engine
blip would become a day of empty reports, and the cache would be the reason.

sqlite3 in WAL mode, one process lock, one table. There is no schema-migration
story on purpose: a cache that cannot be read is a cache that gets deleted, not
one that gets repaired.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

from ._extraction_text import challenge_page, corrupted_text
from .models import ExtractedDoc

_LOG = logging.getLogger(__name__)

# The cache file inside the data dir. Named, not derived, so an operator who has
# to delete it knows exactly what to delete.
CACHE_FILENAME = "retrieval-cache.sqlite"

# A search answer goes stale within a day (the web moved, the engines rotated);
# the TEXT of a page that already read does not, so a page is kept a week.
SEARCH_TTL_S = 24 * 60 * 60
PAGE_TTL_S = 7 * 24 * 60 * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    stored_at REAL NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (kind, key)
)
"""


def search_cache_key(base_url: str, categories: str, time_range: str, query: str) -> str:
    """The identity of one search: where it was sent, how it was scoped, what it asked.

    The query is normalized (casefold, whitespace-collapsed) because the same
    question comes back every batch spelled with cosmetically different spacing.
    The instance and the scope are in the key because the same words asked of a
    different instance, or over a different time window, are a different search.
    """
    normalized = " ".join(query.split()).casefold()
    return hashlib.sha256(f"{base_url}|{categories}|{time_range}|{normalized}".encode()).hexdigest()


class RetrievalCache:
    """Searches and pages, kept on disk with a TTL each.

    ``now`` is injectable so expiry is testable without waiting a day for it.
    """

    def __init__(self, path: str | Path, *, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._guard = threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Guarded by `_guard` on every statement, so it is safe to hand the same
        # connection to the thread a sync provider happens to run on.
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(_SCHEMA)
        self._db.commit()

    def get_search(self, key: str) -> list[Any] | None:
        """The stored results for this search key, or None on a miss/expiry."""
        cached = self._get("search", key, SEARCH_TTL_S)
        return cached if isinstance(cached, list) else None

    def put_search(self, key: str, results: list[Any]) -> None:
        """Store one search's results. Callers filter out degraded answers first."""
        self._put("search", key, results)

    def get_page(self, url: str) -> dict[str, Any] | None:
        """The stored ExtractedDoc fields for this url, or None on a miss/expiry."""
        cached = self._get("page", url, PAGE_TTL_S)
        return cached if isinstance(cached, dict) else None

    def put_page(self, url: str, doc_fields: dict[str, Any]) -> None:
        """Store one page's extracted fields. Callers store only ok docs."""
        self._put("page", url, doc_fields)

    def _get(self, kind: str, key: str, ttl_s: float) -> Any | None:
        with self._guard:
            row = self._db.execute(
                "SELECT stored_at, payload FROM entries WHERE kind = ? AND key = ?",
                (kind, key),
            ).fetchone()
        if row is None:
            return None
        if self._now() - float(row[0]) > ttl_s:
            _LOG.info("retrieval cache entry expired (%s), refetching", kind)
            return None
        try:
            return json.loads(row[1])
        except ValueError:
            # A payload we can no longer read is a miss, never a crash: the
            # fetch that follows will overwrite it.
            _LOG.warning("retrieval cache payload unreadable (%s), treating as a miss", kind)
            return None

    def _put(self, kind: str, key: str, payload: Any) -> None:
        with self._guard:
            self._db.execute(
                "INSERT OR REPLACE INTO entries (kind, key, stored_at, payload) "
                "VALUES (?, ?, ?, ?)",
                (kind, key, self._now(), json.dumps(payload)),
            )
            self._db.commit()


async def cached_search(
    cache: RetrievalCache | None,
    key: str,
    fetch: Callable[[], Awaitable[tuple[list[Any], dict[str, object]]]],
) -> tuple[list[Any], dict[str, object]]:
    """One search's results, from disk when a warm entry is worth trusting.

    Storing is the narrow part, and it is the whole safety story: an answer is
    kept only if it carried at least one result AND named no unresponsive
    engine. A degraded or empty answer cached is an outage cached, and the next
    day of reports would inherit it.
    """
    if cache is None:
        return await fetch()
    cached = cache.get_search(key)
    if cached is not None:
        _LOG.info("search cache hit: %d results served from disk", len(cached))
        return cached, {"result_count": len(cached), "latency_ms": 0, "cache": "hit"}
    results, diagnostic = await fetch()
    if results and not diagnostic.get("unresponsive_engines"):
        cache.put_search(key, results)
    return results, diagnostic


def cached_docs(
    cache: RetrievalCache | None, urls: list[str], *, namespace: str = ""
) -> dict[str, ExtractedDoc]:
    """The pages a recent run already read, keyed by url."""
    if cache is None:
        return {}
    hits: dict[str, ExtractedDoc] = {}
    for url in urls:
        fields = cache.get_page(namespace + url)
        if fields is not None:
            doc = ExtractedDoc(**fields)
            if corrupted_text(doc.content) or challenge_page(doc.title, doc.content):
                _LOG.warning("extraction cache contains unreadable content for %s; refetching", url)
                continue
            _LOG.info("extraction cache hit for %s", url)
            hits[url] = doc
    return hits


def store_docs(
    cache: RetrievalCache | None, docs: Iterable[ExtractedDoc], *, namespace: str = ""
) -> None:
    """Remember the pages that actually read. A failure is never stored — a
    cached failure would delete that source from every run for a week."""
    if cache is None:
        return
    for doc in docs:
        if (
            doc.fetched_ok
            and doc.status == "ok"
            and doc.content
            and not corrupted_text(doc.content)
            and not challenge_page(doc.title, doc.content)
        ):
            cache.put_page(namespace + doc.url, doc.model_dump(mode="json"))


# One cache per path per process. Providers are rebuilt for every research run;
# opening a fresh sqlite connection each time would leak a file descriptor per
# run for the life of the server.
_OPEN: dict[str, RetrievalCache] = {}
_OPEN_GUARD = threading.Lock()


def open_cache(path: str | Path) -> RetrievalCache:
    """The one cache this process holds for ``path``, opening it on first use."""
    key = str(Path(path).expanduser())
    with _OPEN_GUARD:
        cache = _OPEN.get(key)
        if cache is None:
            cache = _OPEN[key] = RetrievalCache(key)
        return cache


__all__ = [
    "CACHE_FILENAME",
    "PAGE_TTL_S",
    "SEARCH_TTL_S",
    "RetrievalCache",
    "cached_docs",
    "cached_search",
    "open_cache",
    "store_docs",
    "search_cache_key",
]
