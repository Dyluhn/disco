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
import { Send } from "lucide-react";
import { useState } from "react";

interface Props {
  onSteer: (text: string) => void;
  disabled?: boolean;
}

export function DeepSteerInput({ onSteer, disabled }: Props) {
  const [text, setText] = useState("");
  const [sent, setSent] = useState<string | null>(null);

  const submit = () => {
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    onSteer(trimmed);
    setText("");
    // The agent picks a steer up at its next turn boundary, not instantly;
    // echo it so the user sees the instruction was accepted rather than
    // wondering whether the box did anything.
    setSent(trimmed);
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
          className="min-w-0 flex-1 rounded-control border border-border bg-surface px-3 py-1.5 font-ui text-[0.86rem] text-text placeholder:text-text-muted focus:border-accent focus:outline-none disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={disabled || !text.trim()}
          aria-label="Send steer"
          data-disco-control="dr-steer-send"
          className="inline-flex items-center gap-1 rounded-control border border-border px-3 py-1.5 font-ui text-[0.82rem] text-text-muted transition-colors hover:border-accent hover:text-text disabled:cursor-not-allowed disabled:opacity-40"
        >
          <Send className="size-3.5" aria-hidden />
          Steer
        </button>
      </form>
      {sent && (
        <p className="font-ui text-[0.78rem] text-text-muted" role="status">
          Steering the next research turn: “{sent}”
        </p>
      )}
    </div>
  );
}
