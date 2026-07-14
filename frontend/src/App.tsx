import { lazy, useEffect, useRef } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  Navigate,
  Route,
  BrowserRouter as Router,
  Routes,
  useLocation,
  useNavigate,
  useParams,
} from "react-router-dom";
import { installE2EBridge, type DiscoE2EState, type Surface } from "@/lib/e2eBridge";
import { getPublishedRunStatus } from "@/lib/runStatusBridge";
import { clearFreshMode } from "@/lib/sessionResume";
import { ModeProvider } from "@/shell/ModeProvider";
import { PairingGate } from "@/components/PairingGate";
import { ToastProvider } from "@/components/Toast";
import { Shell } from "@/shell/Shell";
import { useMode } from "@/shell/mode";

const BuildSurface = lazy(() =>
  import("@/components/BuildSurface").then((module) => ({ default: module.BuildSurface })),
);
const AgentSurface = lazy(() =>
  import("@/components/AgentSurface").then((module) => ({ default: module.AgentSurface })),
);
const ResearchSurface = lazy(() =>
  import("@/components/ResearchSurface").then((module) => ({ default: module.ResearchSurface })),
);
const DeepResearchSurface = lazy(() =>
  import("@/components/research/DeepResearchSurface").then((module) => ({
    default: module.DeepResearchSurface,
  })),
);
const ActivityView = lazy(() =>
  import("@/views/ActivityView").then((module) => ({ default: module.ActivityView })),
);
const HistoryView = lazy(() =>
  import("@/views/HistoryView").then((module) => ({ default: module.HistoryView })),
);
const ImportedRunView = lazy(() =>
  import("@/views/ImportedRunView").then((module) => ({ default: module.ImportedRunView })),
);
const ProjectsView = lazy(() =>
  import("@/views/ProjectsView").then((module) => ({ default: module.ProjectsView })),
);
const SettingsView = lazy(() =>
  import("@/views/SettingsView").then((module) => ({ default: module.SettingsView })),
);
const ShareView = lazy(() =>
  import("@/views/ShareView").then((module) => ({ default: module.ShareView })),
);
const SpacesView = lazy(() =>
  import("@/views/SpacesView").then((module) => ({ default: module.SpacesView })),
);
const WorkflowReviewPanel = lazy(() =>
  import("@/views/WorkflowReviewPanel").then((module) => ({
    default: module.WorkflowReviewPanel,
  })),
);

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
});

/**
 * Gap #4: live run-status seam for the W6 E2E bridge.
 *
 * The `E2EBridgeMounter` lives ABOVE the surface components (it's a sibling of
 * `<Routes>`), so it cannot call `useBuild`/`useDeepResearchStream` directly to
 * learn the live `execution_status`. Previously `runStatus` was therefore
 * hardcoded `null`, blocking every status-gated harness assertion.
 *
 * This is a tiny module-level publish seam: the active surface publishes its
 * current conversation status here (and clears it on unmount), and the bridge
 * getter reads it LIVE on every access — so the harness can `await` the run
 * reaching RUNNING / AWAITING_* / FINISHED without a re-render of the mounter.
 *
 * CROSS-FILE DEPENDENCY (other agents own the surfaces): BuildSurface /
 * AgentSurface / DeepResearchSurface must call `publishRunStatus(status)` when
 * their conversation status changes and `publishRunStatus(null)` on unmount.
 * Until they do, the bridge simply reports `null` (no regression — that was the
 * prior hardcoded value).
 */
/**
 * Mounts the `window.__DISCO_E2E__` metadata bridge (W6).  Derives surface +
 * conversation-id from the router location; runStatus is null at the app level
 * (surface components can extend via the shared stateRef if needed later).
 * Renders nothing — purely a side-effect component.
 */
