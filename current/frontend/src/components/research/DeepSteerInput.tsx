/**
 * DeepSteerInput — the user's mid-run steering control.
 *
 * The steer transport has been live end-to-end since D3, but until now no
 * surface rendered a control for it, so the capability existed only for API
 * callers. This is that control: while a run is RUNNING, typed guidance is
 * sent as a `steer` frame, reaches the research agent's queue, and lands in
 * the NEXT turn's prompt as a priority USER STEER line the model must act on
 * before returning to its own plan.
 *
 * Only rendered while the run is live — steering a finished report is a
 * follow-up question, which the report view owns.
 */
import { Check, Loader2, Send } from "lucide-react";
import { useState } from "react";

interface Props {
  onSteer: (text: string, steerId?: string) => void;
  disabled?: boolean;
  /** Durable host facts, not an optimistic send result. */
  appliedSteerIds?: readonly string[];
}

function newSteerId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `steer-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function DeepSteerInput({ onSteer, disabled, appliedSteerIds = [] }: Props) {
  const [text, setText] = useState("");
  const [submitted, setSubmitted] = useState<Array<{ id: string; text: string }>>([]);

  const submit = () => {
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    const id = newSteerId();
    onSteer(trimmed, id);
    setText("");
    setSubmitted((current) => [...current, { id, text: trimmed }]);
  };

  return (
    <div className="flex flex-col gap-inline">
      <form
        className="flex items-center gap-inline"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <input
          type="text"
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={disabled}
          placeholder="Steer the research — e.g. focus on peer-reviewed sources"
          aria-label="Steer the research"
          data-disco-control="dr-steer-input"
          className="min-h-11 min-w-0 flex-1 rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.86rem] text-text outline-none transition-colors placeholder:text-text-faint focus-within:border-hairline-strong focus:border-hairline-strong disabled:opacity-50 lg:min-h-0"
        />
        <button
          type="submit"
          disabled={disabled || !text.trim()}
          aria-label="Send steer"
          data-disco-control="dr-steer-send"
          className="inline-flex min-h-11 items-center gap-1 rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.82rem] text-text-muted transition-colors hover:border-hairline-strong hover:text-text disabled:cursor-not-allowed disabled:opacity-40 lg:min-h-0"
        >
          <Send className="size-3.5" aria-hidden />
          Steer
        </button>
      </form>
      {submitted.length > 0 && (
        <div className="flex flex-col gap-hair" role="status" aria-live="polite">
          {submitted.map(({ id, text: steerText }) => {
            const applied = appliedSteerIds.includes(id);
            return (
              <p key={id} className="flex items-center gap-hair font-ui text-[0.78rem] text-text-muted">
                {applied ? (
                  <Check className="size-3.5 text-supported" aria-label="applied" />
                ) : (
                  <Loader2 className="size-3.5 animate-spin text-accent" aria-label="pending" />
                )}
                {applied ? "Applied to a research turn" : "Pending delivery"}: “{steerText}”
              </p>
            );
          })}
        </div>
      )}
    </div>
  );
}
