"""Challenge responses remain failures through retrieval, cache and recovery."""

from pathlib import Path

import httpx
import pytest
from disco.retrieval import DefaultRetrievalEngine, LexicalReranker, RetrievalRequest
from disco.retrieval._crawl4ai import Crawl4aiExtractionProvider
from disco.retrieval._extraction_fallbacks import reset_extraction_pacing
from disco.retrieval._retrieval_cache import RetrievalCache, cached_docs, store_docs
from disco.retrieval._transport_retry import (
    _extraction_error_class,
    is_transient_extraction_failure,
)
from disco.retrieval.bundled_providers import FirecrawlExtractionProvider, LocalExtractionProvider
from disco.retrieval.deep_research._agent_state import _AgentState, _report_usable_passage
from disco.retrieval.deep_research._budget import SourceBudget
from disco.retrieval.engine import extract_discovered_hits
from disco.retrieval.models import ExtractedDoc, Passage, RetrievalResult
from research_fakes import FakeSearchProvider, hit

URL = "https://papers.example.test/article"
TITLE = "Radware Bot Manager Captcha"
BODY = (
    "## We apologize for the inconvenience...\n"
    "Please can you confirm you are a human by ticking the box below.\n"
    "Incident ID: fixture-123"
)
ARTICLE = "This study compares the usability of CAPTCHA verification and bot detection."


def _doc(url=URL, *, challenge=True):
    title, text = (TITLE, BODY) if challenge else ("CAPTCHA research", ARTICLE)
    return ExtractedDoc(
        url=url,
        title=title,
        content=text,
        passages=[Passage(id=url, source_url=url, source_title=title, text=text)],
    )


def _assert_blocked(doc):
    assert doc.url == URL
    assert doc.status == "blocked" and not doc.fetched_ok
    assert not doc.passages
    assert _extraction_error_class(doc) == "anti_bot"
    assert not is_transient_extraction_failure(doc)


@pytest.mark.parametrize("provider", ["crawl4ai", "firecrawl"])
def test_http_success_challenge_is_an_unretryable_extraction_failure(provider):
    if provider == "crawl4ai":
        doc = Crawl4aiExtractionProvider("http://crawler")._to_doc(
            URL,
            {"success": True, "status_code": 200, "metadata": {"title": TITLE}, "markdown": BODY},
        )
    else:
        doc = FirecrawlExtractionProvider("fixture-key")._to_doc(
            URL, {"metadata": {"title": TITLE}, "markdown": BODY}
        )
    _assert_blocked(doc)
    assert doc.title == TITLE and doc.content == BODY


@pytest.mark.parametrize("challenge", [True, False])
async def test_local_html_extraction_distinguishes_challenge_from_research(monkeypatch, challenge):
    original = _doc(challenge=challenge)
    calls = []

    async def fetch(url, **kwargs):
        calls.append(url)
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            headers={"content-type": "text/html"},
            text=f"<title>{original.title}</title><main>{original.content}</main>",
        )

    monkeypatch.setattr("disco.retrieval.bundled_providers.guarded_get", fetch)
    result = await LocalExtractionProvider().extract(URL)
    assert calls == [URL]
    if challenge:
        _assert_blocked(result)
    else:
        assert result.fetched_ok and result.status == "ok"
        assert result.content == ARTICLE and result.passages


def test_preexisting_challenge_cache_entry_is_a_miss(tmp_path: Path):
    cache = RetrievalCache(tmp_path / "retrieval.sqlite", now=lambda: 0)
    original = _doc()
    cache.put_page("fixture:" + URL, original.model_dump(mode="json"))
    assert cached_docs(cache, [URL], namespace="fixture:") == {}


def test_cache_refuses_unnormalized_challenges_and_retains_legitimate_articles(tmp_path: Path):
    cache = RetrievalCache(tmp_path / "retrieval.sqlite", now=lambda: 0)
    ordinary = _doc(URL + "/research", challenge=False)
    store_docs(cache, [_doc(), ordinary], namespace="fixture:")
    assert cache.get_page("fixture:" + URL) is None
    assert cached_docs(cache, [URL, ordinary.url], namespace="fixture:") == {ordinary.url: ordinary}


class _Extraction:
    def __init__(self):
        self.calls = []

    async def extract_many(self, urls):
        self.calls.append(urls)
        return [_doc(url, challenge=url == URL) for url in urls]


