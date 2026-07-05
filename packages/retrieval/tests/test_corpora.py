"""Vector store & Space corpora — retrieval-grounding-contract.md §8.3.

The namespace-isolation test is the headline (anti-cross-contamination,
principle 8)."""

from __future__ import annotations

from disco.retrieval import (
    DefaultCorpusService,
    DiskVectorStore,
    ExtractedDoc,
    HashingEmbedder,
    InMemoryVectorStore,
    Passage,
)


def _doc(url: str, text: str) -> ExtractedDoc:
    return ExtractedDoc(
        url=url,
        title=url,
        content=text,
        passages=[Passage(id=f"{url}_p0", source_url=url, source_title=url, text=text)],
    )


async def test_namespace_isolation_no_cross_contamination():
    """A query scoped to corpus A never returns a passage from corpus B."""
    store = InMemoryVectorStore()
    embedder = HashingEmbedder()
    corpora = DefaultCorpusService(store, embedder)
    await corpora.ingest("space_A", owner_id="local", docs=[_doc("a1", "SENTINEL_ALPHA only here")])
    await corpora.ingest("space_B", owner_id="local", docs=[_doc("b1", "SENTINEL_BETA only here")])

    qvec = (await embedder.embed(["SENTINEL_BETA"]))[0]
    a_results = await store.query("space_A", qvec, top_k=10)
    assert a_results, "namespace A should return its own passage"
    assert all("SENTINEL_BETA" not in p.text for p in a_results)  # never leaks B into A
    assert all(p.corpus_id == "space_A" for p in a_results)


async def test_ownership_is_tracked():
    store = InMemoryVectorStore()
    corpora = DefaultCorpusService(store, HashingEmbedder())
    await corpora.ingest("space_alice", owner_id="alice", docs=[_doc("a1", "alice notes")])
    assert corpora.corpus_for_owner("space_alice", "alice") is True
    assert corpora.corpus_for_owner("space_alice", "bob") is False  # not bob's to read


async def test_ingest_round_trip_and_delete():
    store = InMemoryVectorStore()
    embedder = HashingEmbedder()
    corpora = DefaultCorpusService(store, embedder)
    await corpora.ingest("space_X", owner_id="local", docs=[_doc("x1", "provenance preserved")])

    qvec = (await embedder.embed(["provenance"]))[0]
    got = await store.query("space_X", qvec, top_k=5)
    assert len(got) == 1
    assert got[0].source_url == "x1" and got[0].corpus_id == "space_X"  # provenance intact

    await corpora.remove("space_X")
    assert await store.query("space_X", qvec, top_k=5) == []  # gone


async def test_disk_vector_store_persists_across_reinstantiation(tmp_path):
    embedder = HashingEmbedder()
    store = DiskVectorStore(tmp_path / "vectors")
    corpora = DefaultCorpusService(store, embedder)
    await corpora.ingest("space_durable", owner_id="local", docs=[_doc("d1", "durable alpha")])

    restarted = DiskVectorStore(tmp_path / "vectors")
    qvec = (await embedder.embed(["durable alpha"]))[0]
    got = await restarted.query("space_durable", qvec, top_k=5)

    assert len(got) == 1
    assert got[0].source_url == "d1"
    assert got[0].corpus_id == "space_durable"
