"""Recover browser extraction failures whose original response is a PDF."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from disco.core.host_egress import EgressDenied, guarded_get

from ._pdf_extract import pdf_document
from ._transport_retry import _extraction_error_class
from .models import ExtractedDoc, Passage


async def _read_pdf(
    doc: ExtractedDoc,
    *,
    timeout_s: float,
    max_chars: int | None,
    chunk: Callable[[str, str, str], list[Passage]],
) -> ExtractedDoc:
    try:
        response = await guarded_get(doc.url, timeout_s=min(timeout_s, 20.0))
    except (EgressDenied, OSError, ValueError):
        return doc
    if response.status_code != 200:
        return doc
    recovered = await asyncio.to_thread(pdf_document, doc.url, response, chunk=chunk)
    if recovered is None:
        return doc  # A failed HTML crawl keeps its existing result and recovery route.
    if max_chars is not None and len(recovered.content) > max_chars:
        return ExtractedDoc(
            url=doc.url,
            title=recovered.title,
            content="",
            fetched_ok=False,
            status="error",
            error=(
                f"PDF text exceeds the configured {max_chars}-character extraction limit; "
                "no partial source was admitted. Use a smaller primary source or its HTML version."
            ),
        )
    return recovered


async def recover_pdf_sources(
    docs: dict[str, ExtractedDoc],
    *,
    timeout_s: float,
    max_chars: int | None,
    chunk: Callable[[str, str, str], list[Passage]],
) -> dict[str, ExtractedDoc]:
    """One guarded original read per browser 5xx/empty failure; no URL heuristics.

    A browser can mistake a downloadable PDF for an empty/blocked page. Inspect
    the response MIME type and bytes through the bundled parser instead. This
    route neither retries the crawler nor changes account/policy/source denials.
    Successful PDF text uses the provider's normal source identity and cache.
    """
    candidates = {
        url: doc
        for url, doc in docs.items()
        if (kind := _extraction_error_class(doc)) is not None
        and (kind == "empty_content" or kind.startswith("upstream_http_5"))
    }
    gate = asyncio.Semaphore(2)

    async def one(url: str, doc: ExtractedDoc) -> tuple[str, ExtractedDoc]:
        async with gate:
            return url, await _read_pdf(doc, timeout_s=timeout_s, max_chars=max_chars, chunk=chunk)

    return dict(await asyncio.gather(*(one(url, doc) for url, doc in candidates.items())))
