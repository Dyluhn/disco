/**
 * The two-way Ask-gate (Build). When the agent calls `ask_user` with a free-form
 * question (no options), the loop halts at AWAITING_USER_QUESTION and this panel
 * renders the question prominently with a focused answer box — distinct from the
 * pick-a-card AlternativesGate. The typed answer is sent as a user message that
 * resumes the loop (Command–Query: the user's reply IS the resume signal).
 *
 * Design discipline:
 * - accent (not warn) chrome: a question is a normal collaboration beat, not an
 *   error or a recovery state
 * - the question renders as Markdown (the agent may format options inline)
 * - multiline textarea: ⌘/Ctrl+Enter (or Enter) sends; answers are often a sentence
 */

import { useState } from "react";
import { CornerDownLeft, MessageCircleQuestion } from "lucide-react";
import { Markdown } from "@/components/Markdown";
import { cn } from "@/lib/cn";

export function AskPanel({
  question,
  onAnswer,
}: {
  /** The agent's free-form question text (Markdown). */
  question: string;
  /** Send the typed answer — resumes the loop. */
  onAnswer: (text: string) => void;
}) {
  const [text, setText] = useState("");
  const submit = () => {
    if (!text.trim()) return;
    onAnswer(text);
    setText("");
  };
  return (
    <div
      role="alertdialog"
      data-gate="ask"
      aria-label="The agent has a question for you"
      className="flex flex-col gap-inline rounded-control border border-accent/40 bg-accent/5 p-body"
    >
      <header className="flex items-center gap-hair">
        <MessageCircleQuestion className="size-4 shrink-0 text-accent" aria-hidden />
        <h3 className="font-display text-[1.05rem] font-medium text-text">
          The agent has a question for you
        </h3>
      </header>
      <div className="font-ui text-[0.9rem] text-text">
        <Markdown>{question}</Markdown>
      </div>
      <div
        className={cn(
          "flex items-end gap-hair rounded-control border border-hairline bg-surface-1 px-inline py-hair",
          "transition-colors focus-within:border-accent",
        )}
      >
        <textarea
          value={text}
          rows={2}
          autoFocus
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            // Enter sends; Shift+Enter inserts a newline (for a longer answer).
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          placeholder="Type your answer — the agent picks up where it left off"
          aria-label="Answer the agent's question"
          className="min-w-0 flex-1 resize-none bg-transparent font-ui text-[0.85rem] text-text outline-none placeholder:text-text-faint"
        />
        <button
          type="button"
          onClick={submit}
          disabled={!text.trim()}
          aria-label="Send answer"
          data-disco-control="answer-question"
          className="shrink-0 pb-hair text-accent transition-colors hover:opacity-80 disabled:opacity-40"
        >
          <CornerDownLeft className="size-4" aria-hidden />
        </button>
      </div>
    </div>
  );
}
