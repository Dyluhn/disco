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

/**
 * [W-22] The container backends whose sandbox image ships LibreOffice — the only
 * ones that can render a deck → PDF (the `process` dev backend runs on the host with
 * no such image). Must match the server's `_PDF_CAPABLE_BACKENDS` so the hidden PDF
 * button and the route's 409 agree (no false affordance).
 */
const PDF_CAPABLE_BACKENDS = new Set(["gvisor", "local", "podman"]);

/** True iff the active sandbox backend can render a themed deck → PDF (W-22). */
export function deckPdfCapableBackend(backend: string | null | undefined): boolean {
  return !!backend && PDF_CAPABLE_BACKENDS.has(backend);
}
