/**
 * DeepResearchSurface — the top-level Deep Research experience.
 *
 *   ┌─ Empty state ────────────────────────────────────────────┐
 *   │ EmptyState + QueryInput + DepthTierSelector              │
 *   └──────────────────────────────────────────────────────────┘
 *   ┌─ Started (running or finished) ──────────────────────────┐
 *   │ H1 — the question (font-display)                         │
 *   │                                                          │
 *   │ DeepProgressStrip (brief + trace + stats)                │  ← collapsing
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
 * v2 (gateless): submitting STARTS the research. There is no plan proposal and
 * no approve/revise gate on this surface — the model's brief is its first
 * visible output, and Stop / Kill / Resume stay live for the whole run. The
 * build and agent surfaces keep their own plan machinery; none of it is shared
 * here. (The mid-run steer + inject-source transport is wired in
 * `useDeepResearchStream` but still has no rendered control — see the note
 * there; it is a missing affordance, not something this surface removed.)
 *
 * Reuses: ActivityFeed (via DeepProgressStrip), Citation cards (via
 * DeepReportView → CitedText), QueryInput, the conversation WS subscription,
 * the Build status state machine.
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
 *   - DeepResearchRunView          — error/starting-loader/progress/bounded
 *   - DeepResearchReportView       — report/sources/follow-ups/need-more/footer
 */

import { useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { useDeepResearch } from "@/hooks/useDeepResearch";
import { publishConversationTitle } from "@/lib/documentTitle";
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
    reportAction,
    requestReportAction,
  } = useDeepResearchSurfaceState({ r, onScopeChange, draft, onDraftChange });

  // The URL has to carry the run.
  //
  // A run started from `/` left the address bar at `/` while the engine worked,
  // so ANY reload — a user's F5, a vite HMR full reload — landed back on the
  // composer with the run still going and reachable only through History. The
  // entry is REPLACED (never pushed: the composer is not a place to go Back to
  // while a run is live) and a reload then re-enters through
  // `ResumeDeepResearch`, which subscribes and replays history-then-live.
  //
  // WAITING FOR THE FIRST EVENT is load-bearing, not caution. Moving on the
  // conversation id alone re-routes the surface while the opening
  // `send_message` is still QUEUED against a socket that has not finished
  // connecting; the unmount closes that socket, the queued frame dies with it,
  // and the run never starts — proven on the isolated stack, where the
  // conversation was created and its log stayed empty. One persisted event is
  // the server's own evidence that the question landed.
  //
  // A run opened FROM `/deep/:cid` is already at its own URL. Recording that
  // too keeps this once per run: a surface the user has deliberately left
  // cannot be pulled back to the run it still holds in memory.
  // UI-28: name the browser tab after the question being researched.
  const query = r.query;
  useEffect(() => {
    publishConversationTitle(query);
    return () => publishConversationTitle(null);
  }, [query]);

  const runCid = r.cid;
  const runHasEvents = r.activity.lastEventAt !== null;
  const urlCarriesRun = useRef<string | null>(null);
  useEffect(() => {
    if (resumeCid) {
      urlCarriesRun.current = resumeCid;
      return;
    }
    if (!runCid || !runHasEvents || urlCarriesRun.current === runCid) return;
    urlCarriesRun.current = runCid;
    navigate(`/deep/${runCid}`, { replace: true });
  }, [resumeCid, runCid, runHasEvents, navigate]);

  // Gap #39 — a stable, assertable DR lifecycle phase. The run itself is
  // model+live-search+streaming (non-deterministic), but the PHASE TRANSITIONS
  // (idle → running → paused/error → done) are deterministic and can be
  // asserted by the harness off this single attribute. v2 dropped the
  // "planning" phase along with the plan gate: submit goes straight to
  // running.
  const drPhase: string =
    r.status === "RUNNING"
      ? "running"
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
            // At the run's own URL the parent that owned the scope is gone, so
            // this leaves for the main surface — carrying the question, which
            // is what the in-place scope flip preserved.
            else navigate("/", { state: { seedQuery: r.query } });
          }}
          handleNewResearch={() => {
            // The run's URL re-seeds the session from `resumeCid`, so clearing
            // the session alone would put the same run straight back and New
            // research would be a button that does nothing.
            handleNewResearch();
            navigate("/", { replace: true });
          }}
          exportCaps={exportCaps}
          doneNotify={doneNotify}
          topBarIncludeOpen={topBarIncludeOpen}
          setTopBarIncludeOpen={setTopBarIncludeOpen}
          handleTopBarExport={handleTopBarExport}
          handleTopBarIncludeConfirm={handleTopBarIncludeConfirm}
          requestReportAction={requestReportAction}
        />
        <DeepResearchRunView r={r} />
        <DeepResearchReportView r={r} reportAction={reportAction} />
      </main>
    </div>
  );
}
