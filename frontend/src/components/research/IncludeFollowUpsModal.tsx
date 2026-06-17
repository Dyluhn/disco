/**
 * IncludeFollowUpsModal — pre-export / pre-audio follow-up selector (WALK-20 / B4).
 *
 * Shown BEFORE the normal Export modal or Audio mode dialog when the
 * conversation has post-report follow-up Q&A turns. Lets the user choose which
 * follow-up pairs to include in the output.
 *
 * UX flow:
 *   1. User clicks "Export as…" or "Audio Overview"
 *   2. If followUps.length > 0 → this modal opens
 *   3. User picks follow-up pairs (all selected by default), confirms
 *   4. onConfirm(selectedSeqs) → caller opens the next dialog (ExportModal /
 *      AudioModeDialog) with the selection threaded through
 *
 * The modal only shows Q&A pairs whose question has a non-empty answer already
 * in the event log. Pairs without an answer are shown with a "(no answer yet)"
 * placeholder and cannot be toggled ON (they'd produce empty output).
 *
 * Reuses the Radix Dialog + design tokens already used by ExportModal and
 * AudioModeDialog in NeedMoreCard.
 */

import * as Dialog from "@radix-ui/react-dialog";
import { useCallback, useEffect, useState } from "react";
import { Check, X } from "lucide-react";
import { cn } from "@/lib/cn";
import type { MessageEvent } from "@/types/agent";

// ── Types ─────────────────────────────────────────────────────────────────────

interface FollowUpPair {
  questionSeq: number;
  questionText: string;
  answerText: string;
  hasAnswer: boolean;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

/** Group a flat list of user/assistant MessageEvents into Q&A pairs.
 *  followUps must be in event-log order (useDeepResearch.followUps). */
function groupIntoPairs(followUps: MessageEvent[]): FollowUpPair[] {
  const pairs: FollowUpPair[] = [];
  let i = 0;
  while (i < followUps.length) {
    const msg = followUps[i];
    if (msg.message.role === "user") {
      const next = followUps[i + 1];
      const hasAnswer = Boolean(next && next.message.role === "assistant");
      pairs.push({
        questionSeq: msg.seq ?? 0,
        questionText: msg.message.content ?? "",
        answerText: hasAnswer ? (next!.message.content ?? "") : "",
        hasAnswer,
      });
      i += hasAnswer ? 2 : 1;
    } else {
      i++;
    }
  }
  return pairs;
}

// ── Component ─────────────────────────────────────────────────────────────────

export interface IncludeFollowUpsModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** All post-report follow-up MessageEvents (user + assistant, in order). */
  followUps: MessageEvent[];
  /** Called when the user confirms their selection.
   *  ``selectedSeqs`` are the seq values of the selected USER messages.
   *  An empty array means "none selected → export without follow-ups". */
  onConfirm: (selectedSeqs: number[]) => void;
  /** Label for the action (e.g. "Export" or "Generate audio"). */
  actionLabel?: string;
}

