import type { AgentEvent, StatusEvent, WorkspaceVersionEvent } from "@/types/agent";

/** Presentation readiness only; server ownership and immutable-record validation
 * remain authoritative. FINISHED precedes capture, and an older seal cannot
 * make a later run's unfinished capture downloadable. */
export function currentFinalWorkspaceEvent(events: AgentEvent[], cid: string | null) {
  if (!cid) return undefined;
  let terminal: StatusEvent | undefined;
  let version: WorkspaceVersionEvent | undefined;
  let latestEffect: number | null = null;
  const effects = new Set(["action", "observation", "agent_error", "workspace_mutation"]);
  for (const event of events) {
    if (event.kind !== "status" && event.kind !== "workspace_version" && !effects.has(event.kind)) continue;
    const seq = event.seq;
    if (typeof seq !== "number" || !Number.isInteger(seq) || seq < 1) return undefined;
    if (event.kind === "status") {
      if (!terminal || seq > terminal.seq!) terminal = event;
    } else if (event.kind === "workspace_version") {
      if (!version || seq > version.seq!) version = event;
    } else {
      latestEffect = Math.max(latestEffect ?? 0, seq);
    }
  }
  const seal = version?.final_seal;
  if (!terminal || terminal.status !== "FINISHED" || terminal.source !== "system" ||
      !version || version.source !== "system" || version.trigger !== "finish" || !seal ||
      version.seq! <= terminal.seq! || (latestEffect !== null && latestEffect >= terminal.seq!) ||
      seal.schema_version !== 1 || seal.terminal_seq !== terminal.seq ||
      seal.latest_effect_seq !== latestEffect || seal.scope.namespace !== "workspace.tree" ||
      seal.scope.identifier !== cid || seal.version_seq !== version.version_seq ||
      seal.tree_digest !== version.tree_digest) return undefined;
  return version;
}
