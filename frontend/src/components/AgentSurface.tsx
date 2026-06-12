/**
 * The Agent surface — the general-task framing of the agent. It is the SAME
 * machinery as Build (loop + tools + sandbox + plan/confirm gates + workspace
 * persistence); only the framing copy and the entry differ. Rather than fork
 * BuildSurface's 400 lines (and every future gate/steer/replay fix with it), this
 * is a thin wrapper that renders BuildSurface with `framing="agent"`. The backend
 * surface is "agent" (a build-like surface), so it snapshots/rehydrates and lists
 * in Projects exactly like Build.
 */

import { BuildSurface } from "@/components/BuildSurface";

export function AgentSurface({ resumeCid }: { resumeCid?: string | null } = {}) {
  return <BuildSurface resumeCid={resumeCid} framing="agent" />;
}
