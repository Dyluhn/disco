from disco.retrieval.deep_research.source_identity import (
    canonical_work_key,
    distinct_work_count,
    group_by_work,
)
from disco.retrieval.models import Passage, SearchHit


def test_doi_resolvers_share_one_work_identity() -> None:
    urls = [
        "https://doi.org/10.1234/ABC.5678",
        "https://dx.doi.org/10.1234/abc.5678?utm_source=x",
        "doi:10.1234/abc.5678.",
    ]
    assert {canonical_work_key(url) for url in urls} == {"doi:10.1234/abc.5678"}


def test_arxiv_versions_share_work_but_different_ids_do_not() -> None:
    assert canonical_work_key("https://arxiv.org/abs/2401.01234v2") == "arxiv:2401.01234"
    assert canonical_work_key("https://export.arxiv.org/pdf/2401.01234.pdf") == "arxiv:2401.01234"
    assert canonical_work_key("https://arxiv.org/html/2401.01234") == "arxiv:2401.01234"
    assert canonical_work_key("https://arxiv.org/abs/2401.01235") != canonical_work_key(
        "https://arxiv.org/abs/2401.01234"
    )


def test_pmid_and_url_fallback_are_conservative() -> None:
    assert canonical_work_key("https://pubmed.ncbi.nlm.nih.gov/12345678/") == "pmid:12345678"
    assert canonical_work_key("https://www.ncbi.nlm.nih.gov/pubmed/12345678") == "pmid:12345678"
    first = canonical_work_key("https://example.org/paper?id=1&utm_medium=search")
    second = canonical_work_key("https://example.org/paper?id=2")
    assert first == "url:example.org/paper?id=1"
    assert first != second


def test_titles_are_never_used_to_merge_records_and_passages_are_preserved() -> None:
    first = SearchHit(url="https://example.org/a", title="Identical title")
    second = SearchHit(url="https://example.org/b", title="Identical title")
    assert distinct_work_count([first, second]) == 2

    passages = [
        Passage(id="p1", source_url="https://doi.org/10.1234/x", source_title="A", text="one"),
        Passage(id="p2", source_url="https://doi.org/10.1234/x", source_title="A", text="two"),
    ]
    groups = group_by_work(passages)
    assert len(groups) == 1
    assert [passage.id for passage in next(iter(groups.values()))] == ["p1", "p2"]
