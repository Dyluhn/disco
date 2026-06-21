/**
 * ReportFollowUp — a follow-up question input on a finished Deep Research report.
 * The user asks a question about the report; the server spins up a continuation
 * run on the SAME conversation, reusing the report's persisted corpus/passages
 * as grounding (RP-13).
 */

import { useState } from "react";
import { ArrowRight, MessageCircleQuestion } from "lucide-react";
import { cn } from "@/lib/cn";

export function ReportFollowUp({
  onAsk,
  busy,
}: {
  /** Send the follow-up question — starts a continuation run. */
  onAsk: (question: string) => void;
  /** True while the follow-up is being submitted. */
  busy: boolean;
}) {
  const [text, setText] = useState("");

  const submit = () => {
    if (!text.trim() || busy) return;
    onAsk(text.trim());
    setText("");
  };

  return (
    <div
      role="form"
      aria-label="Ask a follow-up question about the report"
      className="flex flex-col gap-inline rounded-control border border-hairline bg-surface-1 p-body"
    >
      <header className="flex items-center gap-hair">
        <MessageCircleQuestion className="size-4 shrink-0 text-text-muted" aria-hidden />
        <h3 className="font-ui text-[0.85rem] font-medium text-text">
          Ask a follow-up question
        </h3>
      </header>
      <p className="font-ui text-[0.78rem] text-text-muted">
        The agent will answer using the sources gathered for this report.
      </p>
      <div
        className={cn(
          "flex items-end gap-hair rounded-control border border-hairline bg-surface-0 px-inline py-hair",
          "transition-colors focus-within:border-accent",
        )}
      >
        <textarea
          value={text}
          rows={2}
          autoFocus
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          placeholder="e.g. Expand on section 3's findings…"
          aria-label="Follow-up question"
          className="min-w-0 flex-1 resize-none bg-transparent font-ui text-[0.85rem] text-text outline-none placeholder:text-text-faint"
        />
        <button
          type="button"
          onClick={submit}
          disabled={!text.trim() || busy}
          aria-label="Ask follow-up"
          data-disco-control="follow-up"
          className={cn(
            "shrink-0 pb-hair text-accent transition-colors hover:opacity-80 disabled:opacity-40",
            busy && "animate-pulse",
          )}
        >
          <ArrowRight className="size-4" aria-hidden />
        </button>
      </div>
    </div>
  );
}
