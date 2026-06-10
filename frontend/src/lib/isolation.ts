/**
 * Maps the server-reported sandbox backend name to the IsolationInfo the UI
 * shows on the AgentStatusBar tier pill.
 *
 * One source of truth — no client-side guessing. The wire value arrives in the
 * WS/HTTP state frame; until it does, callers receive null (renders "…").
 */

import type { IsolationInfo } from "@/types/agent";

const BACKEND_MAP: Record<string, IsolationInfo> = {
  gvisor: {
    tier: "gvisor",
    label: "gVisor (user-space kernel) — strong isolation",
    adversarialSafe: true,
  },
  podman: {
    tier: "container",
    label: "Rootless container — container-grade isolation",
    adversarialSafe: false,
  },
  local: {
    tier: "local container",
    label: "Container-grade isolation (shared host kernel)",
    adversarialSafe: false,
  },
  process: {
    tier: "⚠ host process",
    label: "⚠ No isolation — host process (dev mode)",
    adversarialSafe: false,
  },
};

/**
 * Return the IsolationInfo for the given backend name, or null when the
 * backend is unknown / not yet received (renders "…" in the status bar).
 */
export function isolationForBackend(backend: string | null | undefined): IsolationInfo | null {
  if (!backend) return null;
  return BACKEND_MAP[backend] ?? null;
}
