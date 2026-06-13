import { isDemoMode } from "@/api/client";

/**
 * Persistent, non-dismissable banner shown whenever neither backend is
 * configured — i.e. /env.js was absent, failed to load, or yielded no URLs
 * (window.__PMX_ENV empty + no VITE_API_BASE / VITE_AGENT_BASE).
 *
 * The banner is intentionally inert: it carries no close button so the user
 * cannot accidentally hide a signal that the data they see is canned fixtures.
 * It is invisible when a backend is successfully wired (isDemoMode() === false).
 */
export function DemoDataBadge() {
  if (!isDemoMode()) return null;
  return (
    <div
      role="status"
      aria-label="Demo data — no backend connected"
      className="shrink-0 border-b border-hairline bg-warn/10 px-body py-1 text-center text-xs text-warn"
    >
      Demo data — no backend connected
    </div>
  );
}
