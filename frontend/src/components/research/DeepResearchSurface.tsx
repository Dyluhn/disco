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

import { Ban, Bell, FileText, FileType, Loader2, Play, RotateCcw, Settings as SettingsIcon, Square } from "lucide-react";
import { UploadComposer } from "@/components/build/BuildSurface";
import { useCallback, useEffect, useState } from "react";
import { publishRunStatus } from "@/lib/runStatusBridge";
import { Link } from "react-router-dom";
import { Markdown } from "@/components/Markdown";
import { PlanPanel } from "@/components/build/PlanPanel";
import { useDeepResearch } from "@/hooks/useDeepResearch";
import { useDeepResearchDoneNotification } from "@/hooks/useDeepResearchDoneNotification";
import { useExportCapabilities } from "@/hooks/useExportCapabilities";
import { QueryInput } from "@/components/QueryInput";
import { SuggestionChips } from "@/components/SuggestionChips";
import { SpaceGroundingControl } from "@/components/SpaceGroundingControl";
import { EmptyState, ErrorState } from "@/components/states";
import type { ScopeId } from "@/shell/mode";
import type { ReportExportFmt } from "@/api/deepResearch";
import { DeepBoundedNotice } from "./DeepBoundedNotice";
import { DeepProgressStrip } from "./DeepProgressStrip";
import { asGroundedAnswer, DeepReportView } from "./DeepReportView";
import { DepthTierSelector, type Tier } from "./DepthTierSelector";
import { RecencySelector } from "./RecencySelector";
import { IterativeToggle } from "./IterativeToggle";
import { TieredSourcePanel } from "./TieredSourcePanel";
import { NeedMoreCard } from "./NeedMoreCard";
import { FollowUpStatus } from "./FollowUpStatus";
import { IncludeFollowUpsModal } from "./IncludeFollowUpsModal";

