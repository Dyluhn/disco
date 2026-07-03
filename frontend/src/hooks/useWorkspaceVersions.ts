import { useEffect, useMemo, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { listWorkspaceVersions, type WorkspaceVersion } from "@/api/agent";
import type { AgentEvent, ConversationStatus } from "@/types/agent";

const TERMINAL_STATUSES = new Set<ConversationStatus>(["FINISHED", "STUCK", "ERROR", "IDLE"]);

function latestWorkspaceRestoreKey(events: AgentEvent[]): string | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.kind === "workspace_restored") return `${e.id}:${e.version_seq}`;
  }
  return null;
}

function latestTerminalStatusKey(
  events: AgentEvent[],
  status?: ConversationStatus,
): string | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.kind === "status" && TERMINAL_STATUSES.has(e.status)) {
      return `${e.id}:${e.status}`;
    }
  }
  return status && TERMINAL_STATUSES.has(status) ? `state:${status}` : null;
}

export function useWorkspaceVersions(
  conversationId: string | null,
  events: AgentEvent[] = [],
  status?: ConversationStatus,
): { versions: WorkspaceVersion[]; loading: boolean; refetch: () => Promise<unknown> } {
  const query = useQuery<WorkspaceVersion[]>({
    queryKey: ["workspace-versions", conversationId],
    queryFn: () => listWorkspaceVersions(conversationId!),
    enabled: !!conversationId,
  });

  const restoreKey = useMemo(() => latestWorkspaceRestoreKey(events), [events]);
  const terminalKey = useMemo(
    () => latestTerminalStatusKey(events, status),
    [events, status],
  );
  const previous = useRef<{ cid: string | null; restore: string | null; terminal: string | null }>({
    cid: conversationId,
    restore: restoreKey,
    terminal: terminalKey,
  });

  useEffect(() => {
    if (!conversationId) {
      previous.current = { cid: conversationId, restore: restoreKey, terminal: terminalKey };
      return;
    }
    const prev = previous.current;
    const cidChanged = prev.cid !== conversationId;
    const restoreChanged = restoreKey !== null && restoreKey !== prev.restore;
    const terminalChanged = terminalKey !== null && terminalKey !== prev.terminal;
    previous.current = { cid: conversationId, restore: restoreKey, terminal: terminalKey };
    if (!cidChanged && (restoreChanged || terminalChanged)) {
      void query.refetch();
    }
  }, [conversationId, query, restoreKey, terminalKey]);

  return {
    versions: query.data ?? [],
    loading: query.isLoading,
    refetch: query.refetch,
  };
}
