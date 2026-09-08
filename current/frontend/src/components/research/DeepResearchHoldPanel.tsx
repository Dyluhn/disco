/**
 * DeepResearchHoldPanel — the user's choice while the loop waits on the search
 * pool.
 *
 * The loop resumes by itself; this panel exists because a run that goes quiet
 * for four minutes with no explanation reads as a hang. So it states the four
 * things a wall owes the person in front of it — why it is up, what state the
 * run is in now, what happens next unattended, and what is still allowed — and
 * offers the only two decisions that actually exist:
 *
 *   Continue waiting → nothing is sent. The panel collapses to a one-line
 *                      heartbeat entry and the loop keeps its own schedule.
 *   Stop             → the existing `cancel` frame → PAUSED checkpoint with
 *                      Resume. The gathered sources survive.
 *
 * It is deliberately NOT styled as an error: same quiet aside treatment as the
 * checkpoint notice, not the red confirm-gate treatment.
 */
import { Clock, Square } from "lucide-react";
import { cn } from "@/lib/cn";
import { holdCopy } from "@/lib/deepResearchHold";
import { TAP_TARGET } from "@/lib/tapTarget";
import type { ActiveHold } from "@/lib/deepResearchTrace";

interface Props {
  hold: ActiveHold;
  /** Client clock, ticking, so the countdown moves while nothing arrives. */
  nowMs: number;
  /** Keep waiting — a LOCAL acknowledgement. Sends nothing to the backend. */
  onContinue: () => void;
  /** Stop the run (the existing cancel → resumable PAUSED checkpoint). */
  onStop: () => void;
}

const HOLD_BTN =
  "flex w-full items-center justify-center gap-hair rounded-control px-body py-hair font-ui text-[0.82rem] transition-colors lg:w-auto";

export function DeepResearchHoldPanel({ hold, nowMs, onContinue, onStop }: Props) {
  const copy = holdCopy(hold, nowMs);
  return (
    <section
      aria-label={copy.title}
      data-dr-hold="panel"
      className="rounded-card border border-hairline-strong bg-surface-1 px-body py-body"
    >
      <div className="flex items-start gap-inline">
        <Clock className="mt-px size-4 shrink-0 text-text-muted" aria-hidden />
        <div className="flex-1">
          <h2 className="font-ui text-[0.9rem] font-medium text-text">{copy.title}</h2>

          <p className="mt-hair font-ui text-[0.82rem] leading-snug text-text-muted">
            {copy.why} {copy.notAnError}
          </p>

          <p className="mt-inline font-mono text-[0.78rem] text-text-muted">
            {copy.stateNow}
          </p>

          {copy.queuedWork.length > 0 && (
            <ul
              data-dr-hold-queued="list"
              className="mt-hair list-none space-y-hair font-mono text-[0.78rem] leading-snug text-text-faint"
            >
              {copy.queuedWork.map((query) => (
                <li key={query} className="flex gap-hair">
                  <span aria-hidden>·</span>
                  <span>{query}</span>
                </li>
              ))}
            </ul>
          )}

          <p className="mt-hair font-ui text-[0.82rem] leading-snug text-text">{copy.next}</p>

          <div className="mt-body flex flex-col-reverse gap-inline lg:flex-row lg:items-center">
            <button
              type="button"
              onClick={onStop}
              data-disco-control="dr.hold.stop"
              className={cn(
                TAP_TARGET,
                HOLD_BTN,
                "border border-hairline text-text-muted hover:text-text",
              )}
            >
              <Square className="size-3" aria-hidden />
              Stop and keep what it has
            </button>
            <button
              type="button"
              onClick={onContinue}
              data-disco-control="dr.hold.continue"
              className={cn(
                TAP_TARGET,
                HOLD_BTN,
                "bg-accent font-medium text-bg hover:opacity-90",
              )}
            >
              Continue waiting
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

/**
 * The collapsed form, shown in the heartbeat once the user has chosen to keep
 * waiting. Same live countdown, one line, and Stop stays reachable — the choice
 * is never taken away just because the panel was dismissed.
 */
export function DeepResearchHoldLine({
  hold,
  nowMs,
  onStop,
}: {
  hold: ActiveHold;
  nowMs: number;
  onStop: () => void;
}) {
  const copy = holdCopy(hold, nowMs);
  return (
    <div
      data-dr-hold="line"
      className="flex flex-wrap items-center gap-hair font-ui text-[0.8rem] text-text"
    >
      <Clock className="size-3 shrink-0 text-text-muted" aria-hidden />
      <span>{copy.collapsed}</span>
      <span className="text-text-faint">·</span>
      <button
        type="button"
        onClick={onStop}
        data-disco-control="dr.hold.stop"
        className={cn(TAP_TARGET, "font-ui text-[0.8rem] text-text-muted underline-offset-2 hover:text-text hover:underline")}
      >
        Stop
      </button>
    </div>
  );
}
