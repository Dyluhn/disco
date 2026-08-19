/**
 * Structured pre-plan intake (§K). Renders the `questions_v2` event as a form:
 * each question has bounded options plus an optional free-text answer.
 */

import { useState } from "react";
import { CornerDownLeft, ListChecks } from "lucide-react";
import { Markdown } from "@/components/Markdown";
import { cn } from "@/lib/cn";
import { TAP_TARGET } from "@/lib/tapTarget";

export interface QuestionsV2Item {
  id: string;
  question: string;
  options?: string[];
  allow_free_text?: boolean;
}

const REQUIRED_OPTIONS = ["Explore a few options", "Decide for me", "Other"];

function normalizedOptions(options: string[] | undefined): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of [...(options ?? []), ...REQUIRED_OPTIONS]) {
    const label = raw.trim();
    const key = label.toLowerCase();
    if (!label || seen.has(key)) continue;
    seen.add(key);
    out.push(label);
  }
  return out;
}

export function QuestionsV2Panel({
  question,
  items,
  onAnswer,
}: {
  question: string;
  items: QuestionsV2Item[];
  onAnswer: (text: string) => void;
}) {
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [notes, setNotes] = useState<Record<string, string>>({});

  const allAnswered = items.every((item) => {
    const choice = choices[item.id]?.trim();
    const note = notes[item.id]?.trim();
    return Boolean(choice || note);
  });

  const submit = () => {
    if (!allAnswered) return;
    const lines = items.map((item) => {
      const choice = choices[item.id]?.trim();
      const note = notes[item.id]?.trim();
      const parts = [
        choice ? `Option: ${choice}` : "",
        note ? `Details: ${note}` : "",
      ].filter(Boolean);
      return `**${item.question}**\n${parts.join("\n")}`;
    });
    onAnswer(lines.join("\n\n"));
  };

  return (
    <div
      role="alertdialog"
      data-gate="questions_v2"
      aria-label="The agent needs a few details before planning"
      className="flex flex-col gap-inline rounded-control border border-accent/40 bg-accent/5 p-body"
    >
      <header className="flex items-center gap-hair">
        <ListChecks className="size-4 shrink-0 text-accent" aria-hidden />
        <h3 className="font-display text-[1.05rem] font-medium text-text">
          A few details before planning
        </h3>
      </header>
      <div className="font-ui text-[0.9rem] text-text">
        <Markdown>{question}</Markdown>
      </div>
      <div className="flex flex-col gap-inline">
        {items.map((item) => {
          const options = normalizedOptions(item.options);
          const canFreeText = item.allow_free_text !== false;
          return (
            <fieldset key={item.id} className="flex flex-col gap-hair">
              <legend className="font-ui text-[0.82rem] font-medium text-text">
                {item.question}
              </legend>
              <div className="flex flex-wrap gap-hair">
                {options.map((option) => (
                  <label
                    key={option}
                    className={cn(
                      TAP_TARGET,
                      "flex cursor-pointer items-center gap-hair rounded-control border px-inline py-hair font-ui text-[0.82rem] transition-colors",
                      choices[item.id] === option
                        ? "border-accent bg-accent/10 text-text"
                        : "border-hairline text-text-muted hover:border-accent/40",
                    )}
                  >
                    <input
                      type="radio"
                      name={`questions-v2-${item.id}`}
                      value={option}
                      checked={choices[item.id] === option}
                      onChange={(event) =>
                        setChoices((prev) => ({ ...prev, [item.id]: event.target.value }))
                      }
                      className="sr-only"
                    />
                    {option}
                  </label>
                ))}
              </div>
              {canFreeText && (
                <textarea
                  id={`questions-v2-note-${item.id}`}
                  value={notes[item.id] || ""}
                  rows={2}
                  onChange={(event) =>
                    setNotes((prev) => ({ ...prev, [item.id]: event.target.value }))
                  }
                  placeholder="Add details or type your own answer"
                  className={cn(
                    "rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.85rem] text-text outline-none",
                    "transition-colors focus:border-accent placeholder:text-text-faint",
                  )}
                />
              )}
            </fieldset>
          );
        })}
      </div>
      <div className="flex justify-end">
        <button
          type="button"
          onClick={submit}
          disabled={!allAnswered}
          data-disco-control="submit-questions-v2"
          className={cn(
            TAP_TARGET,
            "flex w-full items-center justify-center gap-hair rounded-control border border-accent/40 px-inline py-hair font-ui text-[0.82rem] text-accent transition-colors lg:w-auto",
            "hover:bg-accent/10 disabled:cursor-not-allowed disabled:opacity-40",
          )}
        >
          Submit answers
          <CornerDownLeft className="size-3.5" aria-hidden />
        </button>
      </div>
    </div>
  );
}
