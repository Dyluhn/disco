/** Saved-workspace availability is established by server events or owner-scoped
 * project metadata, never by merely reopening a conversation route. */
import { useQuery } from "@tanstack/react-query";
import { listProjects } from "@/api/projects";
import { currentFinalWorkspaceEvent } from "@/lib/savedWorkspace";
import type { AgentEvent, ConversationStatus } from "@/types/agent";

function importedWorkspaceEvent(events: AgentEvent[], untrusted: boolean) {
  if (untrusted) return undefined;
  return events.find((event) => event.kind === "message" && event.source === "environment" &&
    (event.meta?.project_import as { workspace_ready?: unknown } | undefined)?.workspace_ready === true);
}

// Older project imports predate the ready event. A successful owner-scoped
// listing distinguishes their persisted workspace from an empty new run without
// probing a missing manifest. This is a read-only presentation check.
function usePersistedProject(cid: string | null, status: ConversationStatus, untrusted: boolean, known: boolean) {
  const inactive = status === "IDLE" || status === "PAUSED" || status === "ERROR";
  const query = useQuery({
    queryKey: ["workspace-existence", cid],
    queryFn: listProjects,
    enabled: Boolean(cid) && inactive && !untrusted && !known,
    retry: false,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });
  return !untrusted
    ? query.data?.projects.find((project) => project.id === cid && !project.files_missing)
    : undefined;
}

export function useSavedWorkspaceAvailability(events: AgentEvent[], status: ConversationStatus, cid: string | null, untrusted = false) {
  const importedWorkspace = importedWorkspaceEvent(events, untrusted);
  const savedVersion = !untrusted && events.some((event) => event.kind === "workspace_version");
  const finalWorkspace = !untrusted && status === "FINISHED" ? currentFinalWorkspaceEvent(events, cid) : undefined;
  const persistedProject = usePersistedProject(cid, status, untrusted, Boolean(importedWorkspace) || savedVersion);
  const hasAvailableWorkspace = !untrusted && (status === "FINISHED"
    ? Boolean(finalWorkspace)
    : Boolean(importedWorkspace || persistedProject || savedVersion));
  return { importedWorkspace, persistedProject, finalWorkspace, hasAvailableWorkspace };
}
