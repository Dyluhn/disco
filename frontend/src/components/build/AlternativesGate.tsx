/**
 * The structured-recovery gate (Build). Renders 2–3 cards of agent-proposed
 * alternatives after 4 consecutive failures, and a steer/cancel escape. Picking
 * a card sends `pick_alternative` over the WS; the loop runs the option's tool
 * call as the next action and returns to RUNNING.
 *
 * Design discipline:
 * - quiet chrome: warn-color heading (this is a recovery state, not an error)
 * - cards are full-width on narrow viewports, three columns on wide; each shows
 *   title + description + the concrete tool it'll run
 * - one explicit "Skip — let me steer manually" affordance maps to nothing
 *   (closes the gate by letting the user use the SteerInput beneath it)
 */

import { AlertTriangle, ArrowRight, PlayCircle, Terminal } from "lucide-react";
import { cn } from "@/lib/cn";
import type { AlternativesEvent } from "@/types/agent";

const CONTINUE_ID = "__continue__";

export function AlternativesGate({
  alternatives,
  onPick,
  onSteer,
}: {
  alternatives: AlternativesEvent;
  onPick: (optionId: string) => void;
  /** Optional: render a "Skip" link that drops focus into the steer input. */
  onSteer?: () => void;
}) {
  // The "Continue anyway" bypass is rendered as a distinct action, NOT a
  // recommendation card (it's an escape, not a proposed fix).
  const recommendations = alternatives.options.filter((o) => o.id !== CONTINUE_ID);
  const hasContinue = alternatives.options.some((o) => o.id === CONTINUE_ID);
  return (
    <div
      role="alertdialog"
      aria-label="The agent needs a decision after repeated failures"
      className="flex flex-col gap-inline rounded-control border border-warn/40 bg-warn/5 p-body"
    >
      <header className="flex items-center gap-hair">
        <AlertTriangle className="size-4 shrink-0 text-warn" aria-hidden />
        <h3 className="font-display text-[1.05rem] font-medium text-text">
          Pick a recovery path
        </h3>
      </header>
      <p className="font-ui text-[0.84rem] text-text-muted">
        {alternatives.summary}
      </p>

      <ul
        className={cn(
          "mt-hair grid gap-inline",
          recommendations.length >= 3 ? "lg:grid-cols-3" : "lg:grid-cols-2",
        )}
      >
        {recommendations.map((opt, i) => (
          <li key={opt.id}>
            <button
              type="button"
              onClick={() => onPick(opt.id)}
              data-disco-control="pick-alternative"
              className="group flex h-full w-full flex-col gap-hair rounded-control border border-hairline bg-surface-1 p-inline text-left transition-colors hover:border-accent hover:bg-surface-2"
            >
              <span className="flex items-baseline gap-hair">
                <span className="font-mono text-[0.7rem] text-text-faint">
                  {i + 1}
                </span>
                <span className="font-display text-[0.98rem] font-medium text-text">
                  {opt.title}
                </span>
              </span>
              <span className="font-ui text-[0.8rem] text-text-muted">
                {opt.description}
              </span>
              <span
                title={`Will call: ${opt.tool_name}`}
                className="mt-auto flex items-center gap-hair pt-hair font-mono text-[0.72rem] text-text-faint"
              >
                <Terminal className="size-3" aria-hidden />
                {opt.tool_name}
              </span>
              <span className="mt-hair flex items-center gap-hair font-ui text-[0.76rem] text-accent opacity-0 transition-opacity group-hover:opacity-100">
                Run this path
                <ArrowRight className="size-3" aria-hidden />
              </span>
            </button>
          </li>
        ))}
      </ul>

      <div className="mt-hair flex flex-wrap items-center justify-between gap-inline border-t border-hairline pt-inline">
        {hasContinue ? (
          <button
            type="button"
            onClick={() => onPick(CONTINUE_ID)}
            data-disco-control="pick-alternative"
            className="flex items-center gap-hair rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.8rem] text-text-muted transition-colors hover:border-accent hover:text-text"
          >
            <PlayCircle className="size-3.5" aria-hidden />
            Continue anyway — let the agent keep going
          </button>
        ) : (
          <span />
        )}
        {onSteer && (
          <button
            type="button"
            onClick={onSteer}
            className="font-ui text-[0.78rem] text-text-faint underline-offset-2 hover:underline"
          >
            or steer manually
          </button>
        )}
      </div>
    </div>
  );
}
