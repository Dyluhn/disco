/**
 * NeedMoreCard — the Markdown export option in ExportModal, split out so
 * ExportModal's own mccabe stays low (the branching in the format buttons'
 * className/icon logic is what pushed the pre-split component over the cap —
 * distributing it into a dedicated callable per button is what brings it back
 * under, without changing a single rendered attribute or string).
 */

import { ArrowDown, FileText, Loader2 } from "lucide-react";
import { cn } from "@/lib/cn";

export interface ExportMdButtonProps {
  exporting: "md" | "pdf" | null;
  onClick: () => void;
}

export function ExportMdButton({ exporting, onClick }: ExportMdButtonProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={exporting !== null}
      data-disco-control="dr.export.modal.md"
      className={cn(
        "flex items-center gap-inline rounded-control border border-hairline p-inline",
        "font-ui text-[0.85rem] text-text-muted transition-colors text-left",
        "hover:border-accent hover:text-accent",
        exporting === "md" && "opacity-60 cursor-wait",
        exporting !== null && exporting !== "md" && "opacity-40",
      )}
    >
      <FileText className="size-4 shrink-0 text-accent" aria-hidden />
      <div className="flex-1">
        <div className="font-medium text-text">Markdown</div>
        <div className="text-[0.74rem] text-text-faint">Portable plain-text</div>
      </div>
      {exporting === "md" ? (
        <Loader2 className="size-4 animate-spin text-text-faint" aria-hidden />
      ) : (
        <ArrowDown className="size-3.5 text-text-faint" aria-hidden />
      )}
    </button>
  );
}
