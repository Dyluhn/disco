/**
 * NeedMoreCard — the PDF export option in ExportModal, split out (see
 * ExportMdButton's header comment for why). PDF carries more branching than
 * MD because it's gated on server capability, so it gets its own callable.
 */

import { ArrowDown, FileType, Loader2 } from "lucide-react";
import { cn } from "@/lib/cn";
import type { ExportCapabilities } from "@/hooks/useExportCapabilities";

export interface ExportPdfButtonProps {
  exporting: "md" | "pdf" | null;
  exportCaps: ExportCapabilities;
  onClick: () => void;
}

export function ExportPdfButton({ exporting, exportCaps, onClick }: ExportPdfButtonProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={!exportCaps.pdf || exporting !== null}
      aria-disabled={!exportCaps.pdf}
      title={
        exportCaps.pdf
          ? "Download as PDF"
          : "PDF export requires WeasyPrint on the server"
      }
      data-disco-control="dr.export.modal.pdf"
      data-export-cap={String(exportCaps.pdf)}
      className={cn(
        "flex items-center gap-inline rounded-control border p-inline",
        "font-ui text-[0.85rem] transition-colors text-left",
        exportCaps.pdf
          ? "border-hairline text-text-muted hover:border-accent hover:text-accent"
          : "border-dashed border-hairline text-text-faint opacity-50 cursor-not-allowed",
        exporting === "pdf" && "opacity-60 cursor-wait",
        exporting !== null && exporting !== "pdf" && exportCaps.pdf && "opacity-40",
      )}
    >
      <FileType
        className={cn("size-4 shrink-0", exportCaps.pdf ? "text-accent" : "text-text-faint")}
        aria-hidden
      />
      <div className="flex-1">
        <div className={cn("font-medium", exportCaps.pdf ? "text-text" : "text-text-faint")}>
          PDF
        </div>
        <div className="text-[0.74rem] text-text-faint">
          {exportCaps.pdf ? "Print-ready PDF" : "Needs WeasyPrint on the server"}
        </div>
      </div>
      {exporting === "pdf" ? (
        <Loader2 className="size-4 animate-spin text-text-faint" aria-hidden />
      ) : (
        <ArrowDown
          className={cn("size-3.5", exportCaps.pdf ? "text-text-faint" : "opacity-30 text-text-faint")}
          aria-hidden
        />
      )}
    </button>
  );
}
