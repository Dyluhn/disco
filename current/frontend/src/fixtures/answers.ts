/*
 * Realistic GroundedAnswer fixtures — the UI is built against these first
 * (BoD §13.3 build discipline). Deliberately includes weak + unsupported claims
 * and paywalled/blocked/error sources so the honest-failure and verification
 * states are exercised from day one. Inline citations are `[[passage_id]]`
 * markers in prose text (anchored to the claim, never an end dump).
 */
import type { GroundedAnswer, Passage, SearchHit, VerifiedClaim } from "@/types/grounded";

const passages: Passage[] = [
  {
    id: "rrf_p0",
    source_url: "https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf",
    source_title: "Reciprocal Rank Fusion outperforms Condorcet and individual rank learning",
    text: "RRF computes a score for each document by summing 1/(k + rank) across the ranked lists in which it appears, with k a small constant (the authors use 60).",
  },
  {
    id: "elastic_p0",
    source_url: "https://www.elastic.co/guide/en/elasticsearch/reference/current/rrf.html",
    source_title: "Reciprocal rank fusion | Elasticsearch Guide",
    text: "Because RRF depends only on rank position and not on raw scores, it fuses result sets from different retrieval methods (e.g. BM25 and dense vectors) without score normalization.",
  },
  {
    id: "blog_p0",
    source_url: "https://example-search-blog.dev/posts/hybrid-retrieval",
    source_title: "Notes on hybrid retrieval",
    text: "In our experiments a smaller k made the fusion more sensitive to top-ranked items, though results varied by dataset.",
  },
  {
    id: "wiki_p0",
    source_url: "https://en.wikipedia.org/wiki/Learning_to_rank",
    source_title: "Learning to rank",
    text: "Learning-to-rank methods train a model on labelled relevance judgments to order documents.",
  },
];

const claims: VerifiedClaim[] = [
  {
    claim: { text: "RRF sums 1/(k+rank) across lists", cited_passage_ids: ["rrf_p0"] },
    verdict: "supported",
    best_passage_id: "rrf_p0",
    entailment_score: 0.94,
  },
  {
    claim: {
      text: "RRF needs no score normalization",
      cited_passage_ids: ["elastic_p0"],
    },
    verdict: "supported",
    best_passage_id: "elastic_p0",
    entailment_score: 0.89,
  },
  {
    claim: {
      text: "a smaller k always improves quality",
      cited_passage_ids: ["blog_p0"],
    },
    verdict: "weak",
    best_passage_id: "blog_p0",
    entailment_score: 0.46,
  },
  {
    claim: {
      text: "RRF requires labelled training data",
      cited_passage_ids: ["wiki_p0"],
    },
    verdict: "unsupported",
    best_passage_id: "wiki_p0",
    entailment_score: 0.08,
  },
];

const all_hits: SearchHit[] = [
  {
    url: "https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf",
    title: "Reciprocal Rank Fusion outperforms Condorcet… (Cormack et al., SIGIR'09)",
    snippet: "We present a simple method, Reciprocal Rank Fusion (RRF), that combines…",
    source_engine: "searxng",
    rank: 0,
    status: "ok",
  },
  {
    url: "https://www.elastic.co/guide/en/elasticsearch/reference/current/rrf.html",
    title: "Reciprocal rank fusion | Elasticsearch Guide",
    snippet: "RRF is a method for combining multiple result sets with different…",
    source_engine: "searxng",
    rank: 1,
    status: "ok",
  },
  {
    url: "https://example-search-blog.dev/posts/hybrid-retrieval",
    title: "Notes on hybrid retrieval",
    snippet: "A practitioner's notes on combining BM25 and dense retrieval…",
    source_engine: "searxng",
    rank: 2,
    status: "ok",
  },
  {
    url: "https://link.springer.com/article/10.1007/s00778-paywalled",
    title: "A survey of rank aggregation methods",
    snippet: "This survey reviews rank aggregation across information retrieval…",
    source_engine: "brave",
    rank: 3,
    status: "paywalled",
  },
  {
    url: "https://news.example.com/retrieval-explainer",
    title: "How modern search ranks results",
    snippet: "",
    source_engine: "searxng",
    rank: 4,
    status: "blocked",
  },
  {
    url: "https://defunct.example.org/old-paper",
    title: "(unreachable)",
    snippet: "",
    source_engine: "searxng",
    rank: 5,
    status: "not_found",
  },
];

