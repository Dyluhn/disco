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
  Headphones,
  MessageCircleQuestion,
  Play,
  Presentation,
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
import { DeepResearchExportControls } from "./DeepResearchExportControls";
import type { ReportActionName } from "../NeedMoreCard";
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
  /** Press one of the report actions the "Need More?" card owns (UI-25). */
  requestReportAction: (name: ReportActionName) => void;
}

/** The three report actions that used to be reachable only by scrolling past
 *  the whole report and its sources. Export is already here as MD / PDF, so it
 *  is not repeated. The card keeps all four; these are the same presses. */
const REPORT_ACTIONS: Array<{
  name: ReportActionName;
  label: string;
  title: string;
  Icon: typeof Headphones;
}> = [
  {
    name: "follow_up",
    label: "Follow-up",
    title: "Ask a follow-up question about this report",
    Icon: MessageCircleQuestion,
  },
  {
    name: "audio",
    label: "Audio",
    title: "Generate an audio overview of this report",
    Icon: Headphones,
  },
  {
    name: "deck",
    label: "Deck",
    title: "Turn this report into a slide deck",
    Icon: Presentation,
  },
];

/**
 * Stop, and what replaces it once Stop has been asked for.
 *
 * Presence implies affordance: while the run is winding down a second press
 * does nothing, so a live Stop button would promise an action that no longer
 * exists. The stopping state is read from the run's own `stop_requested`
 * marker — which clears on the next status the run writes — rather than from a
 * local flag that could drift from what the server actually did.
 */
function StopControl({ r }: { r: ReturnType<typeof useDeepResearch> }) {
  if (r.activity.stopRequested !== null) {
    return (
      <button
        type="button"
        disabled
        aria-disabled
        data-disco-control="dr.stopping"
        className={PENDING_BTN}
      >
        <Square className="size-3" aria-hidden />
        Stopping…
      </button>
    );
  }
  return (
    <button type="button" onClick={r.stop} data-disco-control="dr.stop" className={CTRL_BTN}>
      <Square className="size-3" aria-hidden />
      Stop
    </button>
  );
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
  requestReportAction,
}: Props) {
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
              <StopControl r={r} />
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
          <DeepResearchExportControls
            hasReport={Boolean(r.report)}
            exportPending={r.exportPending}
            exportCaps={exportCaps}
            handleTopBarExport={handleTopBarExport}
          />
          {/* UI-25: the report's own actions, next to the exports. */}
          {r.status === "FINISHED" &&
            r.report &&
            REPORT_ACTIONS.map(({ name, label, title, Icon }) => (
              <button
                key={name}
                type="button"
                onClick={() => requestReportAction(name)}
                data-disco-control={`dr.report-action.${name}`}
                className={CTRL_BTN}
                title={title}
              >
                <Icon className="size-3.5" aria-hidden />
                {label}
              </button>
            ))}
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
