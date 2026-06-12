/**
 * DeepResearchSurface — the top-level Deep Research experience.
 *
 *   ┌─ Empty state ────────────────────────────────────────────┐
 *   │ EmptyState + QueryInput + DepthTierSelector              │
 *   └──────────────────────────────────────────────────────────┘
 *   ┌─ Started (running or finished) ──────────────────────────┐
 *   │ H1 — the question (font-display)                         │
 *   │                                                          │
 *   │ Plan-edit gate (PlanPanel in gate mode)                  │  ← only at AWAITING_PLAN_APPROVAL
 *   │   ── or ──                                               │
 *   │ DeepProgressStrip (checklist + trace + stats)            │  ← collapsing
 *   │                                                          │
 *   │ DeepBoundedNotice                                        │  ← when bounded_by
 *   │                                                          │
 *   │ DeepReportView (assembling → finished report)            │
 *   │                                                          │
 *   │ TieredSourcePanel (Cited / Reviewed / Discovered)        │  ← when report
 *   │                                                          │
 *   │ Export, Refine                                           │  ← FINISHED actions
 *   └──────────────────────────────────────────────────────────┘
 *
 * Reuses: PlanPanel (gate), ActivityFeed (via DeepProgressStrip), Citation
 * cards (via DeepReportView → CitedText), QueryInput, the conversation WS
 * subscription, the Build status state machine.
 */

import { Ban, Download, File, FileText, FileType, Play, RotateCcw, Settings as SettingsIcon, Square } from "lucide-react";
import { Link } from "react-router-dom";
import { PlanPanel } from "@/components/build/PlanPanel";
import { useDeepResearch } from "@/hooks/useDeepResearch";
import { QueryInput } from "@/components/QueryInput";
import { EmptyState, ErrorState } from "@/components/states";
import type { ScopeId } from "@/shell/mode";
import { DeepBoundedNotice } from "./DeepBoundedNotice";
import { DeepProgressStrip } from "./DeepProgressStrip";
import { DeepReportView } from "./DeepReportView";
import { DepthTierSelector, type Tier } from "./DepthTierSelector";
import { TieredSourcePanel } from "./TieredSourcePanel";

interface Props {
  /** When passed via /deep/:cid, the surface resumes the existing conversation. */
  resumeCid?: string | null;
  /** Bubble scope changes up to the parent ResearchSurface — when the user
   *  picks "Standard" mid-surface, the parent re-renders with the standard
   *  scope and Deep Research unmounts cleanly. */
  onScopeChange?: (next: ScopeId) => void;
}

const CTRL_BTN =
  "flex items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:text-text";
// Kill is destructive (ends the run for good) → warn-tinted, distinct from Stop.
const KILL_BTN =
  "flex items-center gap-hair rounded-control border border-warn/40 px-inline py-hair font-ui text-[0.78rem] text-warn transition-colors hover:bg-warn/10";
// A control that exists but is not yet wired — visibly inert (no hover, dimmed,
// not-allowed cursor), never a click that silently errors. NO FALSE AFFORDANCES.
const PENDING_BTN =
  "flex items-center gap-hair rounded-control border border-hairline border-dashed px-inline py-hair font-ui text-[0.78rem] text-text-faint opacity-50 cursor-not-allowed";
// RP-07: PDF/DOCX generation needs pandoc + WeasyPrint, which ship in the sandbox
// image rebuild (BP-08/BP-04 VM-201). Until that lands they CANNOT work, so the
// buttons are disabled + labelled — not clickable buttons that 500. Flip to true
// in the same change that adds the toolchain to deploy/sandbox/Dockerfile.
const EXPORT_BINARY_FORMATS_READY = false;

