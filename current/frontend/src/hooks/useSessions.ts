/**
 * Live tmux session polling for the Terminal tab (BP-14).
 *
 * useSessions(cid, active, visible):
 *   - polls /sessions every 2s while active (so the tab badge works regardless of
 *     which tab is shown)
 *   - polls the selected session's /view every 1s only when the tab is visible
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getSessions, getSessionView } from "@/api/agent";
import type { SessionInfo, SessionView } from "@/api/agent";

export type { SessionInfo, SessionView };

export function useSessions(cid: string | null, active: boolean, visible: boolean) {
  const [selectedName, setSelectedName] = useState<string | null>(null);

  const sessionsEnabled = !!cid && active;
  const sessionsQuery = useQuery({
    queryKey: ["sessions", cid],
    queryFn: () => getSessions(cid!),
    enabled: sessionsEnabled,
    refetchInterval: sessionsEnabled ? 2000 : false,
  });

  const sessions: SessionInfo[] = sessionsQuery.data?.sessions ?? [];

  // Auto-select the first session when the list arrives and nothing is selected.
  // Keep the selection if the session still exists; drop it if it's gone.
  const resolvedName =
    selectedName && sessions.some((s) => s.name === selectedName)
      ? selectedName
      : (sessions[0]?.name ?? null);

  const viewEnabled = sessionsEnabled && visible && resolvedName !== null;
  const viewQuery = useQuery({
    queryKey: ["session-view", cid, resolvedName],
    queryFn: () => getSessionView(cid!, resolvedName!),
    enabled: viewEnabled,
    refetchInterval: viewEnabled ? 1000 : false,
  });

  return {
    sessions,
    selectedName: resolvedName,
    setSelectedName,
    view: viewQuery.data ?? null,
  };
}
