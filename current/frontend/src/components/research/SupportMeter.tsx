/**
 * SupportMeter — the evidence-check bar: "N of M supported" over a segmented
 * bar that shows what the rest of the claims actually were.
 *
 * The bar used to be the only thing beside the number, so a report with 25
 * supported claims out of 104 read as 79 failures. It is split by the
 * checker's own four states (supported / possible contradiction / unresolved /
 * not checked), and the report-level meter names them in a legend — UNRESOLVED
 * is by far the biggest slice on a normal run and it is not a finding against
 * the report (UI-20).
 *
 * Pure, props-driven — no data fetching.
 */

import type { VerificationStatus } from "@/types/grounded";
import { CHECK_LABEL, type CheckCounts } from "@/lib/claimCheck";
import { cn } from "@/lib/cn";

interface SupportMeterProps {
  counts: CheckCounts;
  /** When set, show this quiet label instead of an empty bar at zero claims. */
  emptyLabel?: string;
  /** Name the colours underneath. The report header has the width for it; the
   *  per-section meter sits inline in a heading and does not. */
  legend?: boolean;
}

/** Draw order, and the colour each state owns. `unavailable` has no colour of
 *  its own — nothing was measured, so it reads as the empty part of the bar. */
const SEGMENTS: Array<{ status: VerificationStatus; bar: string; swatch: string }> = [
  { status: "supported", bar: "bg-supported", swatch: "bg-supported" },
  { status: "contradicted", bar: "bg-unsupported", swatch: "bg-unsupported" },
  { status: "unresolved", bar: "bg-weak", swatch: "bg-weak" },
  { status: "unavailable", bar: "bg-surface-2", swatch: "bg-surface-2 border border-hairline" },
];

function total(counts: CheckCounts): number {
  return counts.supported + counts.contradicted + counts.unresolved + counts.unavailable;
}

export function SupportMeter({ counts, emptyLabel, legend = false }: SupportMeterProps) {
  const claims = total(counts);

  if (claims === 0) {
    if (!emptyLabel) return null;
    return (
      <span className="font-ui text-[0.72rem] uppercase tracking-wide text-text-faint">
        {emptyLabel}
      </span>
    );
  }

  const present = SEGMENTS.filter((segment) => counts[segment.status] > 0);
  const title = present
    .map((segment) => `${counts[segment.status]} ${CHECK_LABEL[segment.status].toLowerCase()}`)
    .join(", ");

  return (
    <span
      className={cn("inline-flex gap-inline", legend ? "flex-col" : "items-center")}
      title={title}
    >
      <span className={cn("inline-flex items-center gap-inline", legend && "flex-wrap")}>
        <span className="font-ui text-[0.72rem] uppercase tracking-wide text-text-muted">
          {counts.supported} of {claims} supported
        </span>
        <span
          className={cn(
            "inline-flex overflow-hidden rounded-full bg-surface-2",
            legend ? "h-1.5 w-28" : "h-1.5 w-14",
          )}
          aria-hidden
        >
          {present.map((segment, index) => (
            <span
              key={segment.status}
              className={cn("h-full", segment.bar, index > 0 && "border-l border-bg")}
              style={{ width: `${Math.round((counts[segment.status] / claims) * 100)}%` }}
            />
          ))}
        </span>
      </span>
      {legend && (
        <span className="flex flex-wrap items-center gap-inline font-ui text-[0.68rem] text-text-faint">
          {present.map((segment) => (
            <span key={segment.status} className="flex items-center gap-hair">
              <span className={cn("size-1.5 rounded-full", segment.swatch)} aria-hidden />
              {counts[segment.status]} {CHECK_LABEL[segment.status].toLowerCase()}
            </span>
          ))}
        </span>
      )}
    </span>
  );
}
