import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Navigate, Route, BrowserRouter as Router, Routes } from "react-router-dom";
import { ResearchSurface } from "@/components/ResearchSurface";
import { ModeProvider } from "@/shell/ModeProvider";
import { Shell } from "@/shell/Shell";
import { HistoryView } from "@/views/HistoryView";
import { SettingsView } from "@/views/SettingsView";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
});

/**
 * App composition root: server-state provider (TanStack Query) → router → mode
 * context → the shell, which frames the routed views. The main surface (/) is the
 * research surface; /history and /settings are placeholders until their prompts
 * land. Unknown routes (incl. the dormant /spaces) fall back to the main surface.
 */
export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Router>
        <ModeProvider>
          <Routes>
            <Route element={<Shell />}>
              <Route index element={<ResearchSurface />} />
              <Route path="history" element={<HistoryView />} />
              <Route path="settings" element={<SettingsView />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
        </ModeProvider>
      </Router>
    </QueryClientProvider>
  );
}
