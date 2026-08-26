import type { AgentEvent, ConversationStatus, StatusEvent, WorkspaceVersionEvent } from "@/types/agent";

/** The immutable identity of one finished workspace commit.  A FINISHED status
 * is deliberately not enough: the status is written before the workspace is
 * copied into project storage, while this seal is written after that copy. */
export interface CommittedFinish {
  terminalSeq: number;
  versionSeq: number;
  treeDigest: string;
  status: StatusEvent;
  version: WorkspaceVersionEvent;
  seal: NonNullable<WorkspaceVersionEvent["final_seal"]>;
}

function seq(event: AgentEvent): number {
  return typeof event.seq === "number" ? event.seq : -1;
}

/**
 * Select the current committed FINISHED iteration from a replayable event log.
 *
 * The selector is pure and intentionally strict. It accepts only a FINISHED
 * StatusEvent with a finish-triggered WorkspaceVersionEvent whose final seal
 * names that exact terminal sequence and the exact version/digest on the
 * version event. Replay can therefore arrive in either order without exposing
 * a half-committed release. A later FINISHED event naturally supersedes an old
 * seal; a later non-FINISHED status makes the result unavailable.
 */
export function selectCommittedFinish(
  events: AgentEvent[],
  currentStatus?: ConversationStatus,
): CommittedFinish | null {
  if (currentStatus !== undefined && currentStatus !== "FINISHED") return null;

  const statuses = events
    .filter((event): event is StatusEvent => event.kind === "status" && event.status === "FINISHED")
    .sort((a, b) => seq(b) - seq(a));
  const status = statuses[0];
  if (!status || typeof status.seq !== "number") return null;

  const version = events
    .filter((event): event is WorkspaceVersionEvent => event.kind === "workspace_version")
    .filter((event) => {
      const seal = event.final_seal;
      return (
        event.trigger === "finish" &&
        seal !== null &&
        seal !== undefined &&
        seal.terminal_seq === status.seq &&
        seal.version_seq === event.version_seq &&
        seal.tree_digest === event.tree_digest
      );
    })
    .sort((a, b) => seq(b) - seq(a))[0];
  if (!version?.final_seal) return null;

  return {
    terminalSeq: status.seq,
    versionSeq: version.version_seq,
    treeDigest: version.tree_digest,
    status,
    version,
    seal: version.final_seal,
  };
}

export function releaseSealKey(
  conversationId: string | null,
  finish: CommittedFinish | null,
): readonly unknown[] {
  return [
    "project-release",
    conversationId,
    finish?.terminalSeq ?? null,
    finish?.versionSeq ?? null,
    finish?.treeDigest ?? null,
  ] as const;
}
