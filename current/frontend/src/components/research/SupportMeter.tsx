/**
 * SupportMeter — a compact per-section "N of M claims supported" bar with a
 * 3-segment breakdown (supported / weak / unsupported), colored with the
 * existing text-supported / text-weak / text-unsupported tokens.
 *
 * Pure, props-driven — no data fetching. Works with any source of counts.
 */

import { cn } from "@/lib/cn";

interface SupportMeterProps {
  supported: number;
  weak: number;
  unsupported: number;
  /** When true, show a quiet "no claim data" label instead of an empty bar. */
  emptyLabel?: string;
}

function segmentPct(count: number, total: number): number {
  if (total <= 0) return 0;
  return Math.round((count / total) * 100);
}

export function SupportMeter({ supported, weak, unsupported, emptyLabel }: SupportMeterProps) {
  const total = supported + weak + unsupported;

  if (total === 0) {
    if (!emptyLabel) return null;
    return (
      <span className="font-ui text-[0.72rem] uppercase tracking-wide text-text-faint">
        {emptyLabel}
      </span>
    );
  }

  const pctSupported = segmentPct(supported, total);
  const pctWeak = segmentPct(weak, total);
  const pctUnsupported = segmentPct(unsupported, total);

  return (
    <span
      className="inline-flex items-center gap-inline"
      title={`${supported} supported, ${weak} weak, ${unsupported} unsupported`}
    >
      <span className="font-ui text-[0.72rem] uppercase tracking-wide text-text-muted">
        {supported} of {total} supported
      </span>
      <span
        className="inline-flex h-1.5 w-14 overflow-hidden rounded-full bg-surface-2"
        aria-hidden
      >
        {pctSupported > 0 && (
          <span
            className="h-full bg-supported"
            style={{ width: `${pctSupported}%` }}
          />
        )}
        {pctWeak > 0 && (
          <span
            className={cn("h-full bg-weak", pctSupported > 0 && "border-l border-bg")}
            style={{ width: `${pctWeak}%` }}
          />
        )}
        {pctUnsupported > 0 && (
          <span
            className={cn("h-full bg-unsupported", (pctSupported > 0 || pctWeak > 0) && "border-l border-bg")}
            style={{ width: `${pctUnsupported}%` }}
          />
        )}
      </span>
    </span>
  );
}
