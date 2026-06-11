/**
 * RP-06 — ReplayScrubber: minimal range input + position label for stepping
 * through the event log. Rendered in the Build view footer; hidden during
 * live RUNNING.
 */

import type { ReplayState } from "@/lib/useReplay";

interface ReplayScrubberProps {
  replay: ReplayState;
}

export function ReplayScrubber({ replay }: ReplayScrubberProps) {
  const { position, max, atLive, seek } = replay;
  const label = max > 0 ? `Event ${position} of ${max}` : "No events";

  return (
    <div
      role="group"
      aria-label="Replay scrubber"
      data-testid="replay-scrubber"
      className="flex items-center gap-inline px-body py-hair"
    >
      <input
        type="range"
        aria-label="Replay position"
        data-testid="replay-scrubber-range"
        min={0}
        max={max}
        value={Math.min(position, max)}
        onChange={(e) => seek(Number(e.target.value))}
        className="h-4 flex-1 cursor-pointer appearance-none rounded bg-surface-2 accent-accent"
      />
      <span
        data-testid="replay-scrubber-label"
        className="shrink-0 font-mono text-[0.7rem] tabular-nums text-text-faint"
      >
        {label}
      </span>
    </div>
  );
}
