/**
 * splitThink — separate inline `<think>…</think>` reasoning from the answer.
 *
 * W-02: some models (Qwen-style) inline their chain-of-thought as raw
 * `<think>…</think>` tags directly in the streamed content. The request-side
 * strip (`_truncate_think_block`) doesn't cover the streaming path, so the raw
 * tags reach the UI. Rather than DELETE the reasoning (Dylan wants it kept, just
 * formatted), this splits the text into the visible `answer` and the collapsible
 * `reasoning` so the feed can render reasoning inside a closed `<details>`.
 *
 * Handles:
 *  - a single closed block,
 *  - multiple blocks (joined with a blank line, in order),
 *  - an UNCLOSED trailing `<think>` while streaming — everything after the open
 *    tag is treated as reasoning-in-progress,
 *  - plain text with no tags (passthrough → all answer, empty reasoning).
 *
 * Tag matching is case-insensitive; surrounding whitespace is trimmed.
 */

export interface ThinkSplit {
  /** The chain-of-thought extracted from `<think>` blocks (may be empty). */
  reasoning: string;
  /** The user-facing answer with all `<think>` blocks removed. */
  answer: string;
}

const OPEN = "<think>";
const CLOSE = "</think>";

// The model sometimes echoes the View's rotating "Reasoning:" / "Thought:" surface-form
// decoration back into its own thought, and because each turn re-decorates, the prefix
// STACKS — "Reasoning: Reasoning: Reasoning: …". The backend now strips this before
// storing, but historical events (and any provider that slips through) still carry it,
// so we defensively strip the leaked LEADING decorators here too. Scoped to the two
// surface forms the View applies to thoughts (Reasoning / Thought), matching the backend.
const DECOR_PREFIX = /^(?:\s*(?:reasoning|thought)\s*:\s*)+/i;

/** Strip leaked, stacked surface-form prefixes ("Reasoning: Reasoning: …") off the
 * FRONT of a thought. Only leading repeated decorators are removed; a legitimate
 * mid-sentence "Reasoning:" is untouched. Idempotent. */
export function stripReasoningPrefixes(text: string | null | undefined): string {
  return (text ?? "").replace(DECOR_PREFIX, "");
}

export function splitThink(text: string | null | undefined): ThinkSplit {
  // Strip any leaked stacked "Reasoning:" decoration first so it never shows in the answer.
  const src = stripReasoningPrefixes(text);
  if (!src) return { reasoning: "", answer: "" };

  const lower = src.toLowerCase();
  let i = 0;
  let reasoning = "";
  let answer = "";

  while (i < src.length) {
    const open = lower.indexOf(OPEN, i);
    if (open === -1) {
      // No more think blocks — the rest is answer.
      answer += src.slice(i);
      break;
    }
    // Text before the open tag is part of the answer.
    answer += src.slice(i, open);

    const contentStart = open + OPEN.length;
    const close = lower.indexOf(CLOSE, contentStart);
    if (close === -1) {
      // Unclosed trailing <think> (still streaming): everything after the open
      // tag is reasoning-in-progress.
      reasoning += (reasoning ? "\n\n" : "") + src.slice(contentStart);
      i = src.length;
      break;
    }
    reasoning += (reasoning ? "\n\n" : "") + src.slice(contentStart, close);
    i = close + CLOSE.length;
  }

  return { reasoning: reasoning.trim(), answer: answer.trim() };
}
