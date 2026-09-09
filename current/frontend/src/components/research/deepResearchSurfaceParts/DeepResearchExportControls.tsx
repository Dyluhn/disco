/**
 * DeepResearchTopBar — the MD / PDF export buttons, extracted verbatim from
 * DeepResearchTopBar.tsx's `{r.report && (...)}` block. Pulling this branch
 * cluster into its own component is what gets DeepResearchTopBar back under
 * the AST-complexity cap; the rendered markup is unchanged.
 */
import { FileText, FileType, Loader2 } from "lucide-react";
import type { ReportExportFmt } from "@/api/deepResearch";
import type { ExportCapabilities } from "@/hooks/useExportCapabilities";
import { CTRL_BTN, PENDING_BTN } from "./styles";

/** Deliberately takes plain values, not the whole `useDeepResearch` handle:
 *  this module lives in the shell context, and importing the research-context
 *  hook (even as a type) would open a new cross-context edge. */
interface Props {
  /** Whether a report exists yet — the exports render only once it does. */
  hasReport: boolean;
  /** The format whose server export is in flight, or null when idle. */
  exportPending: ReportExportFmt | null;
  exportCaps: ExportCapabilities;
  handleTopBarExport: (fmt: ReportExportFmt) => void;
}

export function DeepResearchExportControls({
  hasReport,
  exportPending,
  exportCaps,
  handleTopBarExport,
}: Props) {
  if (!hasReport) return null;
  // fix-c #4: disable while in-flight so a second click can't double-fire the
  // server export; shared by `disabled` and `aria-disabled` below (was two
  // separately-written copies of the same condition).
  const exportDisabled = exportPending !== null;
  const pdfDisabled = !exportCaps.pdf || exportDisabled;
  return (
    <>
      <button
        type="button"
        onClick={() => handleTopBarExport("md")}
        disabled={exportDisabled}
        aria-disabled={exportDisabled}
        data-disco-control="dr.export.topbar.md"
        data-export-cap="true"
        className={exportDisabled ? PENDING_BTN : CTRL_BTN}
        title="Download as Markdown"
      >
        {exportPending === "md" ? (
          <Loader2 className="size-3.5 animate-spin" aria-hidden />
        ) : (
          <FileText className="size-3.5" aria-hidden />
        )}
        MD
      </button>
      <button
        type="button"
        onClick={() => handleTopBarExport("pdf")}
        // fix-c #4: disable while in-flight so a second click can't
        // double-fire the server export; the icon swaps to a spinner
        // when this fmt is the active pending one. Mirrors the
        // ExportModal pattern in NeedMoreCard.
        disabled={pdfDisabled}
        aria-disabled={pdfDisabled}
        data-disco-control="dr.export.topbar.pdf"
        data-export-cap={String(exportCaps.pdf)}
        className={pdfDisabled ? PENDING_BTN : CTRL_BTN}
        title={
          exportCaps.pdf
            ? "Download as PDF"
            : "PDF export unavailable — the server has no WeasyPrint"
        }
      >
        {exportPending === "pdf" ? (
          <Loader2 className="size-3.5 animate-spin" aria-hidden />
        ) : (
          <FileType className="size-3.5" aria-hidden />
        )}
        PDF
      </button>
      {!exportCaps.pdf && (
        <span className="font-ui text-[0.68rem] text-text-faint">
          PDF needs WeasyPrint on the server
        </span>
      )}
    </>
  );
}
