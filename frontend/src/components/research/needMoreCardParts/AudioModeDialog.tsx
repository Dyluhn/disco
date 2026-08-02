/**
 * NeedMoreCard — audio mode chooser dialog, extracted verbatim from
 * NeedMoreCard.tsx.
 */

import * as Dialog from "@radix-ui/react-dialog";
import { Mic, Radio, X } from "lucide-react";
import { cn } from "@/lib/cn";
import type { AudioMode } from "./audioProgress";

// ── Audio mode chooser dialog ─────────────────────────────────────────────────

export interface AudioModeDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onChoose: (mode: AudioMode) => void;
}

export function AudioModeDialog({ open, onOpenChange, onChoose }: AudioModeDialogProps) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/40 backdrop-blur-md" />
        <Dialog.Content
          className={cn(
            "fixed left-1/2 top-1/2 z-50 w-[min(26rem,92vw)]",
            "-translate-x-1/2 -translate-y-1/2",
            "bg-[color-mix(in_oklch,var(--surface-1)_95%,transparent)]",
            "backdrop-blur-xl border border-hairline rounded-card p-body pmx-rise",
          )}
        >
          <div className="flex items-center justify-between">
            <Dialog.Title className="font-ui text-[0.95rem] font-semibold text-text">
              Generate audio overview
            </Dialog.Title>
            <Dialog.Close asChild>
              <button
                type="button"
                aria-label="Close audio mode dialog"
                className="rounded-control p-hair text-text-faint transition-colors hover:text-text"
              >
                <X className="size-4" aria-hidden />
              </button>
            </Dialog.Close>
          </div>

          <Dialog.Description className="mt-hair font-ui text-[0.82rem] text-text-muted">
            Choose an audio style for this research report.
          </Dialog.Description>

          <div className="mt-section flex flex-col gap-inline">
            {/* Podcast style */}
            <button
              type="button"
              onClick={() => onChoose("podcast")}
              data-disco-control="dr.audio.podcast"
              className={cn(
                "flex items-start gap-inline rounded-card border border-hairline p-inline",
                "font-ui text-[0.85rem] text-text-muted transition-colors text-left",
                "hover:border-accent hover:text-accent",
              )}
            >
              <Radio className="mt-px size-4 shrink-0 text-accent" aria-hidden />
              <div className="flex-1">
                <div className="flex items-center gap-hair font-medium text-text">
                  Podcast style
                  <span className="rounded border border-accent/40 bg-accent/10 px-[0.3rem] py-px font-ui text-[0.65rem] font-semibold uppercase tracking-wide text-accent">
                    alpha
                  </span>
                </div>
                <div className="text-[0.74rem] text-text-faint">
                  Two hosts discuss the key findings in a conversational back-and-forth.
                </div>
              </div>
            </button>

            {/* Single speaker */}
            <button
              type="button"
              onClick={() => onChoose("single")}
              data-disco-control="dr.audio.single"
              className={cn(
                "flex items-start gap-inline rounded-card border border-hairline p-inline",
                "font-ui text-[0.85rem] text-text-muted transition-colors text-left",
                "hover:border-accent hover:text-accent",
              )}
            >
              <Mic className="mt-px size-4 shrink-0 text-accent" aria-hidden />
              <div className="flex-1">
                <div className="font-medium text-text">Single speaker</div>
                <div className="text-[0.74rem] text-text-faint">
                  An honest walkthrough — covers the findings, discusses gaps, and gives a
                  balanced breakdown. Uses the Host A voice from Settings.
                </div>
              </div>
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
