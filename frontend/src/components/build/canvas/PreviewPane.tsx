import { useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ExternalLink, MonitorPlay, RotateCw } from "lucide-react";
import { cn } from "@/lib/cn";
import { deriveFiles, deriveSrcDoc } from "@/lib/buildTrace";
import { agentHttpBase, previewHostUrl } from "@/api/client";
import { restartPreview } from "@/api/agent";
import { useBuildPreview } from "@/hooks/useBuildPreview";
import type { AgentEvent, ConversationStatus } from "@/types/agent";

/** E2 — detect a Vite/bundler entry HTML by the module-script src it references.
 *  The srcdoc iframe can't load `/src/...` or `/@vite/...` (those are dev-server
 *  virtual modules), so a bundler entry is UNRUNNABLE as a srcdoc preview. The
 *  PreviewPane defaults to the live server in that case (when one is available). */
function isBundlerEntryHtml(html: string): boolean {
  return /<script[^>]*\bsrc=["'](?:\/src\/|\/@vite\/)/i.test(html);
}

/** One phase-aware pane (replacing the redundant Preview + Live tabs). When the agent's
 * dev server is reachable (via the ACTIVE backend's port exposure — local direct, gVisor
 * over the tailnet), this IS the live interactive preview (an iframe of the real running
 * artifact). Until then it shows the phase-aware state + the honest reason. */
export function PreviewPane({
  status,
  cid,
  events,
  untrusted = false,
}: {
  status: ConversationStatus;
  cid: string | null;
  events: AgentEvent[];
  /** The events are UNTRUSTED third-party content (a shared/imported run). Drop
   * `allow-scripts` from the static-preview iframe — with open-CORS no-auth APIs,
   * a script in that frame could fetch this instance's endpoints. */
  untrusted?: boolean;
}) {
  // Keep the backend preview active through FINISHED/STUCK too (UI 2.2): the
  // pane shouldn't go MORE dead at the moment of completion.
  const active =
    status === "RUNNING" ||
    status === "WAITING_FOR_CONFIRMATION" ||
    status === "FINISHED" ||
    status === "STUCK";
  const { data } = useBuildPreview(cid, active);
  const qc = useQueryClient();

  // A client-side srcdoc render of the written files — shows the ACTUAL site the
  // agent wrote (entry HTML + inlined local css/js), no backend / dev server.
  const srcDoc = useMemo(() => deriveSrcDoc(deriveFiles(events)), [events]);
  const proxyAvailable = Boolean(data?.available && cid);

  // E7 — one-click recovery. Reload the iframe (bump the key); if the live proxy
  // is NOT reachable, also ask the backend to restart the static serve, then
  // re-poll availability. So a hung/down preview is always user-fixable here.
  const [reloadKey, setReloadKey] = useState(0);
  const [previewPort, setPreviewPort] = useState(8000);
  const boundPorts = (data?.ports ?? [])
    .filter((p) => p.owner != null)
    .map((p) => p.port);
  // a selected extra port that died falls back to the primary
  useEffect(() => {
    if (previewPort !== 8000 && !boundPorts.includes(previewPort)) {
      setPreviewPort(8000);
    }
  }, [boundPorts, previewPort]);

  const [restarting, setRestarting] = useState(false);
  async function refresh() {
    setReloadKey((k) => k + 1);
    if (cid && !proxyAvailable) {
      setRestarting(true);
      try {
        await restartPreview(cid);
        await qc.invalidateQueries({ queryKey: ["build-preview", cid] });
      } finally {
        setRestarting(false);
      }
    }
  }
  const RefreshButton = (
    <button
      type="button"
      onClick={refresh}
      disabled={restarting}
      title="Reload the preview — and restart the server if it's down"
      className="flex items-center gap-hair font-ui text-[0.74rem] text-text-muted transition-colors hover:text-text disabled:opacity-50"
    >
      <RotateCw className={cn("size-3", restarting && "animate-spin")} aria-hidden />
      {restarting ? "Restarting…" : "Refresh"}
    </button>
  );
  const proxySrc = cid ? `${previewHostUrl(cid, previewPort)}/?r=${reloadKey}` : null;
  // E3 — Firefox-safe path-based route. The origin-true `{cid8}-{port}.localhost`
  // URL above is the most correct (separate origin, dev-server assets resolve
  // relative to the same host) but Firefox won't resolve `.localhost` subdomains
  // out of the box. The agent-server ALSO serves a same-origin
  // `/conversations/{cid}/preview-app/{path}` route that proxies the same content
  // — works in every browser, no DNS setup. Expose it as an "Open in new tab"
  // link so Firefox / Safari / non-magic-DNS users have an escape hatch. The
  // subdomain iframe stays — Chrome users get the correct asset origins.
  const openInNewTabUrl = cid
    ? `${agentHttpBase()}/conversations/${encodeURIComponent(cid)}/preview-app/`
    : null;

  // PRIORITY (the fix): default to the RENDERED view, because the backend's bare
  // `python -m http.server` shows a useless directory LISTING (file paths) when
  // there's no index.html at the served root — which is most of the time. The
  // rendered view shows the real HTML instead. A live dev server (a real bundler
  // app, e.g. Vite) is one click away via the toggle / Open, and is used
  // automatically when there's no renderable file to show.
  //
  // E2 — exception for bundler entries: an index.html that references Vite
  // /bundler module scripts (`<script type="module" src="/src/...">`,
  // `/@vite/client`) is UNRUNNABLE as a srcdoc — the iframe can't resolve
  // `/src/main.tsx` or the Vite virtual modules. When a live proxy is available
  // for that case, default straight to the live server so the user sees the
  // real running app, not a broken srcdoc. Static HTML still defaults to
  // "rendered" (the srcdoc DOES render that). The user can always toggle.
  const [mode, setMode] = useState<"rendered" | "live">(() =>
    proxyAvailable && srcDoc != null && isBundlerEntryHtml(srcDoc) ? "live" : "rendered",
  );
  const showLive = proxyAvailable && (mode === "live" || srcDoc == null);

  const selectedOwner =
    previewPort === 8000
      ? data?.owner
      : (data?.ports ?? []).find((p) => p.port === previewPort)?.owner;

  if (showLive && proxySrc) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        <div className="flex shrink-0 items-center justify-between gap-inline border-b border-hairline px-body py-hair">
          <div className="flex flex-col min-w-0">
            <span className="truncate font-mono text-[0.74rem] text-text-faint">live server</span>
            {selectedOwner && (
              <span className="truncate font-mono text-[0.68rem] text-text-faint italic" title={selectedOwner.cmdline}>
                Serving: {selectedOwner.cmdline}
              </span>
            )}
            {boundPorts.length > 1 && (
              <div className="flex items-center gap-hair pt-hair" role="tablist" aria-label="Bound ports">
                {boundPorts.map((p) => (
                  <button
                    key={p}
                    type="button"
                    role="tab"
                    aria-selected={previewPort === p}
                    onClick={() => setPreviewPort(p)}
                    className={cn(
                      "rounded-full border border-hairline px-inline font-mono text-[0.68rem] transition-colors",
                      previewPort === p
                        ? "bg-text text-bg"
                        : "text-text-muted hover:text-text",
                    )}
                  >
                    :{p}
                  </button>
                ))}
              </div>
            )}
          </div>
          <div className="flex items-center gap-inline">
            {RefreshButton}
            {srcDoc != null && (
              <button
                type="button"
                onClick={() => setMode("rendered")}
                className="font-ui text-[0.74rem] text-text-muted transition-colors hover:text-text"
              >
                Rendered
              </button>
            )}
            <a
              href={proxySrc}
              target="_blank"
              rel="noreferrer"
              className="flex items-center gap-hair font-ui text-[0.74rem] text-text-muted transition-colors hover:text-text"
            >
              <ExternalLink className="size-3" aria-hidden /> Open
            </a>
            {openInNewTabUrl && (
              <a
                href={openInNewTabUrl}
                target="_blank"
                rel="noreferrer"
                title="Open via the agent-server path route (works in Firefox / Safari / non-magic-DNS setups)"
                className="flex items-center gap-hair font-ui text-[0.74rem] text-text-muted transition-colors hover:text-text"
              >
                <ExternalLink className="size-3" aria-hidden /> Open in new tab
              </a>
            )}
          </div>
        </div>
        {/* E3 — honest cross-browser hint. The iframe above points at the
            origin-true `{cid8}-{port}.localhost` subdomain, which Chrome
            resolves but Firefox does not. Tell the user the truth (no
            fake-affordance, no auto-redirect): Chrome works inline; Firefox
            needs "Open in new tab" (or a network.dns.localDomains tweak). */}
        <div className="shrink-0 border-b border-hairline bg-surface-1 px-body py-hair font-ui text-[0.7rem] text-text-faint">
          Live preview opens best in Chrome; in Firefox use “Open in new tab” or set{" "}
          <code className="font-mono text-text-muted">network.dns.localDomains</code>.
        </div>
        <iframe
          key={reloadKey}
          title="Live preview"
          src={proxySrc}
          sandbox="allow-scripts allow-forms allow-same-origin allow-popups"
          className="min-h-0 flex-1 border-0 bg-white"
        />
      </div>
    );
  }

  // The renderable HTML artifact → show the real site now, no server needed.
  if (srcDoc != null) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        <div className="flex shrink-0 items-center justify-between gap-inline border-b border-hairline px-body py-hair">
          <span className="truncate font-mono text-[0.74rem] text-text-faint">preview</span>
          <div className="flex items-center gap-inline">
            {RefreshButton}
            {proxyAvailable && (
              <button
                type="button"
                onClick={() => setMode("live")}
                className="flex items-center gap-hair font-ui text-[0.74rem] text-text-muted transition-colors hover:text-text"
              >
                <MonitorPlay className="size-3" aria-hidden /> Live server
              </button>
            )}
          </div>
        </div>
        {untrusted && (
          <div className="shrink-0 border-b border-hairline bg-surface-1 px-body py-hair font-ui text-[0.72rem] text-text-faint">
            Scripted preview is disabled for shared/imported runs (untrusted content).
          </div>
        )}
        <iframe
          key={reloadKey}
          title="Static preview"
          srcDoc={srcDoc}
          // untrusted → empty sandbox (no scripts): a script here could reach this
          // instance's open-CORS APIs. Trusted (your own run) keeps allow-scripts.
          sandbox={untrusted ? "" : "allow-scripts"}
          className="min-h-0 flex-1 border-0 bg-white"
        />
      </div>
    );
  }

  const done = status === "FINISHED" || status === "IDLE";
  return (
    <div className="flex h-full flex-col items-center justify-center gap-inline px-body text-center">
      <p className="font-ui text-[0.9rem] text-text-muted">
        {done ? "Live view" : "Building preview"}
      </p>
      <p className="max-w-measure font-ui text-[0.8rem] text-text-faint">
        {data?.reason ??
          "While the agent builds, a live preview of the deliverable renders here when its dev server starts."}
      </p>
      {data?.stub && (
        <span className="rounded-full border border-weak px-inline py-px font-ui text-[0.66rem] uppercase tracking-wide text-weak">
          podman stub
        </span>
      )}
      {/* one-click recovery even when nothing renders yet (§E7) */}
      {cid && !data?.stub && (
        <button
          type="button"
          onClick={refresh}
          disabled={restarting}
          className="mt-hair flex items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:border-accent hover:text-text disabled:opacity-50"
        >
          <RotateCw className={cn("size-3.5", restarting && "animate-spin")} aria-hidden />
          {restarting ? "Restarting preview…" : "Refresh / restart preview"}
        </button>
      )}
    </div>
  );
}
