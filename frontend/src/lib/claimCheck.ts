/**
 * One vocabulary for the automated evidence check.
 *
 * The report header counted four states (supported / possible contradiction /
 * unresolved / not checked) while the meter beside it said only "25 OF 104
 * SUPPORTED". Read together those say "three quarters of this report is
 * unsupported", which is not what the checker measured: most of the remainder
 * is UNRESOLVED — no source excerpt was found to match the sentence against.
 * That is a gap in the evidence, not a finding against the sentence (UI-20).
 *
 * The names and the counting rule live here so the header, the meter and the
 * per-claim list cannot drift apart again.
 */
import type { VerificationStatus, VerifiedClaim } from "@/types/grounded";

export const CHECK_LABEL: Record<VerificationStatus, string> = {
  supported: "Supported",
  contradicted: "Possible contradiction",
  unresolved: "Unresolved",
  unavailable: "Not checked",
};

/** The sentence a reader needs next to the counts, in plain words. */
export const UNRESOLVED_MEANING =
  "Unresolved means the checker found no source excerpt to match the sentence " +
  "against — it is not a contradiction.";

export type CheckCounts = Record<VerificationStatus, number>;

/**
 * The claim's own check status.
 *
 * Reports written before the checker recorded one carry only a verdict, and a
 * verdict of "unsupported" covers BOTH a real contradiction and a claim whose
 * cited passage id was unknown. Those two cannot be separated after the fact,
 * so an old claim is counted as unresolved — "nothing was established" is true
 * of either, where "possible contradiction" would be true of only one.
 */
function checkStatus(claim: VerifiedClaim): VerificationStatus {
  if (claim.verification_status) return claim.verification_status;
  return claim.verdict === "supported" ? "supported" : "unresolved";
}

export function claimCheckCounts(claims: VerifiedClaim[]): CheckCounts {
  const counts: CheckCounts = {
    supported: 0,
    contradicted: 0,
    unresolved: 0,
    unavailable: 0,
  };
  for (const claim of claims) counts[checkStatus(claim)] += 1;
  return counts;
}
