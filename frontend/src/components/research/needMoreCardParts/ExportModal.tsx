/**
 * NeedMoreCard — export dialog (extracted from NeedMoreCard.tsx). Purely
 * presentational: all export state + logic lives in `useReportExport`.
 */

import * as Dialog from "@radix-ui/react-dialog";
import { useCallback, useState } from "react";
import { X } from "lucide-react";
import { cn } from "@/lib/cn";
import { useExportCapabilities } from "@/hooks/useExportCapabilities";
import { useTemplates } from "@/hooks/useTemplates";
import type { ReportEvent } from "@/types/agent";
import { TemplatePicker } from "../TemplatePicker";
import { ExportMdButton } from "./ExportMdButton";
import { ExportPdfButton } from "./ExportPdfButton";
import { useReportExport } from "./useReportExport";

// ── Export modal ──────────────────────────────────────────────────────────────

export interface ExportModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  report: ReportEvent;
  cid: string;
  /** Selected follow-up user-message seqs to include (WALK-20). */
  followUpSeqs?: number[];
  /** Exact activated Export opener to return focus to on close. Optional:
   *  existing callers without a ref keep default Radix behavior. */
  triggerRef?: { current: HTMLButtonElement | null };
}

/** Guard: only a still-connected, enabled, visible opener may take focus.
 *  Never falls back to another card's button (no selectors, no globals). */
function isReturnTargetFocusable(el: HTMLButtonElement | null | undefined): el is HTMLButtonElement {
  if (!el) return false;
  if (!el.isConnected) return false;
  if (el.disabled) return false;
  if (el.closest("[hidden], [inert]")) return false;
  const view = el.ownerDocument?.defaultView;
  const style = view?.getComputedStyle?.(el);
  if (style && (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse")) return false;
  return true;
}

export function ExportModal({ open, onOpenChange, report, cid, followUpSeqs, triggerRef }: ExportModalProps) {
  const exportCaps = useExportCapabilities();
  // Export template — a brand theme spin. Threaded into the POST body as `theme`
  // (registry name) + `mode` (split from the chosen catalogue entry). Default =
  // Disco light; MD ignores it (byte-identical regardless).
  const templates = useTemplates();
  const [templateId, setTemplateId] = useState("disco-light");
  const tpl = templates.find((t) => t.id === templateId) ?? templates[0];

  const { exporting, exportError, fsa, handleMd, handleFmt } = useReportExport({
    cid,
    reportQuery: report.query,
    followUpSeqs,
    themeName: tpl.name,
    themeMode: tpl.mode,
  });

  // Controlled Dialog has no registered Trigger, so default Close/Escape
  // focus restoration lands on BODY. Return focus explicitly to the exact
  // activated opener; guarded targets leave default behavior (BODY), never
  // another card. Open focus + containment untouched (no autoFocus props).
  const handleCloseAutoFocus = useCallback(
    (event: Event) => {
      const opener = triggerRef?.current;
      if (!isReturnTargetFocusable(opener)) return;
      event.preventDefault();
      opener.focus({ preventScroll: true });
    },
    [triggerRef],
  );

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/40 backdrop-blur-md" />
        <Dialog.Content
          onCloseAutoFocus={handleCloseAutoFocus}
          className={cn(
            "fixed left-1/2 top-1/2 z-50 w-[min(26rem,92vw)]",
            "-translate-x-1/2 -translate-y-1/2",
            "bg-[color-mix(in_oklch,var(--surface-1)_95%,transparent)]",
            "backdrop-blur-xl border border-hairline rounded-card p-body pmx-rise",
          )}
        >
          <div className="flex items-center justify-between">
            <Dialog.Title className="font-ui text-[0.95rem] font-semibold text-text">
              Export report
            </Dialog.Title>
            <Dialog.Close asChild>
              <button
                type="button"
                aria-label="Close export dialog"
                className="grid size-11 place-items-center rounded-control text-text-faint transition-colors hover:text-text lg:size-auto lg:p-hair"
              >
                <X className="size-4" aria-hidden />
              </button>
            </Dialog.Close>
          </div>

          <Dialog.Description className="mt-hair font-ui text-[0.82rem] text-text-muted">
            Choose a format.{" "}
            {fsa
              ? "A save picker lets you choose where to put the file."
              : "The file downloads to your default Downloads folder."}
          </Dialog.Description>

          <div className="mt-section flex flex-col gap-inline">
            {/* MD — always available */}
            <ExportMdButton exporting={exporting} onClick={handleMd} />

            {/* PDF — gated on server capability */}
            <ExportPdfButton
              exporting={exporting}
              exportCaps={exportCaps}
              onClick={() => handleFmt("pdf")}
            />

            {/* PDF template — the brand-theme spin, only where PDF export is real */}
            {exportCaps.pdf && (
              <TemplatePicker
                templates={templates}
                value={templateId}
                onChange={setTemplateId}
                disabled={exporting !== null}
                label="PDF template"
                id="pdf-template"
                dataControl="dr.export-template"
              />
            )}
          </div>

          {exportError && (
            <p role="alert" className="mt-inline font-ui text-[0.78rem] text-unsupported">
              {exportError}
            </p>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
