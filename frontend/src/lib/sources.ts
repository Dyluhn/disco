import type { ExtractStatus, GroundedAnswer, Passage, Verdict } from "@/types/grounded";

/** Human-readable domain (no scheme, no www, no path) for source cards. */
export function cleanDomain(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

/** A neutral single-letter monogram stands in for a favicon — no third-party
 * favicon service (zero third-party egress, BoD §4.2) and no image CLS.
 * [VERIFY] real favicons can come from the extraction provider's page metadata. */
export function monogram(url: string): string {
  return cleanDomain(url).charAt(0).toUpperCase() || "•";
}

/** 1-based citation numeral for a passage = the position of its SOURCE in
 * first-seen `source_url` order — NOT the passage's raw index. One source often
 * contributes several passages, and the Sources panel (deriveSourceTiers) shows
 * ONE row per source in the same first-seen order; numbering chips by raw
 * passage index drifted +1 after every duplicate (found live 2026-07-09: the
 * chip said [5] where the sources list said [4]). Passages from the same source
 * now share that source's number, matching the panel row they deep-link to.
 * A passage with no URL gets its own number (keyed by its id) rather than a
 * bogus [0]. */
export function citationNumbers(answer: GroundedAnswer): Map<string, number> {
  const bySource = new Map<string, number>();
  const m = new Map<string, number>();
  for (const p of answer.passages) {
    const key = p.source_url || `#${p.id}`;
    let n = bySource.get(key);
    if (n === undefined) {
      n = bySource.size + 1;
      bySource.set(key, n);
    }
    m.set(p.id, n);
  }
  return m;
}

/** The verification verdict attached to a cited passage (via the claim that
 * cites it). Chroma is driven by this — the one place color is meaningful. */
export function verdictForPassage(answer: GroundedAnswer, passageId: string): Verdict {
  const claim = answer.claims.find((c) => c.claim.cited_passage_ids.includes(passageId));
  return claim?.verdict ?? "supported";
}

export function passageById(answer: GroundedAnswer, id: string): Passage | undefined {
  return answer.passages.find((p) => p.id === id);
}

export const STATUS_LABEL: Record<ExtractStatus, string> = {
  ok: "Read",
  paywalled: "Paywalled",
  blocked: "Blocked",
  not_found: "Not found",
  error: "Failed",
};

export function isFailedStatus(status?: ExtractStatus): boolean {
  return status !== undefined && status !== "ok";
}
