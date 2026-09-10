"""Conservative work-level identities for deep-research evidence.

Passage IDs remain the grounding and citation unit.  These helpers provide a
second identity only for source-diversity and corroboration accounting.  A
strong identifier (DOI, arXiv ID, or PMID) wins; otherwise the canonical URL
is used.  Titles are deliberately ignored: similar titles are not sufficient
evidence that two records describe the same work.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from urllib.parse import unquote

from ..models import Passage, SearchHit
from ..url_policy import source_url_key

_DOI_RE = re.compile(r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*)?(10\.\d{4,9}/[^\s<>\"']+)", re.I)
_ARXIV_RE = re.compile(
    r"(?:arxiv(?::|\.org/(?:abs/|pdf/|html/))|/(?:abs|pdf|html)/)([a-z-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?(?:$|[/?#])",
    re.I,
)
_PMID_RE = re.compile(
    r"(?:pubmed\.ncbi\.nlm\.nih\.gov/|ncbi\.nlm\.nih\.gov/pubmed/|pmid[:/])([0-9]+)",
    re.I,
)
_Source = str | SearchHit | Passage | Mapping[str, object]


def _url_for(source: _Source) -> str:
    if isinstance(source, str):
        return source.strip()
    if isinstance(source, (SearchHit, Passage)):
        return source.url if isinstance(source, SearchHit) else source.source_url
    # This accepts serialized hits/passages at API boundaries without making
    # title-based guesses.  Missing/non-string URLs simply use an empty key.
    value = source.get("url", source.get("source_url", ""))
    return value.strip() if isinstance(value, str) else ""


def canonical_work_key(source: _Source) -> str:
    """Return a stable work identity for a URL or retrieval record.

    DOI, arXiv, and PMID identifiers are normalized across common resolver
    URLs and version suffixes.  URL identity is the conservative fallback;
    no fuzzy title matching is performed.
    """

    url = unquote(_url_for(source)).strip()
    if not url:
        return "url:"

    doi_match = _DOI_RE.search(url)
    if doi_match:
        doi = doi_match.group(1).split("?", 1)[0].split("#", 1)[0]
        doi = doi.rstrip(".,;:)]}").lower()
        return f"doi:{doi}"

    arxiv_match = _ARXIV_RE.search(url)
    if arxiv_match:
        return f"arxiv:{arxiv_match.group(1).lower()}"

    pmid_match = _PMID_RE.search(url)
    if pmid_match:
        return f"pmid:{pmid_match.group(1)}"

    return f"url:{source_url_key(url)}"


def group_by_work[T](
    sources: Iterable[T],
    *,
    key: Callable[[T], str] = canonical_work_key,
) -> dict[str, list[T]]:
    """Group records by work identity while preserving input order.

    The returned lists contain the original records, so passage-level
    grounding/citation data is never collapsed or discarded.
    """

    grouped: dict[str, list[T]] = defaultdict(list)
    for source in sources:
        grouped[key(source)].append(source)
    return dict(grouped)


def distinct_work_count(sources: Iterable[_Source]) -> int:
    """Count canonical works, treating multiple passages as one work."""

    return len(group_by_work(sources))


__all__ = ["canonical_work_key", "distinct_work_count", "group_by_work"]
