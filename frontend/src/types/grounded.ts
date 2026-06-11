/*
 * Front-end mirror of the shapes this surface renders — kept faithful to
 * retrieval-grounding-contract.md (GroundedAnswer/Passage/SearchHit/
 * VerifiedClaim) and event-state-contract.md §7 (the WS frames). These are the
 * only contract types the UI knows; rendering is driven entirely by them.
 */

export type ExtractStatus = "ok" | "paywalled" | "blocked" | "not_found" | "error";
export type Verdict = "supported" | "weak" | "unsupported";

export interface SearchHit {
  url: string;
  title: string;
  snippet: string; // provider snippet — NEVER cited as content (§1.4)
  source_engine: string;
  rank: number;
  /** discovery-set extraction outcome, for explicit-failure rendering (§2.2) */
  status?: ExtractStatus;
}

export interface Passage {
  id: string; // stable citation id, e.g. "src3_p7"
  source_url: string;
  source_title: string;
  text: string; // the corroborating span shown in the hover card
  char_start?: number | null;
  char_end?: number | null;
  corpus_id?: string | null;
}

export interface Claim {
  text: string;
  cited_passage_ids: string[];
}

export interface VerifiedClaim {
  claim: Claim;
  verdict: Verdict;
  best_passage_id: string | null;
  entailment_score: number;
}

/**
 * Structured answer content (BoD §13.3: components, not a markdown blob). The
 * backend streams these blocks; the UI renders a component per `kind`. Each
 * carries the cited passage ids that anchor its inline citations.
 */
export type AnswerBlock =
  | { kind: "prose"; id: string; text: string; cited_passage_ids?: string[] }
  | { kind: "heading"; id: string; text: string; level: 2 | 3 }
  | { kind: "table"; id: string; columns: string[]; rows: string[][]; caption?: string }
  | { kind: "code"; id: string; language: string; code: string }
  | {
      kind: "chart";
      id: string;
      chart_type: "bar" | "line" | "pie" | "scatter";
      data: any; // Narrow per-type schemas handled in component
      title?: string;
      x_label?: string;
      y_label?: string;
      cited_passage_ids?: string[];
    }
  | {
      kind: "callout";
      id: string;
      tone: "note" | "definition" | "warning";
      title: string;
      text: string;
    }
  | {
      kind: "sheet";
      id: string;
      title: string;
      filename: string;
      sheet_names: string[];
      formulas_evaluated: false;
    };

export interface GroundedAnswer {
  query: string;
  blocks: AnswerBlock[]; // the document, as structured components
  claims: VerifiedClaim[]; // per-claim verification verdicts
  passages: Passage[]; // the cited sources (provenance)
  all_hits: SearchHit[]; // the full discovery set — "All Searched"
  unsupported_count: number;
  follow_ups: string[]; // context-aware follow-up suggestions
}

/** A user-adjusted retrieval scope (re-scope controls → a re-issue mutation). */
export interface ReScope {
  query: string;
  domains_deny?: string[];
  drop_weak?: boolean;
  /** Per-conversation lead-model override (the main-screen pill → CallContext
   *  .model_override). null/undefined → use the Settings default. */
  model_override?: string | null;
  /** The Think toggle: run the answerer in reasoning mode (it thinks first, then
   *  the answer streams). Honored by the live backend for reasoning models. */
  think?: boolean;
}

// ---- WS frames (event-state §7.2) — what the stream delivers ----------------

export interface TokenFrame {
  type: "token";
  token: string;
  block_id: string; // which block the token appends to (ephemeral)
}
export interface BlockFrame {
  type: "block"; // a completed structured block (source of truth for its text)
  block: AnswerBlock;
}
export interface StateFrame {
  type: "state";
  status: "running" | "finished" | "error";
}
export interface FinalFrame {
  type: "final"; // the authoritative GroundedAnswer; tokens reconcile to this
  answer: GroundedAnswer;
}
export interface ErrorFrame {
  type: "error";
  message: string;
}
export type StreamFrame = TokenFrame | BlockFrame | StateFrame | FinalFrame | ErrorFrame;
