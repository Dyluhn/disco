/**
 * Settings → Agent → Live browser (noVNC).
 * A simple off/on toggle. When on, a "Live" button appears on the Agent canvas
 * browser pane and clicking it opens an iframe to the noVNC view of the sandbox.
 *
 * Security notes (per docs/design-novnc-live-browser.md):
 *  - VNC is loopback-bound inside the sandbox (127.0.0.1 only, never network).
 *  - The noVNC endpoint goes through the existing per-conversation preview proxy
 *    ({cid8}-6080.localhost) — same auth/jail as the dev-server preview.
 *  - View-only by default (view_only=1 noVNC param); no input injection.
 *  - gVisor backend requires D7 egress allowlist update — deferred.
 *
 * P5 live jail acceptance is HARDWARE-DEFERRED (sandbox VM destroyed).
 */

import { Monitor, MonitorOff } from "lucide-react";
import { cn } from "@/lib/cn";
import { useLiveBrowserConfig, useUpdateLiveBrowserConfig } from "@/hooks/useModels";

export function LiveBrowserSection() {
  const { data, isLoading } = useLiveBrowserConfig();
  const save = useUpdateLiveBrowserConfig();

  return (
    <section className="flex flex-col gap-section border-t border-hairline pt-section">
      <header>
        <h2 className="font-display text-[1.3rem] tracking-tight text-text">Live browser</h2>
        <p className="mt-hair font-ui text-[0.86rem] text-text-muted">
          Stream the agent's headed browser live via noVNC. A "Live" toggle appears on the Agent
          canvas browser pane when enabled. The VNC stack starts on demand — idle sessions cost
          nothing. View-only by default; loopback-bound inside the sandbox.
        </p>
        <p className="mt-hair font-ui text-[0.78rem] text-text-faint">
          Requires the sandbox image with Xvfb/x11vnc/noVNC/websockify (P1). gVisor backend needs
          D7 egress allowlist update (deferred). Use local or podman backend.
        </p>
      </header>

      {isLoading || !data ? (
        <p className="font-ui text-[0.86rem] text-text-faint">Loading…</p>
      ) : (
        <div className="flex flex-col gap-inline">
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
                help: 'A "Live" toggle appears on the Agent canvas browser pane. Clicking it opens the noVNC iframe. The Xvfb/x11vnc/websockify stack starts lazily on first open.',
              },
            ] as const
          ).map(({ enabled, Icon, label, help }) => {
            const isActive = data.enabled === enabled;
            return (
              <button
                key={String(enabled)}
                type="button"
                onClick={() => {
                  if (!isActive) save.mutate({ enabled });
                }}
                disabled={save.isPending}
                aria-pressed={isActive}
                className={cn(
                  "flex items-start gap-inline rounded-card border px-body py-inline text-left transition-colors",
                  isActive
                    ? "border-accent/50 bg-accent/5"
                    : "border-hairline hover:border-hairline-strong",
                  save.isPending && "opacity-60",
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
                  <span className="font-ui text-[0.9rem] font-medium text-text">{label}</span>
                  <span className="font-ui text-[0.8rem] leading-relaxed text-text-faint">
                    {help}
                  </span>
                </span>
              </button>
            );
          })}
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
