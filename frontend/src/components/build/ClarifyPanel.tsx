/**
 * The Clarify gate (Build) — renders when the planner calls `clarify` with
 * typed multi-question items. Modeled on AskPanel but supports MULTIPLE
 * structured question types: short_text (single-line input), long_text
 * (textarea), and choice (radio buttons from `options`).
 *
 * The user fills in all questions and clicks Submit; the answers are
 * re-injected as a user message that resumes planning with all the
 * clarified details in context.
 */

import { useState } from "react";
import { CornerDownLeft, MessageCircleQuestion } from "lucide-react";
import { Markdown } from "@/components/Markdown";
import { cn } from "@/lib/cn";

export interface ClarifyQuestionItem {
  id: string;
  question: string;
  type: "short_text" | "long_text" | "choice";
  options?: string[];
}

export function ClarifyPanel({
  question,
  items,
  onAnswer,
}: {
  /** The overarching question summary. */
  question: string;
  /** The typed question items. */
  items: ClarifyQuestionItem[];
  /** Send the formatted answers — resumes the loop. */
  onAnswer: (text: string) => void;
}) {
  const [answers, setAnswers] = useState<Record<string, string>>({});

  const update = (id: string, value: string) => {
    setAnswers((prev) => ({ ...prev, [id]: value }));
  };

  const allAnswered = items.every((it) => {
    const a = answers[it.id]?.trim();
    return a && a.length > 0;
  });

  const submit = () => {
    if (!allAnswered) return;
    const lines = items.map((it) => `**${it.question}**\n${answers[it.id]?.trim() || ""}`);
    onAnswer(lines.join("\n\n"));
  };

  return (
    <div
      role="alertdialog"
      data-gate="clarify"
      aria-label="The agent needs clarification before planning"
      className="flex flex-col gap-inline rounded-control border border-accent/40 bg-accent/5 p-body"
    >
      <header className="flex items-center gap-hair">
        <MessageCircleQuestion className="size-4 shrink-0 text-accent" aria-hidden />
        <h3 className="font-display text-[1.05rem] font-medium text-text">
          The agent needs clarification
        </h3>
      </header>
      <div className="font-ui text-[0.9rem] text-text">
        <Markdown>{question}</Markdown>
      </div>
      <div className="flex flex-col gap-inline">
        {items.map((it) => (
          <div key={it.id} className="flex flex-col gap-hair">
            <label
              htmlFor={`clarify-${it.id}`}
              className="font-ui text-[0.82rem] font-medium text-text"
            >
              {it.question}
            </label>
            {it.type === "choice" && it.options && it.options.length > 0 ? (
              <div className="flex flex-wrap gap-inline">
                {it.options.map((opt) => (
                  <label
                    key={opt}
                    className={cn(
                      "flex items-center gap-hair rounded-control border px-inline py-hair font-ui text-[0.82rem] cursor-pointer transition-colors",
                      answers[it.id] === opt
                        ? "border-accent bg-accent/10 text-text"
                        : "border-hairline text-text-muted hover:border-accent/40",
                    )}
                  >
                    <input
                      type="radio"
                      name={`clarify-${it.id}`}
                      value={opt}
                      checked={answers[it.id] === opt}
                      onChange={(e) => update(it.id, e.target.value)}
                      className="sr-only"
                    />
                    {opt}
                  </label>
                ))}
              </div>
            ) : it.type === "long_text" ? (
              <textarea
                id={`clarify-${it.id}`}
                value={answers[it.id] || ""}
                rows={3}
                onChange={(e) => update(it.id, e.target.value)}
                placeholder="Type your answer…"
                className={cn(
                  "rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.85rem] text-text outline-none",
                  "transition-colors focus:border-accent placeholder:text-text-faint",
                )}
              />
            ) : (
              <input
                id={`clarify-${it.id}`}
                type="text"
                value={answers[it.id] || ""}
                onChange={(e) => update(it.id, e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && allAnswered) {
                    e.preventDefault();
                    submit();
                  }
                }}
                placeholder="Type your answer…"
                className={cn(
                  "rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.85rem] text-text outline-none",
                  "transition-colors focus:border-accent placeholder:text-text-faint",
                )}
              />
            )}
          </div>
        ))}
      </div>
      <div className="flex justify-end">
        <button
          type="button"
          onClick={submit}
          disabled={!allAnswered}
          data-disco-control="submit-clarify"
          className={cn(
            "flex items-center gap-hair rounded-control border border-accent/40 px-inline py-hair font-ui text-[0.82rem] text-accent transition-colors",
            "hover:bg-accent/10 disabled:opacity-40 disabled:cursor-not-allowed",
          )}
        >
          Submit answers
          <CornerDownLeft className="size-3.5" aria-hidden />
        </button>
      </div>
    </div>
  );
}
