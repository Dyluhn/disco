"""A selected source read delivers usable text without a second mandatory lookup."""

import re

from disco.retrieval.deep_research._turn_context import _evidence_digest
from disco.retrieval.models import Passage


def passage(number, text):
    return Passage(
        id=f"p{number}",
        source_url=f"https://source.example/{number}",
        source_title=f"Source {number}",
        text=text,
    )


def test_short_source_includes_later_conditions_on_initial_read():
    body = "The device supports encrypted connections. " * 35
    body += "Strict configuration refuses plaintext fallback when authentication fails."
    context = _evidence_digest([passage(1, body)], {"p1"}, "encrypted connections")
    assert body in context
    assert "complete source" in context
    assert "inspect for more" not in context


def test_source_conditions_remain_visible_after_fresh_flag_clears():
    body = "The study measured one installation. " * 40
    body += "These results do not establish performance in other climates."
    pool = [passage(1, body)]
    assert body in _evidence_digest(pool, {"p1"}, "study results")
    assert body in _evidence_digest(pool, set(), "study results")


def test_large_pool_shares_one_bounded_text_allowance_without_hiding_sources():
    pool = [passage(i, f"Unique result {i}. " * 1200) for i in range(60)]
    context = _evidence_digest(pool, {p.id for p in pool}, "result")
    ranges = re.findall(r"excerpt (\d+):(\d+) of (\d+) characters", context)
    assert len(ranges) == len(pool)
    assert sum(int(end) - int(start) for start, end, _ in ranges) <= 48_000
    assert all(f"[{p.id}]" in context for p in pool)
    assert "inspect for more" in context


def test_long_source_window_preserves_the_selected_range():
    body = "Unrelated introductory material. " * 260
    body += "Measured throughput reached 927 units under continuous load. " * 20
    context = _evidence_digest([passage(1, body)], {"p1"}, "throughput 927 units")
    start, end, total = map(
        int, re.search(r"excerpt (\d+):(\d+) of (\d+) characters", context).groups()
    )
    assert total == len(body)
    assert " ".join(body[start:end].split()) in context
    assert "927 units" in context
    assert end - start <= 6000
