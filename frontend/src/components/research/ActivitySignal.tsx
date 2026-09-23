/**
 * ActivitySignal — the honest replacement for the "⟳ Working" spinner.
 *
 * The old indicator was driven by `status === "RUNNING"`, so it span at the
 * same speed whether the engine had reported something two seconds ago or
 * nothing for ten minutes. This one is driven by the AGE of the newest event:
 *
 *   live      (< 20 s)   filled accent dot   "Live"
 *   buffered  (≥ 20 s)   hollow muted ring   "Waiting for model reply · 2:00"
 *   quiet     (20–60 s)  hollow muted ring   "quiet for 35 s"
 *   silent    (≥ 60 s)   hollow warn ring    "no signal for 1:12"
 *
 * No animation — nothing on screen claims progress the backend did not report.
 * `buffered` is calm on purpose: on a driver that does not stream, the quiet is
 * the transport's and the run is not stalling, so it must never wear the warn
 * ring that means "this may be stuck".
 */
import { cn } from "@/lib/cn";
import type { SignalLevel, SignalReading } from "@/lib/deepResearchHeartbeat";

const DOT: Record<SignalLevel, string> = {
  live: "size-1.5 rounded-full bg-accent",
  backoff: "size-1.5 rounded-full border border-text-faint",
  buffered: "size-1.5 rounded-full border border-text-faint",
  quiet: "size-1.5 rounded-full border border-text-faint",
  silent: "size-2 rounded-full border border-warn",
};

const TEXT: Record<SignalLevel, string> = {
  live: "text-accent",
  backoff: "text-text-muted",
  buffered: "text-text-muted",
  quiet: "text-text-muted",
  silent: "text-warn",
};

export function ActivitySignal({
  signal,
  className,
}: {
  signal: SignalReading;
  className?: string;
}) {
  return (
    <span
      data-dr-signal={signal.level}
      className={cn("flex items-center gap-hair font-ui", TEXT[signal.level], className)}
    >
      <span className={DOT[signal.level]} aria-hidden />
      <span>{signal.label}</span>
    </span>
  );
}