function E2EBridgeMounter() {
  const location = useLocation();
  const { mode } = useMode();
  const pathname = location.pathname;

  // Parse `/build|agent|deep|imported|share/<cid>` from the pathname.
  const cidMatch = pathname.match(
    /^\/(build|agent|deep|imported|share)\/([^/]+)/,
  );
  const routePrefix = cidMatch?.[1] ?? null;
  const conversationId =
    routePrefix === "build" ||
    routePrefix === "agent" ||
    routePrefix === "deep" ||
    routePrefix === "imported"
      ? (cidMatch?.[2] ?? null)
      : null;

  let surface: Surface = null;
  if (routePrefix === "build") surface = "build";
  else if (routePrefix === "agent") surface = "agent";
  else if (routePrefix === "deep") surface = "deep_research";
  else if (routePrefix === "imported") surface = "imported";
  else if (routePrefix === "share") surface = "share";
  else if (pathname === "/activity") surface = "activity";
  else if (pathname === "/history") surface = "history";
  else if (pathname === "/projects") surface = "projects";
  else if (pathname === "/workflows") surface = "workflows";
  else if (pathname === "/spaces") surface = "spaces";
  else if (pathname === "/settings") surface = "settings";
  else if (pathname === "/" || pathname === "")
    surface =
      mode === "build" ? "build" : mode === "agent" ? "agent" : "search";

  // Keep a ref so the getter always returns the latest values without re-installing.
  // `runStatus` is filled in LIVE by the getter (below) from the publish seam, so
  // it reflects status changes even between mounter re-renders.
  const stateRef = useRef<DiscoE2EState>({
    schemaVersion: 1,
    surface,
    conversationId,
    runStatus: null,
    route: pathname,
  });
  stateRef.current = {
    schemaVersion: 1,
    surface,
    conversationId,
    runStatus: getPublishedRunStatus(),
    route: pathname,
  };

  useEffect(() => {
    // Read the published run status LIVE on every bridge access (gap #4) — the
    // surface publishes it via `publishRunStatus`, and the harness reads the
    // current value without waiting on a mounter re-render.
    installE2EBridge(() => ({
      ...stateRef.current,
      runStatus: getPublishedRunStatus(),
    }));
    // installE2EBridge is idempotent; run once on mount. The effect closes over
    // only stable bindings (a ref + module-level getters), so there are no deps.
  }, []);

  return null;
}

/** The main surface follows the active top-level mode (the slider): Search → the
 * grounded research surface; Build → the agent surface (software framing); Agent →
 * the same surface, general-task framing. */
function MainSurface() {
  const { mode } = useMode();
  if (mode === "build") return <BuildSurface />;
  if (mode === "agent") return <AgentSurface />;
  return <ResearchSurface />;
}

/** Resume an existing Build project from /build/:cid — opens the Build surface
 * pinned to a specific conversation id (the project surface's reopen path). */