export const rrfAnswer: GroundedAnswer = {
  query: "How does reciprocal rank fusion work, and when should I use it?",
  blocks: [
    {
      kind: "prose",
      id: "b0",
      text: "Reciprocal Rank Fusion (RRF) merges several ranked result lists into one. For each document it sums 1/(k + rank) over every list the document appears in, where k is a small constant (commonly 60) [[rrf_p0]]. Because the score depends only on a document's *position* in each list and never on raw scores, RRF can fuse fundamentally different retrieval methods — say BM25 and dense-vector search — without any score normalization [[elastic_p0]].",
      cited_passage_ids: ["rrf_p0", "elastic_p0"],
    },
    { kind: "heading", id: "h1", text: "The formula", level: 2 },
    {
      kind: "code",
      id: "b1",
      language: "python",
      code: "def rrf(rank_lists, k=60):\n    scores = {}\n    for ranks in rank_lists:\n        for rank, doc in enumerate(ranks):\n            scores[doc] = scores.get(doc, 0) + 1 / (k + rank)\n    return sorted(scores, key=scores.get, reverse=True)",
    },
    {
      kind: "callout",
      id: "b2",
      tone: "definition",
      title: "Why rank, not score?",
      text: "Raw relevance scores from BM25 and a vector index live on incompatible scales. Ranks are comparable across methods, so fusing on rank sidesteps normalization entirely.",
    },
    { kind: "heading", id: "h2", text: "When to use it", level: 2 },
    {
      kind: "prose",
      id: "b3",
      text: "RRF shines for hybrid retrieval: it is parameter-light and robust across datasets. Some practitioners report that a smaller k always improves quality [[blog_p0]], but that is dataset-dependent and not a general rule. It is sometimes claimed that RRF requires labelled training data [[wiki_p0]] — it does not; RRF is unsupervised and uses only rank positions.",
      cited_passage_ids: ["blog_p0", "wiki_p0"],
    },
    {
      kind: "table",
      id: "b4",
      caption: "RRF vs. learning-to-rank, at a glance",
      columns: ["", "RRF", "Learning-to-rank"],
      rows: [
        ["Needs labels", "No", "Yes"],
        ["Score normalization", "Not required", "Required"],
        ["Tuning surface", "Just k", "Features + model"],
      ],
    },
  ],
  claims,
  passages,
  all_hits,
  unsupported_count: 1,
  follow_ups: [
    "How is k chosen in practice?",
    "RRF vs. weighted score fusion — when does each win?",
    "Does RRF work for more than two result lists?",
  ],
};

/**
 * Fixture that exercises the `sheet` block (RP-11). Routed by the "sheet-demo"
 * query keyword in research.ts (mirrors the provider-error route) so the honest
 * preview card — no false-affordance download button — renders in the real
 * fixture-mode app for visual verification. The .xlsx itself is a workspace
 * artifact; the block is a preview, not a download surface.
 */
export const sheetDemoAnswer: GroundedAnswer = {
  ...rrfAnswer,
  query: "sheet-demo",
  blocks: [
    {
      kind: "prose",
      id: "sb0",
      text: "Here is the workbook generated for your request. It is saved to the conversation workspace; the preview below lists its sheets.",
    },
    {
      kind: "sheet",
      id: "sb1",
      title: "Q4 Sales Report",
      filename: "q4_sales.xlsx",
      sheet_names: ["Revenue", "Costs", "Summary"],
      formulas_evaluated: false,
    },
  ],
};
