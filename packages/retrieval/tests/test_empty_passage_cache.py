"""A fetched shell is not usable evidence; cache only actual passage text."""

import asyncio
import json

import httpx
import pytest
from disco.retrieval._crawl4ai import Crawl4aiExtractionProvider
from disco.retrieval._extraction_text import CORRUPTED_TEXT_ERROR
from disco.retrieval._retrieval_cache import RetrievalCache, cached_docs, store_docs
from disco.retrieval.models import ExtractedDoc, Passage

URL = "https://example.com/article"
ARTICLE = "The published article contains actual citable evidence about the reported event."


def _doc(texts):
    return ExtractedDoc(
        url=URL,
        title="Article",
        content="A retained document.",
        passages=[
            Passage(id=f"p{i}", source_url=URL, source_title="Article", text=text)
            for i, text in enumerate(texts)
        ],
        fetched_ok=True,
        status="ok",
    )


def _row(text):
    return {
        "url": URL,
        "success": True,
        "status_code": 200,
        "metadata": {"title": "Loading"},
        "markdown": {"fit_markdown": text},
    }


@pytest.mark.parametrize("texts", [[], [""], [" \n\t"], ["", " \n"]])
def test_cache_rejects_missing_or_blank_passages_on_admission_and_reuse(tmp_path, texts):
    cache = RetrievalCache(tmp_path / "cache.sqlite")
    doc = _doc(texts)
    store_docs(cache, [doc], namespace="provider:")
    assert cache.get_page("provider:" + URL) is None

    # Old successful-shell entries may already be on disk.
    cache.put_page("provider:" + URL, doc.model_dump(mode="json"))
    assert cached_docs(cache, [URL], namespace="provider:") == {}


@pytest.mark.parametrize("texts", [["7"], ["short fact"], [" ", "7"]])
def test_cache_preserves_legitimate_short_passages_from_other_extractors(tmp_path, texts):
    cache = RetrievalCache(tmp_path / "cache.sqlite")
    doc = _doc(texts)
    store_docs(cache, [doc], namespace="provider:")
    assert cached_docs(cache, [URL], namespace="provider:") == {URL: doc}


async def test_http_successful_shell_is_unusable_uncached_and_refetched_on_next_call(tmp_path):
    posted = []

    def handler(request):
        posted.append(json.loads(request.content))
        text = "Loading" if len(posted) <= 2 else ARTICLE
        return httpx.Response(200, json={"results": [_row(text)]})

    cache = RetrievalCache(tmp_path / "cache.sqlite")
    provider = Crawl4aiExtractionProvider(
        "http://crawler", transport=httpx.MockTransport(handler), cache=cache
    )
    shell = await provider.extract(URL)
    assert not shell.fetched_ok and shell.status == "error"
    assert shell.error == "source returned no usable text passages"
    assert shell.content == "Loading" and shell.title == "Loading"
    assert shell.passages == []
    assert len(posted) == 2  # Only one delayed retry, still no usable evidence.
    assert "delay_before_return_html" not in posted[0]["crawler_config"]["params"]
    assert posted[1]["crawler_config"]["params"]["delay_before_return_html"] == 3.0
    assert cache.get_page(provider._cache_namespace + URL) is None

    recovered = await provider.extract(URL)
    assert recovered.fetched_ok and recovered.passages[0].text == ARTICLE
    assert len(posted) == 3
    assert await provider.extract(URL) == recovered
    assert len(posted) == 3  # The useful extraction is still cached.


async def test_legacy_shell_cache_entry_is_bypassed_and_replaced_by_real_passages(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"results": [_row(ARTICLE)]})

    cache = RetrievalCache(tmp_path / "cache.sqlite")
    provider = Crawl4aiExtractionProvider(
        "http://crawler", transport=httpx.MockTransport(handler), cache=cache
    )
    shell = _doc([]).model_copy(update={"content": "Loading"})
    cache.put_page(provider._cache_namespace + URL, shell.model_dump(mode="json"))
    result = await provider.extract(URL)
    assert len(calls) == 1 and result.fetched_ok and result.passages
    assert cache.get_page(provider._cache_namespace + URL)["passages"]
    assert await provider.extract(URL) == result
    assert len(calls) == 1


@pytest.mark.parametrize(
    "code,expected", [(401, "blocked"), (403, "blocked"), (402, "paywalled"), (404, "not_found")]
)
def test_unusable_shell_does_not_override_http_failure_classification(code, expected):
    row = {**_row("Loading"), "success": False, "status_code": code}
    doc = Crawl4aiExtractionProvider("http://crawler")._to_doc(URL, row)
    assert not doc.fetched_ok and doc.status == expected
    assert doc.error == f"http {code}"


def test_corruption_is_rejected_before_empty_passage_classification():
    doc = Crawl4aiExtractionProvider("http://crawler")._to_doc(URL, _row("\ufffd" * 50))
    assert not doc.fetched_ok and doc.error == CORRUPTED_TEXT_ERROR


def test_successful_crawler_content_retains_existing_passage_threshold():
    crawler = Crawl4aiExtractionProvider("http://crawler")
    assert not crawler._to_doc(URL, _row("x" * 39)).fetched_ok
    doc = crawler._to_doc(URL, _row("x" * 40))
    assert doc.fetched_ok and doc.passages[0].text == "x" * 40