function ResumeProject() {
  const { cid } = useParams<{ cid: string }>();
  const location = useLocation();
  const navigate = useNavigate();
  // W-24: keep the mode slider in sync with the resumed surface so landing on
  // /build/:cid shows the Build toggle position (not whatever was last active).
  const { setMode } = useMode();
  useEffect(() => {
    setMode("build");
    clearFreshMode("build");
  }, [setMode]);
  // A5: a "Build a deck from this report" handoff navigates here with the serialized
  // report in router state — BuildSurface seeds+kicks it ONCE. A plain resume (no
  // state) just reopens the existing build (view ≠ start).
  //
  // React Router persists navigation state in window.history.state and RESTORES it on
  // reload, so we capture the seed on first render (ref) and then CLEAR the history
  // state — otherwise refreshing /build/:cid would re-kick the same build (duplicate
  // run). After the clear, a reload sees no state → plain resume.
  const seedRef = useRef<string | null>(
    (location.state as { seedTask?: string } | null)?.seedTask ?? null,
  );
  // R3: the full report rides along as hidden seedContext (an ENVIRONMENT message),
  // captured here the same way so the visible handoff message stays a one-liner.
  const seedContextRef = useRef<string | null>(
    (location.state as { seedContext?: string } | null)?.seedContext ?? null,
  );
  useEffect(() => {
    if ((location.state as { seedTask?: string } | null)?.seedTask) {
      navigate(location.pathname, { replace: true, state: null });
    }
    // run once on mount — the seed is already captured in seedRef
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return (
    <BuildSurface
      resumeCid={cid ?? null}
      seedTask={seedRef.current}
      seedContext={seedContextRef.current}
    />
  );
}

/** Resume an existing Agent task from /agent/:cid — the same machinery as
 * ResumeProject, agent framing. The stored surface stays "agent"; resume never
 * re-sets it (the WS just replays history-then-live).
 *
 * runthru-v2 #7: the "Build a deck" report→slides handoff now targets the AGENT
 * surface (task framing, not software-dev), so ResumeAgent must capture the
 * seedTask/seedContext from router state + seed-kick ONCE, exactly like
 * ResumeProject — otherwise it landed here without ever starting the deck job. */
function ResumeAgent() {
  const { cid } = useParams<{ cid: string }>();
  const location = useLocation();
  const navigate = useNavigate();
  // W-24: sync the 3-way mode slider to Agent — the "Build Slides" handoff (and a
  // direct /agent/:cid open or reload) lands here, and the slider must reflect it
  // rather than staying on Search/Build.
  const { setMode } = useMode();
  useEffect(() => {
    setMode("agent");
    clearFreshMode("agent");
  }, [setMode]);
  const seedRef = useRef<string | null>(
    (location.state as { seedTask?: string } | null)?.seedTask ?? null,
  );
  const seedContextRef = useRef<string | null>(
    (location.state as { seedContext?: string } | null)?.seedContext ?? null,
  );
  useEffect(() => {
    if ((location.state as { seedTask?: string } | null)?.seedTask) {
      navigate(location.pathname, { replace: true, state: null });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return (
    <AgentSurface
      resumeCid={cid ?? null}
      seedTask={seedRef.current}
      seedContext={seedContextRef.current}
    />
  );
}

/** Resume an existing Deep Research run from /deep/:cid — opens the Deep
 * Research surface pinned to a specific conversation id. The WS subscription
 * replays history-then-live, so all derived state (plan, progress, report)
 * recovers naturally. */
function ResumeDeepResearch() {
  const { cid } = useParams<{ cid: string }>();
  // W-24: Deep Research is a child scope of Search → sync the slider to Search so
  // a resumed /deep/:cid reflects the Search toggle position.
  const { setMode } = useMode();
  useEffect(() => setMode("search"), [setMode]);
  return <DeepResearchSurface resumeCid={cid ?? null} />;
}

/**
 * App composition root: server-state provider (TanStack Query) → router → mode
 * context → the shell, which frames the routed views. The main surface (/) follows the
 * mode slider (Search vs Build); /history and /settings are placeholders until their
 * prompts land. Unknown routes fall back to the main surface.
 */
export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Router>
        <ModeProvider>
          <ToastProvider>
          {/* First-run auth gate: bootstraps a session and prompts for the pairing
              token when the deployment needs it (remote/containerized self-host).
              Transparent (renders children immediately) in fixture/demo mode. */}
          <PairingGate>
          {/* W6: evidence-harness bridge — inert in production without opt-in */}
          <E2EBridgeMounter />
          <Routes>
            <Route element={<Shell />}>
              <Route index element={<MainSurface />} />
              <Route path="activity" element={<ActivityView />} />
              <Route path="history" element={<HistoryView />} />
              <Route path="projects" element={<ProjectsView />} />
              <Route path="workflows" element={<WorkflowReviewPanel />} />
              <Route path="spaces" element={<SpacesView />} />
              <Route path="build/:cid" element={<ResumeProject />} />
              <Route path="agent/:cid" element={<ResumeAgent />} />
              <Route path="imported/:cid" element={<ImportedRunView />} />
              <Route path="deep/:cid" element={<ResumeDeepResearch />} />
              <Route path="share/:token" element={<ShareView />} />
              <Route path="settings" element={<SettingsView />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
          </PairingGate>
          </ToastProvider>
        </ModeProvider>
      </Router>
    </QueryClientProvider>
  );
}
