import type { AgentEvent } from "@/types/agent";

/** A server-persisted terminal followed by its matching final workspace seal. */
export function finishedWorkspaceEvents(cid: string, terminalSeq = 10, versionSeq = 1, latestEffectSeq: number | null = null): AgentEvent[] {
  return [{
    id: `finished-${terminalSeq}`, kind: "status", source: "system", seq: terminalSeq, status: "FINISHED",
  }, {
    id: `sealed-${versionSeq}`, kind: "workspace_version", source: "system", seq: terminalSeq + 1,
    version_seq: versionSeq, tree_digest: "a".repeat(64), trigger: "finish",
    final_seal: {
      schema_version: 1, scope: { namespace: "workspace.tree", identifier: cid },
      terminal_seq: terminalSeq, latest_effect_seq: latestEffectSeq, version_seq: versionSeq,
      tree_digest: "a".repeat(64), file_count: 1, total_bytes: 20,
    },
  }];
}
