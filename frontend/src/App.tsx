import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Navigate, Route, BrowserRouter as Router, Routes, useParams } from "react-router-dom";
import { BuildSurface } from "@/components/BuildSurface";
import { ResearchSurface } from "@/components/ResearchSurface";
import { DeepResearchSurface } from "@/components/research/DeepResearchSurface";
import { ModeProvider } from "@/shell/ModeProvider";
import { ToastProvider } from "@/components/Toast";
import { Shell } from "@/shell/Shell";
import { useMode } from "@/shell/mode";
import { HistoryView } from "@/views/HistoryView";
import { ProjectsView } from "@/views/ProjectsView";
import { SettingsView } from "@/views/SettingsView";
import { ShareView } from "@/views/ShareView";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
});

/** The main surface follows the active top-level mode (the slider): Search → the
 * grounded research surface; Build → the live agent surface. */
function MainSurface() {
  const { mode } = useMode();
  return mode === "build" ? <BuildSurface /> : <ResearchSurface />;
}

/** Resume an existing Build project from /build/:cid — opens the Build surface
 * pinned to a specific conversation id (the project surface's reopen path). */
function ResumeProject() {
  const { cid } = useParams<{ cid: string }>();
  return <BuildSurface resumeCid={cid ?? null} />;
}

/** Resume an existing Deep Research run from /deep/:cid — opens the Deep
 * Research surface pinned to a specific conversation id. The WS subscription
 * replays history-then-live, so all derived state (plan, progress, report)
 * recovers naturally. */
function ResumeDeepResearch() {
  const { cid } = useParams<{ cid: string }>();
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
          <Routes>
            <Route element={<Shell />}>
              <Route index element={<MainSurface />} />
              <Route path="history" element={<HistoryView />} />
              <Route path="projects" element={<ProjectsView />} />
              <Route path="build/:cid" element={<ResumeProject />} />
              <Route path="deep/:cid" element={<ResumeDeepResearch />} />
              <Route path="share/:token" element={<ShareView />} />
              <Route path="settings" element={<SettingsView />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
          </ToastProvider>
        </ModeProvider>
      </Router>
    </QueryClientProvider>
  );
}
