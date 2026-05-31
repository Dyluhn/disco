"""perpleximanus.retrieval — Retrieval Engine + Citation/Grounding (BoD §9 + §14).

Two slots (discovery + extraction) behind provider interfaces, a quality
pipeline (query transformation → RRF → cross-encoder rerank with span
provenance), namespaced Space corpora (isolated silos), and a grounding pipeline
that NLI-verifies every claim against its cited passage. Implements the router's
`NLIVerifier`. A sibling of `tools`; the search/extract tools reach this engine
via the orchestrator-mediated CapabilitySet.
"""

from __future__ import annotations

from .engine import DefaultRetrievalEngine, RetrievalEngine
from .grounding import GroundingPipeline, extract_claims
from .models import (
    Claim,
    ExtractedDoc,
    GroundedAnswer,
    Passage,
    RetrievalRequest,
    RetrievalResult,
    SearchHit,
    VerifiedClaim,
)
from .nli import CrossEncoderNLIVerifier
from .providers import ExtractionProvider, SearchProvider
from .ranking import (
    Embedder,
    HashingEmbedder,
    LexicalReranker,
    QueryRewriter,
    Reranker,
    RouterQueryRewriter,
    reciprocal_rank_fusion,
)
from .vectorstore import (
    CorpusService,
    DefaultCorpusService,
    InMemoryVectorStore,
    VectorStore,
)

__all__ = [
    "Claim",
    "CorpusService",
    "CrossEncoderNLIVerifier",
    "DefaultCorpusService",
    "DefaultRetrievalEngine",
    "Embedder",
    "ExtractedDoc",
    "ExtractionProvider",
    "GroundedAnswer",
    "GroundingPipeline",
    "HashingEmbedder",
    "InMemoryVectorStore",
    "LexicalReranker",
    "Passage",
    "QueryRewriter",
    "Reranker",
    "RetrievalEngine",
    "RetrievalRequest",
    "RetrievalResult",
    "RouterQueryRewriter",
    "SearchHit",
    "SearchProvider",
    "VectorStore",
    "VerifiedClaim",
    "extract_claims",
    "reciprocal_rank_fusion",
]
