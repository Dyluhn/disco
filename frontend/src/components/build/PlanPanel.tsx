/**
 * The plan-approval gate + live capstone tracker (the plan-first Build flow). Before any
 * work runs, the agent's proposed plan is shown here as a checklist; the human Approves to
 * start building or Revises to send focused changes back for a new plan. During and after
 * the build the SAME panel renders read-only, checking steps off as the agent reports them
 * (honest, agent-driven progress — an unreported step stays pending, never inferred).
 */

import { useState } from "react";
import { Check, CircleDashed, ClipboardList, Loader2 } from "lucide-react";
import { cn } from "@/lib/cn";
import { Markdown } from "@/components/Markdown";
import type { StepState, PlanView } from "@/lib/buildTrace";

function StepIcon({ state }: { state: StepState }) {
  if (state === "done") return <Check className="size-3.5 shrink-0 text-supported" aria-hidden />;
  if (state === "active")
    return <Loader2 className="size-3.5 shrink-0 animate-spin text-accent" aria-hidden />;
  return <CircleDashed className="size-3.5 shrink-0 text-text-faint" aria-hidden />;
}

export function PlanPanel({
  plan,
  progress,
  onApprove,
  onRevise,
}: {
  plan: PlanView;
  progress: Map<number, StepState>;
  /** Gate mode: present only while awaiting approval. Omit for the read-only tracker. */
  onApprove?: () => void;
  onRevise?: (text: string) => void;
}) {
  const [revising, setRevising] = useState(false);
  const [text, setText] = useState("");
  const gate = Boolean(onApprove);

  const submitRevision = () => {
    const t = text.trim();
    if (!t || !onRevise) return;
    onRevise(t);
    setText("");
    setRevising(false);
  };

  return (
    <section
      role={gate ? "alertdialog" : undefined}
      aria-label={gate ? "Plan needs your approval" : "Plan progress"}
      className={cn(
        "rounded-card bg-surface-1 px-body py-body",
        gate ? "border-2 border-accent" : "border border-hairline",
      )}
    >
      <header className="flex items-center gap-inline">
        <ClipboardList className="size-4 shrink-0 text-accent" aria-hidden />
        <h2 className="font-ui text-[0.9rem] font-medium text-text">
          {gate ? "Review the plan" : "Plan"}
        </h2>
        {plan.revision > 1 && (
          <span className="rounded-full border border-hairline px-inline py-px font-ui text-[0.66rem] uppercase tracking-wide text-text-faint">
            revision {plan.revision}
          </span>
        )}
        {gate && (
          <span className="ml-auto rounded-full border border-accent px-inline py-px font-ui text-[0.7rem] uppercase tracking-wide text-accent">
            needs your approval
          </span>
        )}
      </header>

      <p className="mt-inline font-ui text-[0.84rem] leading-snug text-text-muted">{plan.summary}</p>

      {plan.context && (
        <details
          className="mt-inline rounded-control border border-hairline bg-surface-2 px-inline py-hair"
          // Open by default at the gate (the rationale is the user's load-bearing input
          // to the approve/revise decision); collapsed during execution.
          open={gate}
        >
          <summary className="cursor-pointer font-ui text-[0.74rem] uppercase tracking-wide text-text-faint">
            Context &amp; rationale
          </summary>
          <div className="mt-hair text-[0.84rem] leading-relaxed text-text-muted">
            <Markdown>{plan.context}</Markdown>
          </div>
        </details>
      )}

      <ol className="mt-body flex flex-col gap-hair">
        {plan.steps.map((step, i) => {
          const state = progress.get(i + 1) ?? "pending";
          return (
            <li key={i} className="flex items-start gap-inline">
              <span className="mt-px flex items-center gap-hair">
                <span className="w-4 text-right font-mono text-[0.72rem] text-text-faint">
                  {i + 1}
                </span>
                <StepIcon state={state} />
              </span>
              <span
                className={cn(
                  "font-ui text-[0.84rem] leading-snug",
                  state === "done" ? "text-text-muted" : "text-text",
                )}
              >
                {step.title}
                {step.detail && (
                  <span className="block text-[0.76rem] text-text-faint">{step.detail}</span>
                )}
              </span>
            </li>
          );
        })}
      </ol>

      {gate && (
        <>
          {revising ? (
            <div className="mt-body flex flex-col gap-hair">
              <textarea
                value={text}
                onChange={(e) => setText(e.target.value)}
                rows={2}
                autoFocus
                placeholder="Describe the changes you want in the plan…"
                className="w-full resize-none rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.82rem] text-text outline-none focus:border-accent"
              />
              <div className="flex items-center justify-end gap-inline">
                <button
                  type="button"
                  onClick={() => setRevising(false)}
                  className="rounded-control border border-hairline px-body py-hair font-ui text-[0.82rem] text-text-muted transition-colors hover:text-text"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={submitRevision}
                  disabled={!text.trim()}
                  className="rounded-control bg-accent px-body py-hair font-ui text-[0.82rem] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-40"
                >
                  Send revision
                </button>
              </div>
            </div>
          ) : (
            <div className="mt-body flex items-center justify-end gap-inline">
              <button
                type="button"
                onClick={() => setRevising(true)}
                className="rounded-control border border-hairline px-body py-hair font-ui text-[0.82rem] text-text-muted transition-colors hover:text-text"
              >
                Revise…
              </button>
              <button
                type="button"
                onClick={onApprove}
                className="rounded-control bg-accent px-body py-hair font-ui text-[0.82rem] font-medium text-bg transition-opacity hover:opacity-90"
              >
                Approve &amp; build
              </button>
            </div>
          )}
        </>
      )}
    </section>
  );
}