interface Props {
  /** When passed via /deep/:cid, the surface resumes the existing conversation. */
  resumeCid?: string | null;
  /** Bubble scope changes up to the parent ResearchSurface — when the user
   *  picks "Standard" mid-surface, the parent re-renders with the standard
   *  scope and Deep Research unmounts cleanly. */
  onScopeChange?: (next: ScopeId) => void;
  /** Leader-model override carried in from the parent ResearchSurface. Without
   *  it, switching search → deep-research silently dropped the user-selected
   *  model (fix-c #2). Defaults to null = "use Settings default". */
  initialLeaderId?: string | null;
  /** W-06: the SHARED draft string lifted to ResearchSurface, so the query text
   *  typed in the standard search box survives the toggle into Deep Research
   *  (this surface mounts its own QueryInput). Omitted on the /deep/:cid resume
   *  path → the input falls back to uncontrolled. */
  draft?: string;
  onDraftChange?: (next: string) => void;
  initialSpaceIds?: string[];
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
export function DeepResearchSurface({
  resumeCid,
  onScopeChange,
  initialLeaderId,
  draft,
  onDraftChange,
  initialSpaceIds,
}: Props) {
  const r = useDeepResearch(resumeCid, initialLeaderId, initialSpaceIds);
  const started = r.started;
  const [localDraft, setLocalDraft] = useState("");
  const draftValue = onDraftChange ? (draft ?? "") : localDraft;
  const setDraftValue = useCallback(
    (next: string) => {
      if (onDraftChange) onDraftChange(next);
      else setLocalDraft(next);
    },
    [onDraftChange],
  );
  // Gap #4: publish the DR run status to the W6 E2E bridge (await RUNNING/PAUSED/
  // FINISHED/ERROR); clear on unmount.
  useEffect(() => {
    publishRunStatus(r.status);
    return () => publishRunStatus(null);
  }, [r.status]);
  // RP-07: PDF/DOCX run in the agent-server (weasyprint / pandoc). The buttons are
  // gated on the REAL server capability — never a clickable button that 500s. MD
  // always works; PDF/DOCX enable wherever the server has the toolchain.
  const exportCaps = useExportCapabilities();
  const doneNotify = useDeepResearchDoneNotification({
    cid: r.cid,
    status: r.status,
    title: r.query,
  });

  // WALK-20: include-follow-ups modal for the TOP-BAR export buttons.
  // When followUps exist, clicking MD/PDF/DOCX in the header opens this modal
  // first; on confirm the real export runs with the selected seqs threaded in.
  const [topBarPendingFmt, setTopBarPendingFmt] = useState<ReportExportFmt | null>(null);
  const [topBarIncludeOpen, setTopBarIncludeOpen] = useState(false);

  const handleTopBarExport = useCallback(
    (fmt: ReportExportFmt) => {
      if (r.followUps.length > 0) {
        setTopBarPendingFmt(fmt);
        setTopBarIncludeOpen(true);
      } else {
        void r.exportReportByFmt(fmt);
      }
    },
    [r],
  );

  const handleTopBarIncludeConfirm = useCallback(
    (seqs: number[]) => {
      setTopBarIncludeOpen(false);
      if (topBarPendingFmt) {
        void r.exportReportByFmt(topBarPendingFmt, seqs);
      }
      setTopBarPendingFmt(null);
    },
    [topBarPendingFmt, r],
  );

  // Gap #39 — a stable, assertable DR lifecycle phase. The run itself is
  // model+live-search+streaming (non-deterministic), but the PHASE TRANSITIONS
  // (idle → planning → running → paused/error → done) are deterministic and can
  // be asserted by the harness off this single attribute.
  const drPhase: string = r.awaitingPlan
    ? "planning"
    : r.status === "RUNNING"
      ? r.plan
        ? "running"
        : "planning"
      : r.status === "PAUSED"
        ? "paused"
        : r.status === "ERROR"
          ? "error"
          : r.status === "FINISHED"
            ? "done"
            : "idle";

  if (!started) {
    return (
      <div className="flex min-h-full flex-col pt-section" data-dr-phase={drPhase}>
        <main className="flex flex-1 flex-col items-center justify-center gap-major px-body pb-[12vh]">
          <EmptyState title="Deep Research" subtitle="Multi-step reports with cited evidence." />
          <div className="flex w-full max-w-measure flex-col gap-inline">
            <QueryInput
              onSubmit={r.submit}
              busy={r.submitting}
              autoFocus
              value={draftValue}
              onValueChange={setDraftValue}
              placeholder="Ask a research question that deserves a multi-page report…"
              leaderId={r.leaderId}
              onLeaderChange={r.setLeaderId}
              scope={"deep_research" as ScopeId}
              onScopeChange={onScopeChange ?? (() => {})}
              // BW-04: Deep Research does not honor a per-run reasoning-effort
              // flag (its create frame carries depth/recency/iterative, not
              // `think`), so the Think toggle is OMITTED rather than rendered
              // inert (always-off, no-op) — no false affordance. Omitting
              // onThinkChange drops only the toggle; model/scope still render.
              extraControls={
                // R10: Depth/Recency/Iterative now sit INLINE in the same pill row
                // as the model/scope cluster (via QueryInput's `extraControls`),
                // not a full-width footer block that grew the card and reflowed the
                // centered layout. The hint paragraph is dropped (it was layout bulk;
                // the leader pill already names the driver model).
                <>
                  <DepthTierSelector value={r.depthTier as Tier} onChange={r.setDepthTier} />
                  <RecencySelector value={r.recencyWindow} onChange={r.setRecencyWindow} />
                  <IterativeToggle value={r.iterative} onChange={r.setIterative} />
                  <SpaceGroundingControl selected={r.spaceIds} onChange={r.setSpaceIds} />
                  {/* G1/DR-4 + runthru-v2 #9: UploadComposer always rendered (it
                      self-disables when cid is null) so the attach affordance does
                      NOT vanish during the brief preCid re-create window on a
                      settings change — it just dims until the new cid resolves. */}
                  <UploadComposer cid={r.preCid} ensureCid={r.ensurePreCid} />
                </>
              }
            />
            {r.submitError && (
              <p role="alert" className="font-ui text-[0.8rem] text-unsupported">
                {r.submitError instanceof Error
                  ? r.submitError.message
                  : "Couldn't start deep research — the server didn't respond. Try again."}
              </p>
            )}
          </div>
          <SuggestionChips surface="deep_research" onPick={setDraftValue} />
        </main>
      </div>
    );
  }

  return (
    <div className="flex min-h-full flex-col pt-section" data-dr-phase={drPhase}>
      <main className="mx-auto flex w-full max-w-doc flex-1 flex-col gap-section px-body pb-major">
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
                  className={
                    doneNotify.armed
                      ? `${CTRL_BTN} border-accent/50 text-accent`
                      : CTRL_BTN
                  }
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
                  data-disco-control="dr.export.topbar.md"
                  data-export-cap="true"
                  className={CTRL_BTN}
                  title="Download as Markdown"
                >
                  <FileText className="size-3.5" aria-hidden />
                  MD
                </button>
                <button
                  type="button"
                  onClick={() => handleTopBarExport("pdf")}
                  // fix-c #4: disable while in-flight so a second click can't
                  // double-fire the server export; the icon swaps to a spinner
                  // when this fmt is the active pending one. Mirrors the
                  // ExportModal pattern in NeedMoreCard.
                  disabled={!exportCaps.pdf || r.exportPending !== null}
                  aria-disabled={!exportCaps.pdf || r.exportPending !== null}
                  data-disco-control="dr.export.topbar.pdf"
                  data-export-cap={String(exportCaps.pdf)}
                  className={exportCaps.pdf ? CTRL_BTN : PENDING_BTN}
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
              <Link to="/" className={CTRL_BTN}>
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

        {/* Error state */}
        {r.status === "ERROR" && (
          <ErrorState
            message={r.error ?? "The research run failed."}
            onRetry={r.retry}
          />
        )}

        {/* Planning loader — the gap between submit and the first PlanEvent
            arriving (typically 3-8 s). Without this the area below the H1 is
            completely blank, which looks like a hang. */}
        {!r.plan && r.status === "RUNNING" && (
          <div className="flex items-center gap-inline font-ui text-[0.86rem] text-text-muted">
            <Loader2 className="size-4 animate-spin text-accent" aria-hidden />
            Planning the research…
          </div>
        )}

        {/* Plan-edit gate — present only at AWAITING_PLAN_APPROVAL.
            Gap #52: PlanPanel is the SHARED build/DR plan gate (owned by the
            build lane) so its internal handles aren't surface-scoped here.
            Wrap the DR-side mount point with a stable marker so the harness can
            detect "the DR plan gate is showing" without reaching into PlanPanel. */}
        {r.awaitingPlan && r.plan && (
          <div data-dr-plan-gate="">
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
              // fix-c #1: the shared PlanPanel defaults its approve button to
              // "Approve & build" (build surface). On deep research the same
              // gate means something different — override.
              approveLabel="Approve research plan"
            />
          </div>
        )}

        {/* During the run + after: the progress strip (collapses on finish) */}
        {!r.awaitingPlan && r.plan && (
          <DeepProgressStrip
            plan={r.plan}
            progress={r.progress}
            trace={r.trace}
            stats={r.stats}
            status={r.status}
            followUpStatus={r.followUpStatus}
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
                    // fix-c #5: use the dedicated callback — set-then-submit
                    // closed over the OLD depthTier and re-ran at the same
                    // bounded tier. runExhaustive takes the tier directly.
                    if (r.query) r.runExhaustive(r.query);
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
            cid={r.cid}
          />
        )}

        {/* The 3-tier sources panel — only after the report arrives. */}
        {r.report && (
          <div className="mx-auto w-full max-w-doc">
            <TieredSourcePanel tiers={r.sources} />
          </div>
        )}

        {/* Wave 2 — follow-up Q&A. The RP-13 follow-up path appends the user's
            question + the agent's grounded answer as MessageEvents after the
            report; DeepReportView only renders the ReportEvent, so these were
            generated server-side but never shown (the answer vanished). Render
            them here as a thread. Plumbing messages are already suppressed in
            the hook's `followUps` derivation (whitelist boundary).
            Gate on report + FINISHED (WALK-08 A4): before the report exists,
            reportSeq would be -1 and the run's own initiating message would
            be misclassified as a follow-up. This gate prevents that ghost. */}
        {r.report && r.status === "FINISHED" && r.followUps.length > 0 && (
          <div className="mx-auto w-full max-w-doc space-y-section">
            {r.followUps.map((m, i) =>
              m.message.role === "user" ? (
                <p key={i} className="font-ui text-[0.95rem] font-medium text-text">
                  <span className="text-text-faint">Follow-up: </span>
                  {m.message.content}
                </p>
              ) : (
                <div
                  key={i}
                  className="rounded-card border border-hairline bg-surface-1 px-body py-body"
                >
                  <p className="mb-inline font-ui text-[0.72rem] uppercase tracking-wide text-text-faint">
                    Answer
                  </p>
                  {/* Render through <Markdown> so the model's markdown (bold,
                      lists, code, etc.) formats properly. Pass the report's
                      passage corpus so [[id]] citation markers resolve to chips
                      instead of staying as raw brackets. */}
                  <div className="prose-reading">
                    <Markdown answer={asGroundedAnswer(r.report, r.query ?? "")}>
                      {m.message.content}
                    </Markdown>
                  </div>
                </div>
              ),
            )}
          </div>
        )}

        {/* WALK-12: follow-up activity indicator. Shows "Reading sources…" /
            "Writing answer…" while a follow-up is generating so the UI
            doesn't look frozen. Driven by followUpStatus (WALK-11) + the
            phase ActionEvents the backend emits during _follow_up_deep_research.
            Rendered BELOW the Q&A thread and ABOVE the NeedMoreCard. */}
        <FollowUpStatus
          followUpStatus={r.followUpStatus}
          events={r.events}
        />

        {/* "Need More?" card — Ask a Follow-Up / Export as… / Audio Overview.
            Only visible once the report is FINISHED and present. */}
        {r.status === "FINISHED" && r.report && r.cid && (
          <div className="mx-auto w-full max-w-doc">
            <NeedMoreCard
              report={r.report}
              cid={r.cid}
              onFollowUp={(question) => r.followUp(question)}
              followUpBusy={false}
              followUps={r.followUps}
            />
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
