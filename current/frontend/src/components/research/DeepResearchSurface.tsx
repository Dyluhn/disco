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
 *
 * ---- Surface map (Epic 12-C decomposition) --------------------------------
 *
 * This component now only: calls the frozen `useDeepResearch` hook, calls
 * `useDeepResearchSurfaceState` for surface-local state/callbacks, derives
 * `drPhase`, and picks between the composer (not started) and the top bar +
 * run view + report view (started). All the JSX conditional density that
 * used to live here has moved to `deepResearchSurfaceParts/`:
 *
 *   - styles.ts                    — the CTRL_BTN token constants
 *   - useDeepResearchSurfaceState  — draft state, new-research, top-bar export
 *   - DeepResearchComposer         — the pre-run composer (empty state)
 *   - DeepResearchTopBar           — H1 + Notify/Stop/Kill/Retry/Export/New
 *   - DeepResearchRunView          — error/planning-loader/plan-gate/progress/bounded
 *   - DeepResearchReportView       — report/sources/follow-ups/need-more/footer
 */

import { useNavigate } from "react-router-dom";
import { useDeepResearch } from "@/hooks/useDeepResearch";
import type { ScopeId } from "@/shell/mode";
import { useDeepResearchSurfaceState } from "./deepResearchSurfaceParts/useDeepResearchSurfaceState";
import { DeepResearchComposer } from "./deepResearchSurfaceParts/DeepResearchComposer";
import { DeepResearchTopBar } from "./deepResearchSurfaceParts/DeepResearchTopBar";
import { DeepResearchRunView } from "./deepResearchSurfaceParts/DeepResearchRunView";
import { DeepResearchReportView } from "./deepResearchSurfaceParts/DeepResearchReportView";

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
  initialSources?: string[];
}

export function DeepResearchSurface({
  resumeCid,
  onScopeChange,
  initialLeaderId,
  draft,
  onDraftChange,
  initialSources,
}: Props) {
  const navigate = useNavigate();
  const r = useDeepResearch(resumeCid, initialLeaderId, initialSources);
  const started = r.started;
  const {
    draftValue,
    setDraftValue,
    handleNewResearch,
    exportCaps,
    doneNotify,
    topBarIncludeOpen,
    setTopBarIncludeOpen,
    handleTopBarExport,
    handleTopBarIncludeConfirm,
  } = useDeepResearchSurfaceState({ r, onScopeChange, draft, onDraftChange });

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
        <DeepResearchComposer
          r={r}
          draftValue={draftValue}
          setDraftValue={setDraftValue}
          onScopeChange={onScopeChange}
        />
      </div>
    );
  }

  return (
    <div className="flex min-h-full flex-col pt-section" data-dr-phase={drPhase}>
      <main className="mx-auto flex w-full max-w-doc flex-1 flex-col gap-section px-body pb-major">
        <DeepResearchTopBar
          r={r}
          onStandardSearch={() => {
            if (onScopeChange) onScopeChange("standard");
            else navigate("/");
          }}
          handleNewResearch={handleNewResearch}
          exportCaps={exportCaps}
          doneNotify={doneNotify}
          topBarIncludeOpen={topBarIncludeOpen}
          setTopBarIncludeOpen={setTopBarIncludeOpen}
          handleTopBarExport={handleTopBarExport}
          handleTopBarIncludeConfirm={handleTopBarIncludeConfirm}
        />
        <DeepResearchRunView r={r} />
        <DeepResearchReportView r={r} />
      </main>
    </div>
  );
}
