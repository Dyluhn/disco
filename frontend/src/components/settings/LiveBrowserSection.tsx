/**
 * Settings → Agent → Live browser (noVNC).
 * A simple off/on toggle. When on, a "Live" button appears on the Agent canvas
 * browser pane and clicking it opens an iframe to the noVNC view of the sandbox.
 *
 * Security notes (per docs/design-novnc-live-browser.md):
 *  - VNC is loopback-bound inside the sandbox (127.0.0.1 only, never network).
 *  - The noVNC endpoint goes through the existing server-minted per-conversation
 *    preview origin (localhost or the configured wildcard site) and auth/jail.
 *  - View-only by default (view_only=1 noVNC param); no input injection.
 *  - gVisor backend requires D7 egress allowlist update — deferred.
 *
 * P5 live jail acceptance is HARDWARE-DEFERRED (sandbox VM destroyed).
 */

import { useState } from "react";
import { Monitor, MonitorOff } from "lucide-react";
import { cn } from "@/lib/cn";
import {
  useLiveBrowserConfig,
  useUpdateLiveBrowserConfig,
  useSandboxConfig,
} from "@/hooks/useModels";
import { backendSupportsLiveView, backendMeta } from "@/types/sandbox";

export function LiveBrowserSection() {
  const { data, isLoading } = useLiveBrowserConfig();
  const { data: sandbox } = useSandboxConfig();
  const save = useUpdateLiveBrowserConfig();
  // Client-side flag when the user clicks "On" while the backend can't run noVNC —
  // a visible message, never a silent no-op (and we never call save in that case).
  const [blocked, setBlocked] = useState(false);

  // The live stack (Xvfb/x11vnc/websockify) only runs on gVisor — mirror the server's
  // LIVE_VIEW_BACKENDS. On any other backend the toggle is greyed and enabling is refused.
  const backendId = sandbox?.backend;
  const liveSupported = backendSupportsLiveView(backendId);
  const backendName = (backendId && backendMeta(backendId)?.name) || "current";
  // Effective state shown: even a stale persisted enabled=true reads OFF on an
  // unsupported backend, so the setting is never ambiguous about what's actually live.
  const effectiveEnabled = liveSupported && (data?.enabled ?? false);

  return (
    <section className="flex flex-col gap-inline">
      <header>
        <h3 className="font-ui text-[0.95rem] font-semibold text-text">
          Live browser
        </h3>
        <p className="mt-hair font-ui text-[0.86rem] text-text-muted">
          Stream the agent's headed browser live via noVNC. When enabled, the
          Agent canvas auto-streams the live view while the agent is browsing
          (no button — it starts when the browser is up and ends when it ends).
          The VNC stack starts on demand — idle sessions cost nothing. View-only
          by default; loopback-bound inside the sandbox.
        </p>
        <p className="mt-hair font-ui text-[0.78rem] text-text-faint">
          Requires a containerized gVisor sandbox (the image with
          Xvfb/x11vnc/noVNC/websockify and the accepted live-jail security
          model). It is not available on the local or Podman sandbox — switch
          the sandbox backend to gVisor to use it.
        </p>
      </header>

      {isLoading || !data ? (
        <p className="font-ui text-[0.86rem] text-text-faint">Loading…</p>
      ) : (
        <div className="flex flex-col gap-inline">
          {/* Backend can't run noVNC → an honest, always-visible explanation; the "On"
              choice below is greyed + locked off (no false affordance). */}
          {!liveSupported && (
            <p
              data-live-browser-unsupported="true"
              data-backend={backendId}
              className="flex items-start gap-hair rounded-control border border-weak/50 px-body py-inline font-ui text-[0.82rem] leading-snug text-text-muted"
            >
              <MonitorOff
                className="mt-px size-4 shrink-0 text-text-faint"
                aria-hidden
              />
              <span>
                Live browser requires a containerized sandbox (gVisor) — it
                isn't available on the {backendName} sandbox. The toggle is
                locked off; the Agent canvas shows the per-action screenshot
                reel as usual.
              </span>
            </p>
          )}
          {(
            [
              {
                enabled: false,
                Icon: MonitorOff,
                label: "Off (default)",
                help: "No live view. The browser tab shows the per-action screenshot reel — lightweight, no VNC overhead.",
              },
              {
                enabled: true,
                Icon: Monitor,
                label: "On",
                help: "While the agent is browsing, the Agent canvas auto-streams the noVNC live view (and shows a green “Live” light) — no button. The Xvfb/x11vnc/websockify stack starts lazily.",
              },
            ] as const
          ).map(({ enabled, Icon, label, help }) => {
            const isActive = effectiveEnabled === enabled;
            // The "On" choice is locked when the backend can't run the stack. We keep the
            // button clickable (not natively disabled) so a click FLAGS the reason instead
            // of being a silent no-op — but it never calls save.
            const locked = !liveSupported && enabled === true;
            return (
              <button
                key={String(enabled)}
                type="button"
                data-disco-control="settings.livebrowser-toggle"
                data-enabled={enabled}
                data-locked={locked || undefined}
                onClick={() => {
                  if (locked) {
                    setBlocked(true);
                    return;
                  }
                  setBlocked(false);
                  if (!isActive) save.mutate({ enabled });
                }}
                disabled={save.isPending}
                aria-disabled={locked || undefined}
                aria-pressed={isActive}
                className={cn(
                  "flex items-start gap-inline rounded-card border px-body py-inline text-left transition-colors",
                  isActive
                    ? "border-accent/50 bg-accent/5"
                    : "border-hairline hover:border-hairline-strong",
                  save.isPending && "opacity-60",
                  locked && "cursor-not-allowed opacity-50",
                )}
              >
                <Icon
                  className={cn(
                    "mt-px size-4 shrink-0",
                    isActive ? "text-accent" : "text-text-faint",
                  )}
                  aria-hidden
                />
                <span className="flex min-w-0 flex-col gap-hair">
                  <span className="font-ui text-[0.9rem] font-medium text-text">
                    {label}
                  </span>
                  <span className="font-ui text-[0.8rem] leading-relaxed text-text-faint">
                    {help}
                  </span>
                </span>
              </button>
            );
          })}
          {/* Flagged error when the user clicks "On" on a backend that can't run noVNC —
              visible, never a silent no-op; the save call is suppressed. */}
          {blocked && !liveSupported && (
            <p
              role="alert"
              data-live-browser-blocked="true"
              className="font-ui text-[0.8rem] text-warn"
            >
              Live browser can't be turned on for the {backendName} sandbox —
              switch the sandbox backend to gVisor first.
            </p>
          )}
          {liveSupported && (
            <p
              data-live-browser-availability="runtime-checked"
              className="font-ui text-[0.76rem] text-text-faint"
            >
              Enabling this saves a preference only — the stream auto-starts on
              the Agent canvas at runtime when the agent is actually browsing
              (and the gVisor stack is up).
            </p>
          )}
          {save.error && (
            <p className="font-ui text-[0.8rem] text-warn">
              Couldn't save: {(save.error as Error).message}
            </p>
          )}
        </div>
      )}
    </section>
  );
}