class _AffinityExtraction(_Extraction):
    async def extract_hits(self, hits):
        self.calls.append(hits)
        return [_doc(item.url, challenge=item.url == URL) for item in hits]

    async def extract_many(self, urls):
        raise AssertionError("discovery affinity was discarded")


@pytest.mark.parametrize("affinity", [False, True])
async def test_provider_independent_boundary_normalizes_without_losing_affinity(affinity):
    extractor = _AffinityExtraction() if affinity else _Extraction()
    discovered = [hit(URL), hit(URL + "/research")]
    docs = await extract_discovered_hits(extractor, discovered)
    assert extractor.calls == [discovered if affinity else [item.url for item in discovered]]
    _assert_blocked(docs[0])
    assert docs[1] == _doc(URL + "/research", challenge=False)


async def test_challenge_is_failed_discovery_and_never_citable_in_retrieval_trace():
    result = await DefaultRetrievalEngine(
        FakeSearchProvider([hit(URL), hit(URL + "/research")]),
        _Extraction(),
        LexicalReranker(),
    ).retrieve(RetrievalRequest(query="CAPTCHA verification", depth="shallow"))
    assert [item.status for item in result.all_hits] == ["blocked", "ok"]
    assert [item.source_url for item in result.passages] == [URL + "/research"]
    trace = result.notes["retrieval_trace"]["extraction"]
    assert (trace["attempted"], trace["success"], trace["failure"]) == (2, 1, 1)
    assert trace["statuses"][0]["error_class"] == "anti_bot"
    assert trace["statuses"][0]["fetched_ok"] is False
    state = _AgentState(SourceBudget(limit=10))
    assert state.admit_retrieved(result) == 1
    assert state.budget.used == 1
    assert [p.source_url for p in state.pool] == [URL + "/research"]
    assert state.extraction.failures == {"anti_bot": 1}


def test_passage_only_research_admission_excludes_web_challenges_but_preserves_user_corpus():
    challenge = _doc().passages[0]
    assert not _report_usable_passage(challenge)
    assert _report_usable_passage(challenge.model_copy(update={"corpus_id": "user-document"}))
    assert _report_usable_passage(_doc(challenge=False).passages[0])


@pytest.mark.parametrize("archive_challenge", [False, True])
async def test_crawler_uses_one_existing_archive_fallback_and_never_caches_a_challenge(
    tmp_path: Path, monkeypatch, archive_challenge
):
    crawl_calls, archive_calls = [], []

    def transport(request):
        crawl_calls.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": URL,
                        "success": True,
                        "status_code": 200,
                        "metadata": {"title": TITLE},
                        "markdown": BODY,
                    }
                ]
            },
        )

    async def archive(self, url):
        archive_calls.append(url)
        return _doc(url, challenge=archive_challenge)

    def validate(url):
        assert url == URL

    monkeypatch.setattr("disco.retrieval._transport_retry.validate_untrusted_url", validate)
    monkeypatch.setattr(LocalExtractionProvider, "extract", archive)
    reset_extraction_pacing()
    cache = RetrievalCache(tmp_path / "retrieval.sqlite", now=lambda: 0)
    provider = Crawl4aiExtractionProvider(
        "http://crawler", transport=httpx.MockTransport(transport), cache=cache
    )
    result = await provider.extract(URL)
    assert crawl_calls == ["/crawl"]
    assert archive_calls == ["https://web.archive.org/web/2id_/" + URL]
    if archive_challenge:
        _assert_blocked(result)
        assert cached_docs(cache, [URL], namespace=provider._cache_namespace) == {}
    else:
        assert result.fetched_ok and result.url == URL
        assert result.content == ARTICLE
        assert all(p.source_url == URL for p in result.passages)
        assert await provider.extract(URL) == result
        assert len(crawl_calls) == len(archive_calls) == 1
    reset_extraction_pacing()


def test_full_document_challenge_and_passage_only_challenge_consume_no_source_capacity():
    document = _doc()
    last_chunk = document.passages[0].model_copy(
        update={"text": "Incident ID fixture and verification diagnostic."}
    )
    state = _AgentState(SourceBudget(limit=10))
    for passages, documents in [([last_chunk], [document]), (document.passages, [])]:
        result = RetrievalResult(
            passages=passages, extracted=documents, all_hits=[hit(URL)], issued_queries=["pavement"]
        )
        assert state.admit_retrieved(result) == 0
        assert state.budget.used == 0 and not state.pool
    assert len(state.all_hits) == 1
