"""The retrieval cache itself — TTLs, key identity, and how it gets wired.

The provider-level behaviour (a repeat search served from disk, a degraded
answer never stored, a failed page never stored) is proven in test_live.py
against a mock transport. What is proven here is the store underneath it: that
an entry stops being served once its TTL has passed, that two spellings of the
same question are one key while two scopes are two, and that the cache is built
only where there is a data dir to put it.

`now` is injected everywhere — an expiry test that waits out a real 24 hours is
not a test.
"""

from __future__ import annotations

from pathlib import Path

from disco.retrieval._provider_wiring import build_retrieval_cache
from disco.retrieval._retrieval_cache import (
    CACHE_FILENAME,
    PAGE_TTL_S,
    SEARCH_TTL_S,
    RetrievalCache,
    search_cache_key,
)
from disco.retrieval.live import _env_data_dir, _make_extraction


def _cache(tmp_path: Path, clock: list[float]) -> RetrievalCache:
    return RetrievalCache(tmp_path / "sub" / CACHE_FILENAME, now=lambda: clock[0])


def test_a_stored_search_comes_back_unchanged(tmp_path: Path) -> None:
    clock = [0.0]
    cache = _cache(tmp_path, clock)
    cache.put_search("k", [{"url": "https://a.example/x", "title": "A"}])
    assert cache.get_search("k") == [{"url": "https://a.example/x", "title": "A"}]


def test_an_unknown_key_is_a_miss_not_an_error(tmp_path: Path) -> None:
    assert _cache(tmp_path, [0.0]).get_search("never-stored") is None
    assert _cache(tmp_path, [0.0]).get_page("https://a.example/never") is None


def test_a_search_stops_being_served_once_its_day_is_up(tmp_path: Path) -> None:
    clock = [1_000.0]
    cache = _cache(tmp_path, clock)
    cache.put_search("k", [{"url": "https://a.example/x"}])
    clock[0] += SEARCH_TTL_S - 1
    assert cache.get_search("k") is not None
    clock[0] += 2
    assert cache.get_search("k") is None


def test_a_page_stops_being_served_once_its_week_is_up(tmp_path: Path) -> None:
    clock = [1_000.0]
    cache = _cache(tmp_path, clock)
    cache.put_page("https://a.example/p", {"url": "https://a.example/p", "content": "body"})
    clock[0] += PAGE_TTL_S - 1
    assert cache.get_page("https://a.example/p") is not None
    clock[0] += 2
    assert cache.get_page("https://a.example/p") is None


def test_a_page_survives_longer_than_a_search() -> None:
    """The web moves under a search within a day; the TEXT of a page that
    already read does not."""
    assert PAGE_TTL_S > SEARCH_TTL_S


def test_the_search_key_ignores_case_and_spacing(tmp_path: Path) -> None:
    assert search_cache_key("http://s", "general", "", "  Some   QUESTION ") == search_cache_key(
        "http://s", "general", "", "some question"
    )


def test_the_search_key_separates_instance_scope_and_time_window() -> None:
    base = search_cache_key("http://s", "general", "", "q")
    assert base != search_cache_key("http://other", "general", "", "q")
    assert base != search_cache_key("http://s", "general,science", "", "q")
    assert base != search_cache_key("http://s", "general", "week", "q")


def test_no_data_dir_means_no_cache_rather_than_a_directory_in_someones_home() -> None:
    assert build_retrieval_cache("") is None
    assert build_retrieval_cache("   ") is None


def test_a_data_dir_puts_the_cache_file_where_an_operator_can_find_it(tmp_path: Path) -> None:
    cache = build_retrieval_cache(str(tmp_path))
    assert cache is not None
    cache.put_search("k", [{"url": "https://a.example/x"}])
    assert (tmp_path / CACHE_FILENAME).exists()


def test_the_same_data_dir_reuses_one_connection(tmp_path: Path) -> None:
    """Providers are rebuilt per run; a fresh sqlite connection each time would
    leak a file descriptor per run for the life of the server."""
    assert build_retrieval_cache(str(tmp_path)) is build_retrieval_cache(str(tmp_path))


def test_the_data_dir_is_read_from_the_wiring_mapping_not_the_real_environment() -> None:
    assert _env_data_dir({}) == ""
    assert _env_data_dir({"DISCO_DATA_DIR": "/data"}) == "/data"
    assert _env_data_dir({"PMX_DATA_DIR": "/legacy"}) == "/legacy"  # legacy name honored
    assert _env_data_dir({"DISCO_DATA_DIR": "/new", "PMX_DATA_DIR": "/legacy"}) == "/new"


def test_only_the_crawler_gets_a_page_cache_and_only_with_a_data_dir(tmp_path: Path) -> None:
    with_dir = _make_extraction("crawl4ai", "http://h", "", str(tmp_path))
    without = _make_extraction("crawl4ai", "http://h", "")
    assert with_dir._cache is not None
    assert without._cache is None