export function IncludeFollowUpsModal({
  open,
  onOpenChange,
  followUps,
  onConfirm,
  actionLabel = "Export",
}: IncludeFollowUpsModalProps) {
  const pairs = groupIntoPairs(followUps);

  // Default: all pairs with answers are selected.
  const defaultSelected = new Set(
    pairs.filter((p) => p.hasAnswer).map((p) => p.questionSeq),
  );
  const [selected, setSelected] = useState<Set<number>>(defaultSelected);

  // Reset selection whenever the modal opens (or the follow-ups change).
  useEffect(() => {
    if (open) {
      setSelected(
        new Set(
          pairs.filter((p) => p.hasAnswer).map((p) => p.questionSeq),
        ),
      );
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const togglePair = useCallback(
    (seq: number, hasAnswer: boolean) => {
      if (!hasAnswer) return; // can't include a pair without an answer
      setSelected((prev) => {
        const next = new Set(prev);
        if (next.has(seq)) {
          next.delete(seq);
        } else {
          next.add(seq);
        }
        return next;
      });
    },
    [],
  );

  const selectAll = useCallback(() => {
    setSelected(new Set(pairs.filter((p) => p.hasAnswer).map((p) => p.questionSeq)));
  }, [pairs]);

  const deselectAll = useCallback(() => setSelected(new Set()), []);

  const handleConfirm = useCallback(() => {
    onOpenChange(false);
    onConfirm(Array.from(selected));
  }, [selected, onConfirm, onOpenChange]);

  const handleSkip = useCallback(() => {
    onOpenChange(false);
    onConfirm([]); // no follow-ups → proceed without them
  }, [onConfirm, onOpenChange]);

  const allSelected = pairs.filter((p) => p.hasAnswer).every((p) => selected.has(p.questionSeq));

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/30 backdrop-blur-sm" />
        <Dialog.Content
          className={cn(
            "fixed left-1/2 top-1/2 z-50 w-[min(32rem,92vw)]",
            "-translate-x-1/2 -translate-y-1/2",
            "bg-[color-mix(in_oklch,var(--surface-1)_80%,transparent)]",
            "backdrop-blur-md border border-hairline rounded-card p-body pmx-rise",
          )}
          aria-labelledby="include-fu-title"
        >
          <div className="flex items-center justify-between">
            <Dialog.Title
              id="include-fu-title"
              className="font-ui text-[0.95rem] font-semibold text-text"
            >
              Include follow-ups?
            </Dialog.Title>
            <Dialog.Close asChild>
              <button
                type="button"
                aria-label="Close dialog"
                className="rounded-control p-hair text-text-faint transition-colors hover:text-text"
              >
                <X className="size-4" aria-hidden />
              </button>
            </Dialog.Close>
          </div>

          <Dialog.Description className="mt-hair font-ui text-[0.82rem] text-text-muted">
            Select follow-up Q&amp;A pairs to include in the {actionLabel.toLowerCase()}.
          </Dialog.Description>

          {/* Select / Deselect all */}
          <div className="mt-section flex items-center gap-inline">
            <button
              type="button"
              onClick={allSelected ? deselectAll : selectAll}
              className="font-ui text-[0.76rem] text-accent underline-offset-2 hover:underline"
            >
              {allSelected ? "Deselect all" : "Select all"}
            </button>
          </div>

          {/* Follow-up pair list */}
          <ul className="mt-inline flex flex-col gap-hair" role="list">
            {pairs.map((pair) => {
              const isSelected = selected.has(pair.questionSeq);
              return (
                <li key={pair.questionSeq}>
                  <button
                    type="button"
                    disabled={!pair.hasAnswer}
                    onClick={() => togglePair(pair.questionSeq, pair.hasAnswer)}
                    className={cn(
                      "flex w-full items-start gap-inline rounded-control border p-inline text-left",
                      "font-ui text-[0.82rem] transition-colors",
                      pair.hasAnswer
                        ? isSelected
                          ? "border-accent/60 bg-accent/5 text-text"
                          : "border-hairline text-text-muted hover:border-accent/40 hover:text-text"
                        : "border-dashed border-hairline text-text-faint opacity-50 cursor-not-allowed",
                    )}
                    aria-pressed={pair.hasAnswer ? isSelected : undefined}
                  >
                    {/* Checkbox visual */}
                    <span
                      className={cn(
                        "mt-px flex size-4 shrink-0 items-center justify-center rounded border",
                        pair.hasAnswer
                          ? isSelected
                            ? "border-accent bg-accent text-white"
                            : "border-hairline bg-surface-1"
                          : "border-hairline bg-surface-1 opacity-40",
                      )}
                      aria-hidden
                    >
                      {isSelected && pair.hasAnswer && (
                        <Check className="size-2.5" strokeWidth={3} />
                      )}
                    </span>

                    <div className="flex-1 min-w-0">
                      <div className="truncate font-medium text-[0.82rem]">
                        {pair.questionText.slice(0, 120)}
                        {pair.questionText.length > 120 ? "…" : ""}
                      </div>
                      <div className="text-[0.72rem] text-text-faint mt-px">
                        {pair.hasAnswer
                          ? `Answer: ${pair.answerText.slice(0, 80)}${pair.answerText.length > 80 ? "…" : ""}`
                          : "(answer still generating — cannot include)"}
                      </div>
                    </div>
                  </button>
                </li>
              );
            })}
          </ul>

          {/* Actions */}
          <div className="mt-section flex items-center justify-end gap-inline">
            <button
              type="button"
              onClick={handleSkip}
              className="rounded-control border border-hairline px-inline py-hair font-ui text-[0.82rem] text-text-muted transition-colors hover:text-text"
            >
              Skip
            </button>
            <button
              type="button"
              onClick={handleConfirm}
              className="rounded-control border border-accent/60 bg-accent/10 px-inline py-hair font-ui text-[0.82rem] font-medium text-accent transition-colors hover:bg-accent/20"
            >
              {selected.size > 0
                ? `${actionLabel} with ${selected.size} follow-up${selected.size === 1 ? "" : "s"}`
                : `${actionLabel} without follow-ups`}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
