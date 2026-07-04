/**
 * E2E bridge — exposes a non-enumerable `window.__DISCO_E2E__` getter for the
 * evidence harness.  ONLY active in Vite development mode (`import.meta.env.DEV`)
 * or when the `disco_e2e=1` localStorage opt-in is set.  Never exposes secrets,
 * tokens, or full application state — navigational and run-lifecycle metadata only.
 *
 * evidence-harness-campaign.md W6
 */

/** The currently visible surface / route segment. */
export type Surface =
  | "search"
  | "build"
  | "agent"
  | "deep_research"
  | "settings"
  | "history"
  | "projects"
  | "workflows"
  | "activity"
  | "share"
  | "imported"
  | null;

/**
 * The state snapshot returned by `window.__DISCO_E2E__`.
 *
 * - `schemaVersion` — always 1; bump on breaking changes.
 * - `surface`       — the currently visible surface.
 * - `conversationId`— the active conversation id, or null when on a non-conversation route.
 * - `runStatus`     — the live conversation status string (e.g. "RUNNING"), or null.
 * - `route`         — current `window.location.pathname`.
 */
export interface DiscoE2EState {
  schemaVersion: 1;
  surface: Surface;
  conversationId: string | null;
  runStatus: string | null;
  route: string;
}

/**
 * Defines `window.__DISCO_E2E__` as a live (non-enumerable, configurable)
 * getter that calls `getState()` on every access, so the harness always reads
 * the **current** navigational + run state without a stale closure.
 *
 * Activation:
 * - Always active in Vite `DEV` mode (development server).
 * - Also active in production builds when `localStorage.disco_e2e === '1'`.
 * - Inert otherwise — no property is defined on `window`.
 *
 * Safe to call multiple times: idempotent after the first successful install.
 */
export function installE2EBridge(getState: () => DiscoE2EState): void {
  const active =
    import.meta.env.DEV ||
    (typeof localStorage !== "undefined" &&
      localStorage.getItem("disco_e2e") === "1");
  if (!active) return;
  if (typeof window === "undefined") return;
  // Idempotent — don't clobber an already-installed getter.
  if ("__DISCO_E2E__" in window) return;

  Object.defineProperty(window, "__DISCO_E2E__", {
    get: getState,
    configurable: true,
    enumerable: false,
  });
}