@pytest.mark.parametrize("final_code", [200, 302])
async def test_delayed_success_recovers_passages_and_respects_final_redirect_status(
    tmp_path, final_code
):
    posted = []

    def handler(request):
        posted.append(json.loads(request.content))
        if len(posted) == 1:
            result = _row("Loading")
        else:
            result = {
                **_row(ARTICLE),
                "status_code": final_code,
                "redirected_status_code": 200,
                "redirected_url": "https://example.com/publisher",
                "metadata": {"title": "Published article"},
            }
        return httpx.Response(200, json={"results": [result]})

    provider = Crawl4aiExtractionProvider(
        "http://crawler",
        transport=httpx.MockTransport(handler),
        cache=RetrievalCache(tmp_path / "cache.sqlite"),
    )
    result = await provider.extract(URL)
    assert result.fetched_ok and result.status == "ok"
    assert result.title == "Published article" and result.passages[0].text == ARTICLE
    assert result.url == URL and result.passages[0].source_url == URL
    assert len(posted) == 2
    assert posted[1]["urls"] == [URL]
    assert posted[1]["crawler_config"]["params"]["delay_before_return_html"] == 3.0
    assert "delay_before_return_html" not in posted[0]["crawler_config"]["params"]
    assert await provider.extract(URL) == result
    assert len(posted) == 2


@pytest.mark.parametrize("failure", ["http", "malformed"])
async def test_delayed_retry_failure_never_replays_successful_batch_siblings(failure):
    good = "https://example.com/good"
    posted = []

    def handler(request):
        body = json.loads(request.content)
        posted.append(body["urls"])
        if len(posted) == 1:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {**_row(ARTICLE), "url": good},
                        _row("Loading"),
                    ]
                },
            )
        if failure == "http":
            return httpx.Response(503, text="temporarily unavailable")
        return httpx.Response(200, text="not json")

    provider = Crawl4aiExtractionProvider("http://crawler", transport=httpx.MockTransport(handler))
    first, second = await provider.extract_many([good, URL])
    assert first.fetched_ok and first.passages
    assert not second.fetched_ok and "empty source retry failed" in second.error
    assert posted == [[good, URL], [URL]]


async def test_cancellation_during_empty_retry_propagates_without_another_attempt():
    posted = []

    def handler(request):
        posted.append(json.loads(request.content))
        if len(posted) == 1:
            return httpx.Response(200, json={"results": [_row("Loading")]})
        raise asyncio.CancelledError

    provider = Crawl4aiExtractionProvider("http://crawler", transport=httpx.MockTransport(handler))
    with pytest.raises(asyncio.CancelledError):
        await provider.extract(URL)
    assert len(posted) == 2


@pytest.mark.parametrize(
    "result",
    [
        None,
        {
            "url": URL,
            "success": True,
            "status_code": 200,
            "error_message": "source returned no usable text passages",
        },
        {**_row("Loading"), "success": "false"},
        {"url": URL, "success": True, "status_code": 200},  # Missing markdown is malformed.
        {**_row("Loading"), "success": False, "status_code": 404},
        {**_row("Loading"), "success": False, "status_code": 403},
        {**_row("Loading"), "success": False, "status_code": 402},
        {**_row("Loading"), "success": False, "status_code": 500},
        {
            **_row("Loading"),
            "success": False,
            "status_code": 500,
            "error_message": "source returned no usable text passages",
        },
        {
            **_row("Loading"),
            "success": False,
            "status_code": 200,
            "error_message": "source returned no usable text passages",
        },
    ],
)
async def test_initial_failure_or_missing_markdown_never_gets_delayed_retry(monkeypatch, result):
    async def no_archive(*args, **kwargs):
        return {}

    monkeypatch.setattr("disco.retrieval._crawl4ai.recover_blocked_pages", no_archive)
    posted = []

    def handler(request):
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"results": [] if result is None else [result]})

    provider = Crawl4aiExtractionProvider("http://crawler", transport=httpx.MockTransport(handler))
    doc = await provider.extract(URL)
    assert not doc.fetched_ok
    assert len(posted) == 1
    assert "delay_before_return_html" not in posted[0]["crawler_config"]["params"]


@pytest.mark.parametrize("content", ["", " \n\t"])
async def test_successful_empty_markdown_gets_only_one_bounded_retry(content):
    posted = []

    def handler(request):
        posted.append(request)
        return httpx.Response(200, json={"results": [_row(content)]})

    doc = await Crawl4aiExtractionProvider(
        "http://crawler", transport=httpx.MockTransport(handler)
    ).extract(URL)
    assert not doc.fetched_ok and doc.error == "source returned no usable text passages"
    assert len(posted) == 2


async def test_challenge_remains_blocked_without_delayed_crawl_retry(monkeypatch):
    async def no_archive(*args, **kwargs):
        return {}

    monkeypatch.setattr("disco.retrieval._crawl4ai.recover_blocked_pages", no_archive)
    posted = []
    text = (
        "# Making sure you're not a bot! Loading... You are seeing this because "
        "the administrator of this website has set up Anubis to protect the server. "
        "Anubis uses a Proof-of-Work scheme in the vein of Hashcash."
    )

    def handler(request):
        posted.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        **_row(text),
                        "metadata": {"title": "Making sure you're not a bot!"},
                    }
                ]
            },
        )

    doc = await Crawl4aiExtractionProvider(
        "http://crawler", transport=httpx.MockTransport(handler)
    ).extract(URL)
    assert not doc.fetched_ok and doc.status == "blocked"
    assert "challenge" in doc.error
    assert len(posted) == 1
