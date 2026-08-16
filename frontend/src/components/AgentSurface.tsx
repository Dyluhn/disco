/**
 * The Agent surface — the general-task framing over the shared execution,
 * sandbox, gate, and workspace machinery. Agent-specific prompts and finish/
 * admission policy live at the backend boundary. Rather than fork
 * BuildSurface's 400 lines (and every future gate/steer/replay fix with it), this
 * is a thin wrapper that renders BuildSurface with `framing="agent"`. The backend
 * surface is "agent", so it snapshots/rehydrates and lists
 * in Projects exactly like Build.
 */

import { BuildSurface } from "@/components/BuildSurface";

export function AgentSurface({
  resumeCid,
  seedTask,
  seedContext,
}: {
  resumeCid?: string | null;
  // runthru-v2 #7: the report→slides handoff seeds the AGENT surface; forward the
  // seed task/context to BuildSurface so the deck job actually starts.
  seedTask?: string | null;
  seedContext?: string | null;
} = {}) {
  return (
    <BuildSurface
      resumeCid={resumeCid}
      framing="agent"
      seedTask={seedTask}
      seedContext={seedContext}
    />
  );
}
