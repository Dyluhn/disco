"""Corrupted crawler text and old cached damage cannot become research evidence."""

import httpx
import pytest
from disco.retrieval._crawl4ai import Crawl4aiExtractionProvider
from disco.retrieval._extraction_text import corrupted_text
from disco.retrieval._retrieval_cache import RetrievalCache, store_docs
from disco.retrieval.bundled_providers import LocalExtractionProvider
from disco.retrieval.models import ExtractedDoc, Passage

BAD = (b"\x1f\x8b\x08\x00" + bytes(range(256)) * 10).decode("utf-8", "replace")
URL = "https://example.com/original-specification"
TEXT = "The original specification preserves the important condition for this operation."


@pytest.mark.parametrize(
    "text,corrupt",
    [
        (BAD, True),
        ("A readable sentence contains one damaged glyph: \ufffd.", False),
        ("文書の条件を確認します。 العربية لغة جميلة. café. " * 30, False),
    ],
)
def test_decoding_damage_is_distinct_from_non_english_text(text, corrupt):
    assert corrupted_text(text) is corrupt


@pytest.mark.parametrize("fallback_ok", [True, False])
async def test_corrupted_crawler_cache_refetches_original_once_and_caches_only_success(
    tmp_path, monkeypatch, fallback_ok
):
    crawl_calls, local_calls = [], []
    cache = RetrievalCache(tmp_path / "cache.sqlite")

    def crawler(request):
        crawl_calls.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": URL,
                        "success": True,
                        "status_code": 200,
                        "markdown": {"raw_markdown": BAD},
                    }
                ]
            },
        )

    async def local(self, url):
        local_calls.append(url)
        return ExtractedDoc(
            url=url,
            title="Original specification",
            content=TEXT if fallback_ok else "",
            fetched_ok=fallback_ok,
            status="ok" if fallback_ok else "error",
            passages=[Passage(id="original", source_url=url, source_title="Original", text=TEXT)]
            if fallback_ok
            else [],
            error=None if fallback_ok else "original fetch unavailable",
        )

    monkeypatch.setattr(LocalExtractionProvider, "extract", local)
    provider = Crawl4aiExtractionProvider(
        "http://crawler", transport=httpx.MockTransport(crawler), cache=cache
    )
    poisoned = ExtractedDoc(url=URL, title="Corrupted", content=BAD)
    cache.put_page(provider._cache_namespace + URL, poisoned.model_dump(mode="json"))
    result = await provider.extract(URL)
    assert len(crawl_calls) == 1 and local_calls == [URL]
    assert result.fetched_ok is fallback_ok
    assert result.content == (TEXT if fallback_ok else "")
    assert result.url == URL
    if fallback_ok:
        assert await provider.extract(URL) == result
        assert len(crawl_calls) == 1 and local_calls == [URL]
        assert result.passages[0].source_url == URL
    else:
        assert result.error == "original fetch unavailable"
        assert cache.get_page(provider._cache_namespace + URL)["content"] == BAD
    store_docs(cache, [poisoned], namespace="new:")
    assert cache.get_page("new:" + URL) is None


async def test_local_extraction_cannot_turn_binary_damage_into_a_success(monkeypatch):
    from disco.core.host_egress import GuardedResponse
    from disco.retrieval import bundled_providers

    async def fetch(*args, **kwargs):
        return GuardedResponse(URL, 200, {"content-type": "text/html"}, BAD.encode())

    monkeypatch.setattr(bundled_providers, "guarded_get", fetch)
    result = await LocalExtractionProvider().extract(URL)
    assert not result.fetched_ok and result.status == "error"
    assert not result.passages
    assert "corrupted text" in result.error
