/**
 * DeepResearchStoppingWall — what the run view shows between Stop and the
 * checkpoint.
 *
 * L29. The measured defect: Stop published a terminal IDLE 29 ms after the
 * click, so every run control vanished, no checkpoint or Resume appeared, and
 * the strip collapsed to "How it researched" — while the engine went on
 * emitting review and rework events for another eight minutes. The status was a
 * statement about nothing.
 *
 * The server now appends a NON-terminal `stop_requested` marker instead, and
 * this is its surface: the four parts a wall owes, each built from an event the
 * engine emitted (`deepResearchStopping`). It stands until the run writes its
 * own ending, at which point the existing checkpoint panel with Resume takes
 * over unchanged.
 *
 * No spinner and no timer-driven text: the only thing that moves is the age of
 * a real event, and the run has never said when it will land, so this never
 * guesses.
 */
import { Square } from "lucide-react";
import { cn } from "@/lib/cn";
import { stoppingCopy, stoppingForLabel } from "@/lib/deepResearchStopping";
import { TAP_TARGET } from "@/lib/tapTarget";
import type { DeepActivity } from "@/lib/deepResearchTrace";

interface Props {
  /** The latest reported fact of each kind, including the Stop marker. */
  activity: DeepActivity;
  /** Client clock, ticking, so the ages move while nothing arrives. */
  nowMs: number;
  /** Sources admitted so far, from the strip's own observed count. */
  sourcesDiscovered: number;
  /** End the run now (the existing kill frame). No checkpoint. */
  onKill: () => void;
}

export function DeepResearchStoppingWall({
  activity,
  nowMs,
  sourcesDiscovered,
  onKill,
}: Props) {
  const copy = stoppingCopy(activity, nowMs, sourcesDiscovered);
  if (copy === null) return null;
  const asked = stoppingForLabel(activity, nowMs);
  return (
    <section
      aria-label={copy.title}
      data-dr-stopping="panel"
      className="rounded-card border border-hairline-strong bg-surface-1 px-body py-body"
    >
      <div className="flex items-start gap-inline">
        <Square className="mt-px size-3.5 shrink-0 text-text-muted" aria-hidden />
        <div className="flex-1">
          <h2 className="font-ui text-[0.9rem] font-medium text-text">{copy.title}</h2>

          <p className="mt-hair font-ui text-[0.82rem] leading-snug text-text-muted">
            {copy.why}
            {asked && <span className="text-text-faint"> {asked}</span>}
          </p>

          {/* The step in flight, in the heartbeat's own words, plus how alive
              the stream is. Both are ages of real events, never a claim. */}
          <p
            data-dr-stopping="state"
            className="mt-inline font-mono text-[0.78rem] text-text-muted"
          >
            {copy.stateNow}
            {copy.signal && <span className="text-text-faint"> · {copy.signal}</span>}
          </p>

          <p className="mt-hair font-ui text-[0.82rem] leading-snug text-text">
            {copy.next}
          </p>

          <div className="mt-body flex flex-col-reverse gap-inline lg:flex-row lg:items-center">
            <button
              type="button"
              onClick={onKill}
              data-disco-control="dr.stopping.kill"
              className={cn(
                TAP_TARGET,
                "flex w-full items-center justify-center gap-hair rounded-control border border-hairline px-body py-hair font-ui text-[0.82rem] text-text-muted transition-colors hover:text-text lg:w-auto",
              )}
            >
              Kill the run now
            </button>
          </div>

          <p className="mt-inline font-ui text-[0.78rem] leading-snug text-text-faint">
            {copy.allowed}
          </p>
        </div>
      </div>
    </section>
  );
}
