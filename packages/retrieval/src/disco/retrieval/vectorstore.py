"""Vector store & Space corpora — retrieval-grounding-contract.md §4.

Namespaced storage: each namespace is an isolated corpus (= a Space silo). A
query is scoped to exactly the requested namespaces — **no cross-namespace
leak** (principle 8, the anti-cross-contamination guarantee). The in-memory
store is the v1 [INTERIOR] impl ([OPEN §24-D1]: pgvector vs a dedicated engine).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import ExtractedDoc, Passage
from .ranking import Embedder


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


@runtime_checkable
class VectorStore(Protocol):
    """[CONTRACT] Namespaced vector storage; queries never cross namespaces."""

    async def upsert(
        self, namespace: str, passages: list[Passage], vectors: list[list[float]]
    ) -> None: ...

    async def query(self, namespace: str, vector: list[float], *, top_k: int) -> list[Passage]: ...

    async def delete_namespace(self, namespace: str) -> None: ...


@runtime_checkable
class CorpusService(Protocol):
    """[CONTRACT] Manages Space corpora: ingest docs into a namespaced corpus
    that becomes an EXCLUSIVE retrieval silo for that Space."""

    async def ingest(self, corpus_id: str, *, owner_id: str, docs: list[ExtractedDoc]) -> None: ...

    async def remove(self, corpus_id: str) -> None: ...


class InMemoryVectorStore:
    """[INTERIOR] v1 in-memory namespaced store. Each namespace is a separate
    bucket; `query(ns, ...)` only ever reads `ns` — isolation by construction."""

    def __init__(self) -> None:
        self._ns: dict[str, list[tuple[Passage, list[float]]]] = {}

    async def upsert(
        self, namespace: str, passages: list[Passage], vectors: list[list[float]]
    ) -> None:
        bucket = self._ns.setdefault(namespace, [])
        existing = {p.id for p, _ in bucket}
        for passage, vector in zip(passages, vectors, strict=True):
            if passage.id not in existing:
                bucket.append((passage, vector))

    async def query(self, namespace: str, vector: list[float], *, top_k: int) -> list[Passage]:
        bucket = self._ns.get(namespace, [])  # ONLY this namespace — no leak
        ranked = sorted(bucket, key=lambda pv: _cosine(vector, pv[1]), reverse=True)
        return [p for p, _ in ranked[:top_k]]

    async def delete_namespace(self, namespace: str) -> None:
        self._ns.pop(namespace, None)


class DefaultCorpusService:
    """[CONTRACT] Ingests docs into a namespaced corpus via the Embedder +
    VectorStore. Tracks `owner_id` (BoD §4.1); corpora are never shared across
    owners — `corpus_for_owner` enforces the ownership check on retrieval."""

    def __init__(self, store: VectorStore, embedder: Embedder) -> None:
        self._store = store
        self._embedder = embedder
        self._owner: dict[str, str] = {}  # corpus_id -> owner_id

    async def ingest(self, corpus_id: str, *, owner_id: str, docs: list[ExtractedDoc]) -> None:
        self._owner[corpus_id] = owner_id
        passages = [
            p.model_copy(update={"corpus_id": corpus_id}) for doc in docs for p in doc.passages
        ]
        if not passages:
            return
        vectors = await self._embedder.embed([p.text for p in passages])
        await self._store.upsert(corpus_id, passages, vectors)

    async def remove(self, corpus_id: str) -> None:
        await self._store.delete_namespace(corpus_id)
        self._owner.pop(corpus_id, None)

    def corpus_for_owner(self, corpus_id: str, owner_id: str) -> bool:
        """[CONTRACT] True iff this corpus belongs to `owner_id` — the caller
        must gate retrieval on this so no owner reads another's corpus (§4)."""
        return self._owner.get(corpus_id) == owner_id
