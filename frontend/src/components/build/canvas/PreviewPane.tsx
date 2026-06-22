import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ExternalLink, MonitorPlay, Pencil, RotateCw } from "lucide-react";
import { cn } from "@/lib/cn";
import { deriveFiles, deriveSrcDoc } from "@/lib/buildTrace";
import { agentHttpBase, previewHostUrl } from "@/api/client";
import { restartPreview } from "@/api/agent";
import { useBuildPreview } from "@/hooks/useBuildPreview";
import { useElementSelect } from "@/hooks/useElementSelect";
import { SelectionOverlay } from "@/components/build/canvas/SelectionOverlay";
import { EditAffordance } from "@/components/build/canvas/EditAffordance";
import { SELECTION_AGENT_SCRIPT } from "@/lib/selectionAgent";
import { formatEditSteer, parseOid } from "@/lib/resolvers/appResolver";
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
  onSteer,
}: {
  status: ConversationStatus;
  cid: string | null;
  events: AgentEvent[];
  /** The events are UNTRUSTED third-party content (a shared/imported run). Drop
   * `allow-scripts` from the static-preview iframe — with open-CORS no-auth APIs,
   * a script in that frame could fetch this instance's endpoints. */
  untrusted?: boolean;
  /** A1.4 — steer the agent from the click-to-edit affordance. When undefined,
   * the Edit toggle is hidden (no false affordance: nothing to steer). */
  onSteer?: (text: string) => void;
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

  // C5: separate the file list so we can both derive srcDoc AND detect server-side
  // HTML artifacts (content="") that deriveSrcDoc now returns null for.
  const files = useMemo(() => deriveFiles(events), [events]);

  // A client-side srcdoc render of the written files — shows the ACTUAL site the
  // agent wrote (entry HTML + inlined local css/js), no backend / dev server.
  // After the C5 fix, deriveSrcDoc returns null for server-side (content="") HTML
  // artifacts, so the guards below work correctly.
  // §4.1 C-EDIT-1: inject the selection agent into trusted (non-untrusted) srcdoc.
  const srcDoc = useMemo(
    () => deriveSrcDoc(files, untrusted ? undefined : SELECTION_AGENT_SCRIPT),
    [files, untrusted],
  );

  // §4.1 C-EDIT-1: ref + selection state for the srcdoc preview iframe.
  // allowedOrigin is "null" (the string) — sandboxed iframes without
  // allow-same-origin always report origin "null" in postMessage events.
  const srcdocIframeRef = useRef<HTMLIFrameElement | null>(null);
  const {
    armed: srcdocArmed,
    arm: srcdocArm,
    disarm: srcdocDisarm,
    selection: srcdocSelection,
    walkUp: srcdocWalkUp,
    resetSelection: srcdocResetSelection,
  } = useElementSelect(srcdocIframeRef, "null");

  // A1.6 — the entry HTML's workspace path (same pick as deriveSrcDoc: index.html,
  // else any *.html with real client-side content). Used to build the preview-edit
  // URL that the iframe loads in Edit mode (server-stamped + selection-injected).
  const entryHtmlPath = useMemo(() => {
    const withContent = files.filter((f) => f.content && /\.html$/i.test(f.path));
    return (
      withContent.find((f) => /(^|\/)index\.html$/i.test(f.path))?.path ??
      withContent[0]?.path ??
      null
    );
  }, [files]);

  // A1.4/A1.6 — Edit mode is only offered when there's a real steer wire, a real
  // entry HTML to stamp, a conversation, and the run is trusted. Otherwise no
  // toggle is shown (no false affordance).
  const canEdit = Boolean(onSteer && entryHtmlPath && cid && !untrusted);
  const [editMode, setEditMode] = useState(false);
  // The current source-kind selection that carries a real (stamped) file:line —
  // the only case the inline edit box appears for. A deck/non-source/blank-oid
  // selection keeps the existing inspect-only behaviour.
  const sourceRef = useMemo(() => {
    const ref = srcdocSelection?.selection_ref;
    if (!ref || ref.kind !== "source") return null;
    return parseOid(ref.oid);
  }, [srcdocSelection]);
  // The server-stamped, selection-injected edit URL for the entry HTML. The
  // iframe's `key={reloadKey}` already forces a fresh load on Refresh, so no
  // cache-buster query is needed here.
  const editSrc =
    cid && entryHtmlPath
      ? `${agentHttpBase()}/conversations/${encodeURIComponent(cid)}/preview-edit/${entryHtmlPath}`
      : null;

  function applyEdit(instruction: string) {
    if (!onSteer || !sourceRef) return;
    const steerText = formatEditSteer(sourceRef, instruction);
    if (steerText === null) return; // blank instruction → no-op
    onSteer(steerText);
    srcdocResetSelection();
  }

  // C5: when no client-side srcDoc is available, check for a server-side .html
  // artifact (slides_generate / deliverable with empty content string).  If found,
  // we can preview it via the ?inline=true route with a sandboxed iframe.
  const inlineHtmlArtifact = useMemo(
    () => files.find((f) => !f.content && /\.html$/i.test(f.path)) ?? null,
    [files],
  );
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
      aria-label="Refresh the preview — and restart the server if it's down"
      data-disco-control="build.preview-refresh"
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
  // runthru-v2 #4: when a live preview server is available, DEFAULT TO LIVE — it
  // serves the real built site (correct asset resolution for css/js subpaths, which
  // the srcdoc can't do reliably, and which is why the preview showed only a few
  // stray lines/emojis). Previously only bundler-entry HTML defaulted to live and
  // static sites fell to the broken srcdoc. The user can still toggle to "rendered".
  // (Pairs with ROOT-2: files now land at the workspace root the server serves.)
  const [mode, setMode] = useState<"rendered" | "live">(() =>
    proxyAvailable ? "live" : "rendered",
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
    // A1.6 — Edit mode swaps the client-side `srcDoc` for the server-stamped,
    // selection-injected preview-edit route (`src=`). data-oid stamping needs the
    // line-aware server parser, so click-to-edit only works against that route.
    const showEdit = editMode && canEdit && editSrc !== null;
    return (
      <div className="flex h-full min-h-0 flex-col">
        <div className="flex shrink-0 items-center justify-between gap-inline border-b border-hairline px-body py-hair">
          <span className="truncate font-mono text-[0.74rem] text-text-faint">
            {showEdit ? "edit" : "preview"}
          </span>
          <div className="flex items-center gap-inline">
            {RefreshButton}
            {/* A1.4 — Edit toggle (only when steering + a stampable entry exist). */}
            {canEdit && (
              <button
                type="button"
                onClick={() => {
                  // Leaving edit mode disarms any in-flight selection so a stale
                  // edit box can't survive the iframe swap.
                  if (editMode) srcdocDisarm();
                  setEditMode((v) => !v);
                }}
                aria-pressed={showEdit}
                aria-label="Toggle click-to-edit mode on the preview"
                data-disco-control="build.edit-toggle"
                className={cn(
                  "flex items-center gap-hair font-ui text-[0.74rem] transition-colors",
                  showEdit ? "text-accent" : "text-text-muted hover:text-text",
                )}
              >
                <Pencil className="size-3" aria-hidden /> Edit
              </button>
            )}
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
        {showEdit && (
          <div className="shrink-0 border-b border-hairline bg-surface-1 px-body py-hair font-ui text-[0.72rem] text-text-faint">
            Edit mode: click an element, then describe the change — the agent edits the source.
          </div>
        )}
        {/* §4.1 C-EDIT-1: relative wrapper so SelectionOverlay can be positioned
            over the iframe via `absolute inset-0`. */}
        <div className="relative min-h-0 flex-1">
          <iframe
            // Bump the key when switching modes so the iframe (and its in-frame
            // selection agent) re-initialise for the new document.
            key={`${reloadKey}-${showEdit ? "edit" : "view"}`}
            ref={srcdocIframeRef}
            title={showEdit ? "Editable preview" : "Static preview"}
            // Edit mode loads the server-stamped route by URL; view mode renders
            // the client-assembled srcDoc. Both keep the SAME ref + overlay.
            {...(showEdit ? { src: editSrc } : { srcDoc })}
            // untrusted → empty sandbox (no scripts): a script here could reach this
            // instance's open-CORS APIs. Trusted (your own run) keeps allow-scripts.
            // No allow-same-origin in either mode → the frame reports origin "null"
            // (matches useElementSelect(..., "null")) and can't reach this instance.
            sandbox={untrusted ? "" : "allow-scripts"}
            className="h-full w-full border-0 bg-white"
          />
          <SelectionOverlay
            armed={srcdocArmed}
            untrusted={untrusted}
            selection={srcdocSelection}
            onArm={srcdocArm}
            onDisarm={srcdocDisarm}
            onWalkUp={srcdocWalkUp}
          />
          {/* A1.4 — inline edit box. Appears ONLY for a source-kind selection that
              carries a real stamped file:line; a deck/non-source/blank selection
              shows nothing here (no false affordance). */}
          {showEdit && sourceRef !== null && srcdocSelection !== null && (
            <EditAffordance
              file={sourceRef.file}
              line={sourceRef.line}
              rect={srcdocSelection.rect}
              onApply={applyEdit}
              onCancel={srcdocResetSelection}
            />
          )}
        </div>
      </div>
    );
  }

  // C5: server-side HTML artifact → render via ?inline=true.
  // Uses `src=` (NOT srcdoc) so the browser fetches the file from the agent-server
  // route which sets a strict CSP (sandbox allow-scripts; no allow-same-origin).
  // The iframe sandbox here must NOT include allow-same-origin — without it the
  // framed page cannot reach this instance's open-CORS APIs even if it runs scripts.
  // This is intentionally distinct from the live-proxy iframe (which keeps
  // allow-same-origin + allow-forms + allow-popups for dev-server assets).
  if (inlineHtmlArtifact !== null && cid) {
    const inlineSrc = `${agentHttpBase()}/conversations/${encodeURIComponent(cid)}/artifacts/${inlineHtmlArtifact.path}?inline=true&r=${reloadKey}`;
    return (
      <div className="flex h-full min-h-0 flex-col">
        <div className="flex shrink-0 items-center justify-between gap-inline border-b border-hairline px-body py-hair">
          <span className="truncate font-mono text-[0.74rem] text-text-faint">preview</span>
          <div className="flex items-center gap-inline">
            {RefreshButton}
          </div>
        </div>
        {untrusted && (
          <div className="shrink-0 border-b border-hairline bg-surface-1 px-body py-hair font-ui text-[0.72rem] text-text-faint">
            Scripted preview is disabled for shared/imported runs (untrusted content).
          </div>
        )}
        <iframe
          key={reloadKey}
          title="Artifact preview"
          src={inlineSrc}
          // no allow-same-origin: the sandboxed page must not reach this instance's APIs.
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
          aria-label="Refresh or restart the preview server"
          data-disco-control="build.preview-restart"
          className="mt-hair flex items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:border-accent hover:text-text disabled:opacity-50"
        >
          <RotateCw className={cn("size-3.5", restarting && "animate-spin")} aria-hidden />
          {restarting ? "Restarting preview…" : "Refresh / restart preview"}
        </button>
      )}
    </div>
  );
}
