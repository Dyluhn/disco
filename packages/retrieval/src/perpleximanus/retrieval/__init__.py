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
from .evaluation import (
    FrontierJudge,
    GroundingEvalMetrics,
    RetrievalEvalMetrics,
    cited_passage_rerank_positions,
    grounding_metrics,
    retrieval_metrics,
    select_for_judging,
)
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
from .wiring import research_answer, retrieval_capability_handlers

__all__ = [
    "Claim",
    "CorpusService",
    "CrossEncoderNLIVerifier",
    "DefaultCorpusService",
    "DefaultRetrievalEngine",
    "Embedder",
    "ExtractedDoc",
    "ExtractionProvider",
    "FrontierJudge",
    "GroundedAnswer",
    "GroundingEvalMetrics",
    "GroundingPipeline",
    "HashingEmbedder",
    "InMemoryVectorStore",
    "LexicalReranker",
    "Passage",
    "QueryRewriter",
    "Reranker",
    "RetrievalEngine",
    "RetrievalEvalMetrics",
    "RetrievalRequest",
    "RetrievalResult",
    "RouterQueryRewriter",
    "SearchHit",
    "SearchProvider",
    "VectorStore",
    "VerifiedClaim",
    "cited_passage_rerank_positions",
    "extract_claims",
    "grounding_metrics",
    "research_answer",
    "retrieval_capability_handlers",
    "retrieval_metrics",
    "reciprocal_rank_fusion",
    "select_for_judging",
]
