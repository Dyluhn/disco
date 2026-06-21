/**
 * The Steering Wheel (BoD §13.4): redirect a RUNNING agent without losing context —
 * "stop researching X, focus on Y" — not just Stop. Sends a steer frame the loop folds
 * in as a user message and replans against.
 */

import { useState } from "react";
import { CornerDownLeft, Navigation } from "lucide-react";
import { cn } from "@/lib/cn";

export function SteerInput({ onSteer, disabled }: { onSteer: (text: string) => void; disabled?: boolean }) {
  const [text, setText] = useState("");
  const submit = () => {
    if (!text.trim() || disabled) return;
    onSteer(text);
    setText("");
  };
  return (
    <div
      className={cn(
        "flex items-center gap-hair rounded-control border border-hairline bg-surface-1 px-inline py-hair transition-colors focus-within:border-hairline-strong",
        disabled && "opacity-50",
      )}
    >
      <Navigation className="size-3.5 shrink-0 text-text-faint" aria-hidden />
      <input
        value={text}
        disabled={disabled}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            submit();
          }
        }}
        placeholder="Steer the agent — e.g. “skip the cleanup, keep the file”"
        aria-label="Steer the agent"
        className="min-w-0 flex-1 bg-transparent font-ui text-[0.82rem] text-text outline-none placeholder:text-text-faint"
      />
      <button
        type="button"
        onClick={submit}
        disabled={disabled || !text.trim()}
        aria-label="Send steer"
        data-disco-control="steer"
        className="shrink-0 text-text-faint transition-colors hover:text-text disabled:opacity-40"
      >
        <CornerDownLeft className="size-3.5" aria-hidden />
      </button>
    </div>
  );
}
