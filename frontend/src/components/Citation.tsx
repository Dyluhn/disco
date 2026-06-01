import * as Dialog from "@radix-ui/react-dialog";
import { type ReactNode, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { cn } from "@/lib/cn";
import { useCoarsePointer } from "@/lib/usePointer";
import { cleanDomain, monogram } from "@/lib/sources";
import type { Passage, Verdict } from "@/types/grounded";

const VERDICT_CHIP: Record<Verdict, string> = {
  // Chroma ONLY here, and it carries verification meaning (BoD §13.3).
  supported: "text-accent border-accent/40 hover:border-accent",
  weak: "text-weak border-weak/45 hover:border-weak",
  unsupported: "text-unsupported border-unsupported/50 hover:border-unsupported",
};

const VERDICT_NOTE: Record<Verdict, string> = {
  supported: "Supported by this source",
  weak: "Weakly supported — verify",
  unsupported: "Not supported by the cited source",
};

const VERDICT_DOT: Record<Verdict, string> = {
  supported: "bg-supported",
  weak: "bg-weak",
  unsupported: "bg-unsupported",
};

function SourceCardContent({ n, passage, verdict }: { n: number; passage: Passage; verdict: Verdict }) {
  return (
    <div className="flex flex-col gap-inline">
      <div className="flex items-center gap-inline">
        <span
          aria-hidden
          className="grid size-5 shrink-0 place-items-center rounded-control bg-surface-2 font-ui text-[0.7rem] text-text-muted"
        >
          {monogram(passage.source_url)}
        </span>
        <span className="truncate font-ui text-[0.78rem] text-text-faint">
          {cleanDomain(passage.source_url)}
        </span>
        <span className="ml-auto flex items-center gap-hair font-ui text-[0.7rem] text-text-faint">
          <span className={cn("size-1.5 rounded-full", VERDICT_DOT[verdict])} />
          {VERDICT_NOTE[verdict]}
        </span>
      </div>
      <a
        href={passage.source_url}
        target="_blank"
        rel="noreferrer"
        className="font-ui text-[0.92rem] font-medium leading-snug text-text hover:text-link"
      >
        {passage.source_title}
      </a>
      {/* the corroborating span — verify without leaving the page */}
      <p className="border-l-2 border-hairline-strong pl-inline font-reading text-[0.85rem] leading-relaxed text-text-muted">
        <span className="mr-hair font-ui text-text-faint">[{n}]</span>
        {passage.text}
      </p>
    </div>
  );
}

interface CitationProps {
  n: number;
  passage: Passage;
  verdict: Verdict;
}

/** Inline `[n]` citation anchored to its claim. Hover (fine pointer) → source
 * card; tap (coarse pointer) → bottom-sheet. */
export function Citation({ n, passage, verdict }: CitationProps) {
  const coarse = useCoarsePointer();
  const label = `Source ${n}: ${passage.source_title} (${VERDICT_NOTE[verdict]})`;

  const chip = (
    <sup>
      <button
        type="button"
        aria-label={label}
        className={cn(
          "mx-px inline-flex min-w-4 cursor-pointer justify-center rounded-[0.25rem] border bg-transparent px-1 align-super font-ui text-[0.62rem] font-medium leading-none transition-colors",
          VERDICT_CHIP[verdict],
        )}
      >
        {n}
      </button>
    </sup>
  );

  if (coarse) {
    return (
      <Dialog.Root>
        <Dialog.Trigger asChild>{chip}</Dialog.Trigger>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-40 bg-black/40" />
          <Dialog.Content
            aria-describedby={undefined}
            className="fixed inset-x-0 bottom-0 z-50 rounded-t-card border-t border-hairline bg-surface-1 p-section pmx-rise"
          >
            <Dialog.Title className="sr-only">{passage.source_title}</Dialog.Title>
            <div className="mx-auto mb-section h-1 w-10 rounded-full bg-hairline-strong" />
            <SourceCardContent n={n} passage={passage} verdict={verdict} />
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    );
  }

  return <GutterCitation n={n} passage={passage} verdict={verdict} chip={chip} label={label} />;
}

/**
 * Fine-pointer (mouse) citation: a "source peek" pinned to the RIGHT GUTTER,
 * vertically near the citation — never over the reading column (item C). Because
 * citations sit at varying horizontal positions in the prose, no trigger-anchored
 * side is safe; pinning to the gutter guarantees the corroborating snippet sits
 * BESIDE the prose it annotates, not on top of it. A short close-delay + a hover
 * bridge (the card keeps itself open on hover) lets you move onto the card to
 * click its link.
 */
function GutterCitation({
  n,
  passage,
  verdict,
  chip,
  label,
}: CitationProps & { chip: ReactNode; label: string }) {
  const [open, setOpen] = useState(false);
  const [top, setTop] = useState(0);
  const anchorRef = useRef<HTMLElement>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const cardId = useId();

  const openCard = () => {
    clearTimeout(closeTimer.current);
    const r = anchorRef.current?.getBoundingClientRect();
    if (r) {
      // clamp so the card stays fully on-screen vertically
      const max = window.innerHeight - 260;
      setTop(Math.min(Math.max(r.top - 8, 16), Math.max(16, max)));
    }
    setOpen(true);
  };
  const scheduleClose = () => {
    closeTimer.current = setTimeout(() => setOpen(false), 140);
  };

  return (
    <span
      ref={anchorRef}
      onMouseEnter={openCard}
      onMouseLeave={scheduleClose}
      onFocus={openCard}
      onBlur={scheduleClose}
    >
      {chip}
      {open &&
        createPortal(
          <div
            role="dialog"
            aria-label={label}
            id={cardId}
            onMouseEnter={openCard}
            onMouseLeave={scheduleClose}
            style={{ position: "fixed", top, right: "1.5rem" }}
            className="z-50 max-h-[min(20rem,70vh)] w-[min(19rem,calc(100vw-3rem))] overflow-y-auto rounded-card border border-hairline-strong bg-surface-1 p-body pmx-rise"
          >
            <SourceCardContent n={n} passage={passage} verdict={verdict} />
          </div>,
          document.body,
        )}
    </span>
  );
}
