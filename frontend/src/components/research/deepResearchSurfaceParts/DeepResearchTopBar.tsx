/**
 * DeepResearchTopBar — the H1 + control row (Notify/Stop/Kill while running,
 * Resume/Kill while paused, Retry on error, MD/PDF export + New research once
 * a report exists) plus the include-follow-ups export modal it drives.
 * Relocated verbatim from DeepResearchSurface.tsx's `<header>` block. See that
 * file's header for the surface map.
 */
import {
  Ban,
  Bell,
  FileText,
  FileType,
  Loader2,
  Play,
  RotateCcw,
  Search,
  Square,
} from "lucide-react";
import { Link } from "react-router-dom";
import type { useDeepResearch } from "@/hooks/useDeepResearch";
import type { useDeepResearchDoneNotification } from "@/hooks/useDeepResearchDoneNotification";
import type { ReportExportFmt } from "@/api/deepResearch";
import type { ExportCapabilities } from "@/hooks/useExportCapabilities";
import { IncludeFollowUpsModal } from "../IncludeFollowUpsModal";
import { CTRL_BTN, KILL_BTN, NOTIFY_ARMED_BTN, PENDING_BTN } from "./styles";

interface Props {
  r: ReturnType<typeof useDeepResearch>;
  onStandardSearch: () => void;
  handleNewResearch: () => void;
  exportCaps: ExportCapabilities;
  doneNotify: ReturnType<typeof useDeepResearchDoneNotification>;
  topBarIncludeOpen: boolean;
  setTopBarIncludeOpen: (open: boolean) => void;
  handleTopBarExport: (fmt: ReportExportFmt) => void;
  handleTopBarIncludeConfirm: (seqs: number[]) => void;
}

export function DeepResearchTopBar({
  r,
  onStandardSearch,
  handleNewResearch,
  exportCaps,
  doneNotify,
  topBarIncludeOpen,
  setTopBarIncludeOpen,
  handleTopBarExport,
  handleTopBarIncludeConfirm,
}: Props) {
  // fix-c #4: disable while in-flight so a second click can't double-fire the
  // server export; shared by `disabled` and `aria-disabled` below (was two
  // separately-written copies of the same condition).
  const exportDisabled = r.exportPending !== null;
  const pdfDisabled = !exportCaps.pdf || exportDisabled;
  return (
    <>
      {/* Header row: H1 + actions */}
      <header className="flex flex-wrap items-baseline justify-between gap-inline border-b border-hairline pb-section">
        <h1 className="font-display text-[1.9rem] font-medium leading-tight tracking-tight text-text">
          {/* fix-c #6: the title is the REAL query, no "(resumed)" suffix.
              On a resumed run the query is recovered from the replayed
              ReportEvent / first user MessageEvent by the hook — the bare
              "(resumed)" sentinel stays internal to useDeepResearch and
              must NOT leak into the H1 (it isn't a real title). */}
          {r.query}
        </h1>
        <div className="flex items-center gap-inline">
          <button
            type="button"
            onClick={onStandardSearch}
            className={CTRL_BTN}
            data-disco-control="dr.standard-search"
          >
            <Search className="size-3.5" aria-hidden />
            Standard Search
          </button>
          {/* While running: Notify, Stop (pause, keeps partial), Kill (end, final). */}
          {r.status === "RUNNING" && (
            <>
              <button
                type="button"
                role="switch"
                aria-checked={doneNotify.armed}
                onClick={doneNotify.toggle}
                disabled={!r.cid}
                data-disco-control="dr.notify-done"
                className={doneNotify.armed ? NOTIFY_ARMED_BTN : CTRL_BTN}
                title="Notify me when this deep research run completes"
              >
                <Bell className="size-3.5" aria-hidden />
                Notify me when done
              </button>
              <button type="button" onClick={r.stop} data-disco-control="dr.stop" className={CTRL_BTN}>
                <Square className="size-3" aria-hidden />
                Stop
              </button>
              <button type="button" onClick={r.kill} data-disco-control="dr.kill" className={KILL_BTN}>
                <Ban className="size-3.5" aria-hidden />
                Kill
              </button>
            </>
          )}
          {/* Stopped (paused): Resume continues it; Kill ends it. */}
          {r.status === "PAUSED" && (
            <>
              <button type="button" onClick={r.resume} data-disco-control="dr.resume" className={CTRL_BTN}>
                <Play className="size-3.5 text-accent" aria-hidden />
                Resume
              </button>
              <button type="button" onClick={r.kill} data-disco-control="dr.kill" className={KILL_BTN}>
                <Ban className="size-3.5" aria-hidden />
                Kill
              </button>
            </>
          )}
          {/* Errored: Retry = a fresh run of the same query. */}
          {r.status === "ERROR" && (
            <button type="button" onClick={r.retry} data-disco-control="dr.retry" className={CTRL_BTN}>
              <RotateCcw className="size-3.5" aria-hidden />
              Retry
            </button>
          )}
          {r.report && (
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
                {r.exportPending === "md" ? (
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
                {r.exportPending === "pdf" ? (
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
          )}
          {(r.status === "FINISHED" || r.status === "ERROR") && (
            <Link to="/" onClick={handleNewResearch} data-disco-control="dr.new-research" className={CTRL_BTN}>
              <RotateCcw className="size-3.5" aria-hidden />
              New research
            </Link>
          )}
        </div>
      </header>

      {/* WALK-20: include-follow-ups modal for top-bar export buttons */}
      <IncludeFollowUpsModal
        open={topBarIncludeOpen}
        onOpenChange={setTopBarIncludeOpen}
        followUps={r.followUps}
        onConfirm={handleTopBarIncludeConfirm}
        actionLabel="Export"
      />
    </>
  );
}
