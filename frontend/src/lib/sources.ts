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

/** 1-based citation numeral for a passage = its position in the cited set. */
export function citationNumbers(answer: GroundedAnswer): Map<string, number> {
  const m = new Map<string, number>();
  answer.passages.forEach((p, i) => m.set(p.id, i + 1));
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