export function DeepResearchSurface({ resumeCid, onScopeChange }: Props) {
  const r = useDeepResearch(resumeCid);
  const started = r.started;

  if (!started) {
    return (
      <div className="flex min-h-full flex-col pt-section">
        <main className="flex flex-1 flex-col items-center justify-center gap-major px-body pb-[12vh]">
          <EmptyState />
          <div className="flex w-full max-w-measure flex-col gap-inline">
            <QueryInput
              onSubmit={r.submit}
              busy={r.submitting}
              autoFocus
              placeholder="Ask a research question that deserves a multi-page report…"
              leaderId={r.leaderId}
              onLeaderChange={r.setLeaderId}
              scope={"deep_research" as ScopeId}
              onScopeChange={onScopeChange ?? (() => {})}
              think={false}
              onThinkChange={() => {}}
            />
            <div className="flex items-center justify-between gap-inline">
              <DepthTierSelector value={r.depthTier as Tier} onChange={r.setDepthTier} />
              <p className="font-ui text-[0.74rem] text-text-faint">
                Deep runs take minutes. The leader pill picks the driver model.
              </p>
            </div>
            {r.submitError && (
              <p role="alert" className="font-ui text-[0.8rem] text-unsupported">
                {r.submitError instanceof Error
                  ? r.submitError.message
                  : "Couldn't start deep research — the server didn't respond. Try again."}
              </p>
            )}
          </div>
        </main>
      </div>
    );
  }

  return (
    <div className="flex min-h-full flex-col pt-section">
      <main className="mx-auto flex w-full max-w-doc flex-1 flex-col gap-section px-body pb-major">
        {/* Header row: H1 + actions */}
        <header className="flex flex-wrap items-baseline justify-between gap-inline border-b border-hairline pb-section">
          <h1 className="font-display text-[1.9rem] font-medium leading-tight tracking-tight text-text">
            {r.query}
          </h1>
          <div className="flex items-center gap-inline">
            {/* While running: Stop (pause, keeps partial) + Kill (end, final). */}
            {r.status === "RUNNING" && (
              <>
                <button type="button" onClick={r.stop} className={CTRL_BTN}>
                  <Square className="size-3" aria-hidden />
                  Stop
                </button>
                <button type="button" onClick={r.kill} className={KILL_BTN}>
                  <Ban className="size-3.5" aria-hidden />
                  Kill
                </button>
              </>
            )}
            {/* Stopped (paused): Resume continues it; Kill ends it. */}
            {r.status === "PAUSED" && (
              <>
                <button type="button" onClick={r.resume} className={CTRL_BTN}>
                  <Play className="size-3.5 text-accent" aria-hidden />
                  Resume
                </button>
                <button type="button" onClick={r.kill} className={KILL_BTN}>
                  <Ban className="size-3.5" aria-hidden />
                  Kill
                </button>
              </>
            )}
            {/* Errored: Retry = a fresh run of the same query. */}
            {r.status === "ERROR" && (
              <button type="button" onClick={r.retry} className={CTRL_BTN}>
                <RotateCcw className="size-3.5" aria-hidden />
                Retry
              </button>
            )}
            {r.report && (
              <>
                <button
                  type="button"
                  onClick={() => r.exportReportByFmt("md")}
                  className={CTRL_BTN}
                  title="Download as Markdown"
                >
                  <FileText className="size-3.5" aria-hidden />
                  MD
                </button>
                <button
                  type="button"
                  onClick={() => r.exportReportByFmt("pdf")}
                  disabled={!EXPORT_BINARY_FORMATS_READY}
                  aria-disabled={!EXPORT_BINARY_FORMATS_READY}
                  className={EXPORT_BINARY_FORMATS_READY ? CTRL_BTN : PENDING_BTN}
                  title={
                    EXPORT_BINARY_FORMATS_READY
                      ? "Download as PDF"
                      : "PDF export arrives with the next sandbox update"
                  }
                >
                  <FileType className="size-3.5" aria-hidden />
                  PDF
                </button>
                <button
                  type="button"
                  onClick={() => r.exportReportByFmt("docx")}
                  disabled={!EXPORT_BINARY_FORMATS_READY}
                  aria-disabled={!EXPORT_BINARY_FORMATS_READY}
                  className={EXPORT_BINARY_FORMATS_READY ? CTRL_BTN : PENDING_BTN}
                  title={
                    EXPORT_BINARY_FORMATS_READY
                      ? "Download as DOCX"
                      : "DOCX export arrives with the next sandbox update"
                  }
                >
                  <File className="size-3.5" aria-hidden />
                  DOCX
                </button>
                {!EXPORT_BINARY_FORMATS_READY && (
                  <span className="font-ui text-[0.68rem] text-text-faint">
                    PDF / DOCX arrive with the next sandbox update
                  </span>
                )}
              </>
            )}
            {(r.status === "FINISHED" || r.status === "ERROR") && (
              <Link to="/" className={CTRL_BTN}>
                <RotateCcw className="size-3.5" aria-hidden />
                New research
              </Link>
            )}
          </div>
        </header>

        {/* Error state */}
        {r.status === "ERROR" && (
          <ErrorState
            message={r.error ?? "The research run failed."}
            onRetry={r.retry}
          />
        )}

        {/* Plan-edit gate — present only at AWAITING_PLAN_APPROVAL */}
        {r.awaitingPlan && r.plan && (
          <PlanPanel
            plan={{
              id: r.plan.id,
              summary: r.plan.summary,
              steps: r.plan.steps,
              revision: r.plan.revision,
              context: r.plan.context,
            }}
            progress={r.progress}
            onApprove={r.approvePlan}
            onRevise={r.requestPlan}
          />
        )}

        {/* During the run + after: the progress strip (collapses on finish) */}
        {!r.awaitingPlan && r.plan && (
          <DeepProgressStrip
            plan={r.plan}
            progress={r.progress}
            trace={r.trace}
            stats={r.stats}
            status={r.status}
          />
        )}

        {/* The bounded-by honest notice — when the engine bounded out */}
        {r.report?.bounded_by && (
          <DeepBoundedNotice
            report={r.report}
            plan={r.plan}
            onTryExhaustive={
              r.report.depth_tier === "exhaustive"
                ? undefined
                : () => {
                    // Set the tier and resubmit. The current cid is left in
                    // history; a fresh run goes deeper.
                    r.setDepthTier("exhaustive");
                    if (r.query) r.submit(r.query);
                  }
            }
          />
        )}

        {/* The report — assembles section-by-section while RUNNING, settled
            after FINISHED. Always rendered once we have a plan so the
            reader sees the document forming. */}
        {!r.awaitingPlan && r.plan && (
          <DeepReportView
            query={r.query ?? ""}
            summary={r.report?.summary ?? null}
            assembling={r.assembling}
            report={r.report}
          />
        )}

        {/* The 3-tier sources panel — only after the report arrives. */}
        {r.report && (
          <div className="mx-auto w-full max-w-doc">
            <TieredSourcePanel tiers={r.sources} />
          </div>
        )}

        {/* Footer: settings link (calm) */}
        {r.status === "FINISHED" && (
          <footer className="flex items-center justify-end pt-section">
            <Link
              to="/settings"
              className="flex items-center gap-hair font-ui text-[0.74rem] text-text-faint hover:text-text-muted"
            >
              <SettingsIcon className="size-3" aria-hidden />
              Settings
            </Link>
          </footer>
        )}
      </main>
    </div>
  );
}
