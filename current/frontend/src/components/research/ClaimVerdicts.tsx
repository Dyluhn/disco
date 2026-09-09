/**
 * ClaimVerdicts — a collapsible per-claim verdict breakdown under a section.
 * Leads with unsupported/weak claims (the honesty signal), then supported.
 * Each row: verdict chip + entailment score + claim text + link to best passage.
 *
 * Pure, props-driven — no data fetching.
 */

import { ChevronDown, ChevronRight, ExternalLink } from "lucide-react";
import { useState } from "react";
import { CHECK_LABEL } from "@/lib/claimCheck";
import { cn } from "@/lib/cn";
import type { Passage, VerifiedClaim, Verdict } from "@/types/grounded";

const VERDICT_LABEL: Record<Verdict, string> = {
  supported: "Supported",
  weak: "Weak",
  unsupported: "Unsupported",
};

const VERDICT_COLOR: Record<Verdict, string> = {
  supported: "bg-supported/15 text-supported border-supported/30",
  weak: "bg-weak/15 text-weak border-weak/30",
  unsupported: "bg-unsupported/15 text-unsupported border-unsupported/30",
};

function sortClaims(claims: VerifiedClaim[]): VerifiedClaim[] {
  const order: Record<Verdict, number> = { unsupported: 0, weak: 1, supported: 2 };
  return [...claims].sort((a, b) => order[a.verdict] - order[b.verdict]);
}

interface ClaimVerdictsProps {
  claims: VerifiedClaim[];
  passages: Passage[];
}

function evidenceDetail(claim: VerifiedClaim): string {
  if (claim.verification_status === "unavailable") return "The evidence check could not run.";
  if (claim.verification_reason === "missing_evidence") return "Cited evidence is missing.";
  if (claim.verification_reason === "conflicting_evidence") return "Cited passages disagree.";
  return `${claim.verification_status ? "Support estimate" : "Score"} ${(claim.entailment_score * 100).toFixed(0)}%`;
}

export function ClaimVerdicts({ claims, passages }: ClaimVerdictsProps) {
  const [open, setOpen] = useState(false);

  if (claims.length === 0) return null;

  const passageMap = new Map<string, Passage>();
  for (const p of passages) passageMap.set(p.id, p);

  const sorted = sortClaims(claims);
  const unsupportedCount = claims.filter((c) => c.verdict === "unsupported").length;
  const weakCount = claims.filter((c) => c.verdict === "weak").length;

  const summary =
    unsupportedCount > 0 || weakCount > 0
      ? `${unsupportedCount + weakCount} claims need attention`
      : `All ${claims.length} claims supported`;

  return (
    <details
      open={open}
      onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
      className="group mt-section rounded-card border border-hairline bg-surface-1/50"
    >
      <summary className="flex min-h-11 cursor-pointer items-center gap-inline px-body py-inline font-ui text-[0.82rem] text-text-muted select-none lg:min-h-0">
        <span className="transition-transform group-open:rotate-90">
          {open ? (
            <ChevronDown className="size-3.5" aria-hidden />
          ) : (
            <ChevronRight className="size-3.5" aria-hidden />
          )}
        </span>
        <span>Claim verification</span>
        <span className="ml-auto text-[0.74rem] text-text-faint">{summary}</span>
      </summary>
      <ul className="flex flex-col border-t border-hairline">
        {sorted.map((vc, i) => {
          const bestPassage = vc.best_passage_id
            ? passageMap.get(vc.best_passage_id)
            : undefined;
          return (
            <li
              key={i}
              className="flex items-start gap-inline border-b border-hairline px-body py-inline last:border-0"
            >
              <span
                className={cn(
                  "mt-px shrink-0 rounded-control border px-1.5 py-px font-ui text-[0.68rem] font-medium uppercase tracking-wide",
                  VERDICT_COLOR[vc.verdict],
                )}
              >
                {vc.verification_status
                  ? CHECK_LABEL[vc.verification_status]
                  : VERDICT_LABEL[vc.verdict]}
              </span>
              <div className="min-w-0 flex-1">
                <p className="font-ui text-[0.82rem] leading-snug text-text">
                  {vc.claim.text}
                </p>
                <span className="font-ui text-[0.72rem] text-text-faint">
                  {evidenceDetail(vc)}
                  {bestPassage && (
                    <>
                      {" · "}
                      <a
                        href={bestPassage.source_url}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex items-center gap-hair text-text-muted hover:text-link"
                      >
                        {bestPassage.source_title.slice(0, 40)}
                        {bestPassage.source_title.length > 40 ? "…" : ""}
                        <ExternalLink className="size-2.5" aria-hidden />
                      </a>
                    </>
                  )}
                </span>
              </div>
            </li>
          );
        })}
      </ul>
    </details>
  );
}
