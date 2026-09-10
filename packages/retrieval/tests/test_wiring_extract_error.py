"""The extract capability handler must surface the provider's concrete failure
cause. Dropping it left the model a bare "error" for an egress-denied loopback
preview URL and invited a retry of an impossible call (pilot seed 405717)."""

from __future__ import annotations

import pytest
from disco.retrieval.models import ExtractedDoc
from disco.retrieval.wiring import retrieval_capability_handlers


class _Search:
    async def search(self, query, *, limit=8):  # noqa: ANN001, ANN201
        return []


class _DeniedExtraction:
    async def extract(self, url: str) -> ExtractedDoc:
        return ExtractedDoc(
            url=url,
            title="",
            content="",
            fetched_ok=False,
            error="resolved address 127.0.0.1 is denied",
            status="error",
        )


class _OkExtraction:
    async def extract(self, url: str) -> ExtractedDoc:
        return ExtractedDoc(url=url, title="Page", content="body", fetched_ok=True)


@pytest.mark.asyncio
async def test_extract_handler_surfaces_provider_error_verbatim():
    handlers = retrieval_capability_handlers(_Search(), _DeniedExtraction())
    doc = await handlers["extract"](url="http://127.0.0.1:3000/")
    assert doc["fetched_ok"] is False
    assert doc["error"] == "resolved address 127.0.0.1 is denied"


@pytest.mark.asyncio
async def test_extract_handler_success_carries_no_error():
    handlers = retrieval_capability_handlers(_Search(), _OkExtraction())
    doc = await handlers["extract"](url="https://example.org/")
    assert doc["fetched_ok"] is True
    assert doc["error"] is None
