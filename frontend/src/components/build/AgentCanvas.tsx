/**
 * The Agent surface inspector (right pane) — a LIGHTER sibling of ExecutionCanvas.
 * The Agent surface operates on the WORLD (browser, tools, artifacts) rather than
 * building source, so this canvas foregrounds the BROWSER and ARTIFACTS and demotes
 * the console. Everything here is REAL: derived from the event stream (no fetches
 * except the img/iframe/anchor URLs the browser loads directly). Honest by
 * construction — no faked viewport for browser-MCP servers (which return fenced
 * text, not screenshots), no broken <img> without a conversation to fetch against.
 */

import { useMemo, useRef, useState, useEffect } from "react";
import * as Tabs from "@radix-ui/react-tabs";
import { Download, FileCode2, FileSpreadsheet, FileText, Globe, History, Package, Presentation, SquareTerminal } from "lucide-react";
import { cn } from "@/lib/cn";
import {
  deriveActivity,
  deriveDeliverable,
  deriveFiles,
  deriveLiveSignal,
  deriveSrcDoc,
  deriveTerminal,
} from "@/lib/buildTrace";
import type { WorkspaceFile } from "@/lib/buildTrace";
import {
  agentGet,
  agentSend,
  agentHttpBase,
  previewBootstrapUrl,
  pathPreviewBootstrapUrl,
  type PreviewLaunch,
} from "@/api/client";
import { useElementSelect } from "@/hooks/useElementSelect";
import { SelectionOverlay } from "@/components/build/canvas/SelectionOverlay";
import { ActivityFeed } from "@/components/build/ActivityFeed";
import { formatSelectionContext } from "@/lib/resolvers/appResolver";
import { SELECTION_AGENT_SCRIPT } from "@/lib/selectionAgent";
import { useLiveBrowserConfig } from "@/hooks/useModels";
import { DeckEditorPane } from "@/components/build/DeckEditorPane";
import { latestEditableDeckBase } from "@/components/build/latestEditableDeckBase";
import type { AgentEvent, ConversationStatus } from "@/types/agent";
import { PreviewLaunchFrame } from "@/components/PreviewLaunchFrame";

type TabId = "browser" | "artifacts" | "console" | "history" | "deck";

/** All screenshot_paths the browser/MCP tools produced, in chronological order
 * (latest last). Real, straight from each observation's structured payload. */
function collectScreenshots(events: AgentEvent[]): string[] {
  const out: string[] = [];
  for (const e of events) {
    if (e.kind !== "observation") continue;
    const sp = e.tool_result.structured?.screenshot_path;
    if (typeof sp === "string" && sp) out.push(sp);
  }
  return out;
}

/** The URL of the most recent `browser` action (its arguments.url) — the page the
 * agent last drove to. Mirrors buildTrace's detailFor() browser extraction. */
function latestBrowserUrl(events: AgentEvent[]): string | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.kind === "action" && e.tool_call?.tool_name === "browser") {
      const u = e.tool_call.arguments.url;
      if (typeof u === "string" && u) return u;
    }
  }
  return null;
}

function fmtBytes(n: number): string {
  return n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`;
}

/** The /browser/live-url `reason`s for which an auto-start is GENUINELY DOOMED — a hard
 * capability/config error that won't fix itself, so we stop retrying for that cid. Every
 * other failure (no_upstream, no_daemon, no_sandbox, network blip) is TRANSIENT: the
 * readiness-gated poll retries it on the next tick once the browser is genuinely up. */
const DOOMED_LIVE_URL_REASONS: ReadonlySet<string> = new Set(["unsupported_backend", "disabled"]);
const LIVE_URL_FAILURE_CAP = 3;

/** Pull the machine `reason` out of a failed agentGet (ApiError.message carries the raw
 * JSON body `{reason, message}`). Returns null when there's no parseable reason — which
 * is treated as TRANSIENT (retryable), the safe default (the poll bounds the retries). */
function liveUrlReason(e: unknown): string | null {
  const msg = e instanceof Error ? e.message : "";
  try {
    const o = JSON.parse(msg) as { reason?: unknown };
    return typeof o?.reason === "string" ? o.reason : null;
  } catch {
    return null;
  }
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-hair px-body text-center font-ui text-[0.82rem] text-text-faint">
      {children}
    </div>
  );
}

/** A workspace image with an honest onError→placeholder fallback (the screenshot
 * may have been pruned). Key by `src` in the parent so the failed state resets
 * when the selection changes. */
function WorkspaceImage({ src, className }: { src: string; className: string }) {
  const [failed, setFailed] = useState(false);
  if (failed)
    return (
      <span
        data-screenshot-state="missing"
        className="font-ui text-[0.74rem] text-text-faint italic"
      >
        screenshot no longer available
      </span>
    );
  return (
    <img
      src={src}
      alt="browser screenshot"
      data-screenshot-state="present"
      onError={() => setFailed(true)}
      className={className}
    />
  );
}

function BrowserPane({
  events,
  status,
  cid,
}: {
  events: AgentEvent[];
  status: ConversationStatus;
  cid: string | null;
}) {
  const shots = useMemo(() => collectScreenshots(events), [events]);
  const url = useMemo(() => latestBrowserUrl(events), [events]);
  // "driving…" while a browser / external browser-MCP tool is executing.
  const live = deriveLiveSignal(events, status);
  const driving =
    live.kind === "tool_executing" &&
    (live.tool_name === "browser" || live.tool_name.startsWith("mcp__"));
  const [picked, setPicked] = useState<string | null>(null);
  const hero = picked && shots.includes(picked) ? picked : (shots[shots.length - 1] ?? null);

  // Live browser (noVNC) state.
  // REDESIGN: there is NO manual "Live" button. When the feature is enabled in Settings
  // AND the backend can actually stream (live-ready true) AND the agent has a browser
  // session up, the live view AUTO-STARTS; it tears down when the session ends, the
  // conversation switches, the feature is disabled, or the pane unmounts. Start
  // failures remain visible while the screenshot reel provides a bounded fallback.
  const { data: liveBrowserCfg } = useLiveBrowserConfig();
  const liveBrowserEnabled = liveBrowserCfg?.enabled ?? false;
  // liveView carries the cid that OWNS the stack (`ownerCid`) — BrowserPane is not
  // keyed by cid, so on a conversation switch we must tear down the conversation that
  // opened the view, NOT whatever cid is current now (else we stop the wrong sandbox
  // and leak the old VNC stack).
  const [liveView, setLiveView] = useState<
    { launch: PreviewLaunch; ownerCid: string } | null
  >(null);
  // Streamability TRUTH from the side-effect-free /browser/live-ready probe: true only
  // when this backend can actually run + stream the stack (gVisor) AND a sandbox + healthy
  // browser daemon are up. Drives BOTH auto-start (start only when genuinely startable)
  // and auto-stop (when it goes false the session ended → revert to screenshots).
  const [liveReady, setLiveReady] = useState(false);
  // True once the noVNC iframe has actually LOADED — the honest "genuinely streaming"
  // signal gating the green-blink "Live" badge (not merely "we requested a URL").
  const [iframeConnected, setIframeConnected] = useState(false);
  const [liveStartFailure, setLiveStartFailure] = useState<"retrying" | "unavailable" | null>(
    null,
  );
  // Mirror live state into a ref so the poll/unmount effects read the latest value
  // without re-subscribing on every change.
  const liveViewRef = useRef(liveView);
  liveViewRef.current = liveView;
  // The cid an auto-start is GENUINELY DOOMED for (a hard capability/config error like
  // unsupported_backend) — only THIS suppresses further attempts. A TRANSIENT live-url
  // failure (e.g. no_upstream: live_start succeeded but the port isn't exposed yet) must
  // NOT latch, so the next readiness tick retries once the browser is genuinely up. Reset
  // when the session ends / the conversation switches so a fresh browse can re-start.
  const doomedRef = useRef<string | null>(null);
  const startFailuresRef = useRef(0);
  // Guards against overlapping live-url calls (a slow start spanning >1 poll tick). The
  // poll cadence (4s) is the retry clock, so retries are bounded — never a render-storm.
  const startInFlightRef = useRef(false);

  // Tell the sandbox to tear the live-view stack down. Best-effort + fire-and-forget:
  // teardown should never block on (or error from) the call.
  const stopLiveStack = (conv: string) => {
    void agentSend("POST", `/conversations/${encodeURIComponent(conv)}/browser/live-stop`).catch(
      () => {},
    );
  };

  // Poll the side-effect-free readiness probe while the feature is enabled and there's a
  // cid. The poll is BOTH the auto-start clock AND the auto-stop watchdog:
  //  • ready + no view open → AUTO-START the stack (the same server path the old button
  //    used, no user action). Driving the start from the 4s poll means a transient
  //    live-url failure retries on the NEXT tick — bounded, never a tight render loop.
  //  • not ready while a view is open → the session ended → tear down + revert to
  //    screenshots (2-miss debounce so a single blip doesn't kill a healthy stream).
  // The probe itself has no side-effects (no live_start, no port map).
  useEffect(() => {
    if (!liveBrowserEnabled || !cid) {
      setLiveReady(false);
      setLiveStartFailure(null);
      return;
    }
    let cancelled = false;
    let missStreak = 0;
    doomedRef.current = null; // fresh conversation → clear any prior doomed verdict
    startFailuresRef.current = 0;
    setLiveStartFailure(null);
    const probe = async () => {
      let ready = false;
      try {
        const r = await agentGet<{ ready: boolean; reason: string }>(
          `/conversations/${encodeURIComponent(cid)}/browser/live-ready`,
        );
        ready = !!r.ready;
      } catch {
        ready = false;
      }
      if (cancelled) return;
      setLiveReady(ready);
      if (ready) {
        missStreak = 0;
        // AUTO-START (retry-safe, bounded to this 4s cadence). Only when no view is open,
        // none is in flight, and this cid isn't already known-doomed.
        if (!liveViewRef.current && !startInFlightRef.current && doomedRef.current !== cid) {
          startInFlightRef.current = true;
          try {
            const data = await agentSend<{
              ready: boolean;
              novnc_path: string;
              port: number;
              reason?: string;
            }>(
              "POST",
              `/conversations/${encodeURIComponent(cid)}/browser/live-url`,
            );
            if (!data.ready || !data.port || !data.novnc_path) {
              throw new Error(
                JSON.stringify({ reason: data.reason || "no_upstream" }),
              );
            }
            // SECURITY: build the single-origin proxy URL client-side
            // ({cid8}-6080.localhost). The server intentionally never returns a raw sandbox
            // host:port — that would bypass the auth/cid-scoping proxy. previewHostUrl is
            // the same helper the dev-server preview uses.
            const src = await previewBootstrapUrl(cid, data.port, data.novnc_path);
            if (!src) {
              throw new Error(JSON.stringify({ reason: "no_upstream" }));
            }
            if (!cancelled) {
              startFailuresRef.current = 0;
              setLiveStartFailure(null);
              setIframeConnected(false);
              setLiveView({ launch: src, ownerCid: cid });
            }
          } catch (e: unknown) {
            // Distinguish DOOMED from TRANSIENT without exposing raw backend errors.
            // Transient starts retry only at the 4s readiness cadence and stop after
            // LIVE_URL_FAILURE_CAP attempts; the visible screenshot fallback remains
            // honest instead of silently hammering live-url forever.
            if (cancelled) return;
            startFailuresRef.current += 1;
            const exhausted = startFailuresRef.current >= LIVE_URL_FAILURE_CAP;
            if (DOOMED_LIVE_URL_REASONS.has(liveUrlReason(e) ?? "") || exhausted) {
              doomedRef.current = cid;
              setLiveStartFailure("unavailable");
            } else {
              setLiveStartFailure("retrying");
            }
          } finally {
            startInFlightRef.current = false;
          }
        }
        return;
      }
      // Not streamable. If a view of THIS conversation is open, the session has ended —
      // tear down + revert to screenshots. Require 2 consecutive misses (~8s) so a single
      // transient probe blip doesn't kill a healthy stream.
      missStreak += 1;
      const lv = liveViewRef.current;
      if (lv && lv.ownerCid === cid && missStreak >= 2) {
        stopLiveStack(lv.ownerCid);
        setLiveView(null);
        setIframeConnected(false);
        doomedRef.current = null; // allow a fresh auto-start if browsing resumes
        startFailuresRef.current = 0;
        setLiveStartFailure(null);
      }
    };
    void probe();
    const id = setInterval(() => void probe(), 4000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [liveBrowserEnabled, cid]);

  // Tear the OWNING conversation's stack down when the feature is disabled in Settings,
  // OR when the surface switches to a different conversation while a live view is open
  // (BrowserPane isn't keyed by cid, so we must stop liveView.ownerCid, not `cid`).
  useEffect(() => {
    if (!liveView) return;
    if (!liveBrowserEnabled || liveView.ownerCid !== cid) {
      stopLiveStack(liveView.ownerCid);
      setLiveView(null);
      setIframeConnected(false);
      doomedRef.current = null;
      startFailuresRef.current = 0;
      setLiveStartFailure(null);
    }
  }, [liveBrowserEnabled, liveView, cid]);

  // Heartbeat: while the live view is open, refresh the sandbox idle watchdog so an
  // ACTIVELY-watched session is never reaped under the user. If the tab is closed /
  // sleeps / loses the network, the heartbeats stop and the watchdog reaps the stack
  // (the backstop for an abandoned view that never fired an unmount/close).
  useEffect(() => {
    if (!liveView) return;
    const owner = liveView.ownerCid;
    const id = setInterval(() => {
      void agentSend("POST", `/conversations/${encodeURIComponent(owner)}/browser/live-touch`).catch(
        () => {},
      );
    }, 240_000); // 4 min < the 600s server idle timeout
    return () => clearInterval(id);
  }, [liveView]);

  useEffect(() => {
    return () => {
      // On unmount, stop the live stack this pane started (its owning cid). Reads via the
      // ref so this fires only on unmount (empty deps) with the latest owner.
      if (liveViewRef.current) stopLiveStack(liveViewRef.current.ownerCid);
    };
  }, []);

  // The HONEST "actually streaming" signal: the stack started (liveView set), the noVNC
  // iframe has loaded (iframeConnected), AND the backend still confirms the stack is
  // running (liveReady). Only then do we show the green-blink "Live" badge.
  const streaming = !!liveView && iframeConnected && liveReady;
  const LiveFailureNotice = liveStartFailure ? (
    <div
      role="status"
      data-testid="live-fallback-notice"
      className="shrink-0 border-b border-warn/30 bg-warn/10 px-body py-hair font-ui text-[0.72rem] text-warn"
    >
      {liveStartFailure === "retrying"
        ? "Live view temporarily unavailable; retrying. Showing captured screenshots meanwhile."
        : "Live view unavailable after bounded startup attempts. Showing captured screenshots instead."}
    </div>
  ) : null;

  const Header =
    url || driving || liveView ? (
      <div className="flex shrink-0 items-center justify-between gap-inline border-b border-hairline px-body py-hair">
        <span className="truncate font-mono text-[0.74rem] text-text-faint" title={url ?? undefined}>
          {url ?? "browser"}
        </span>
        <div className="flex shrink-0 items-center gap-inline">
          {/* genuinely streaming → the honest green-blink "Live" badge (no ambiguity) */}
          {streaming && (
            <span
              className="flex shrink-0 items-center gap-hair font-ui text-[0.72rem] font-medium text-[oklch(0.74_0.17_150)]"
              data-disco-control="agent.live-browser"
              data-streaming="true"
              data-testid="live-badge"
              aria-label="Live browser streaming"
            >
              <span
                className="size-1.5 animate-pulse rounded-full bg-[oklch(0.74_0.17_150)]"
                aria-hidden
              />
              Live
            </span>
          )}
          {/* not yet streaming but the agent is actively driving a browser → the existing
              "driving…" pulse (the live view auto-starts behind this when streamable) */}
          {driving && !streaming && (
            <span className="flex shrink-0 items-center gap-hair font-ui text-[0.72rem] text-accent">
              <span className="size-1.5 animate-pulse rounded-full bg-accent" aria-hidden />
              driving…
            </span>
          )}
        </div>
      </div>
    ) : null;

  // When live view is active, show the noVNC iframe instead of screenshots.
  if (liveView && cid) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        {Header}
        <div className="flex min-h-0 flex-1 flex-col">
          <PreviewLaunchFrame
            title="Live browser (noVNC)"
            launch={liveView.launch}
            // The iframe genuinely LOADING is the truth behind the green-blink badge.
            onLoad={() => setIframeConnected(true)}
            // The capability host is cross-origin from the app. same-origin is
            // required inside the frame so noVNC's websocket has its real Origin;
            // cross-origin browser isolation still prevents access to this page.
            sandbox="allow-scripts allow-forms allow-same-origin"
            className="h-full w-full border-0"
            data-testid="novnc-iframe"
          />
        </div>
      </div>
    );
  }

  // No screenshot we can actually load (none captured, or no cid to fetch against)
  // → an honest empty state, never a broken <img>. Auto-start failure also lands
  // here with LiveFailureNotice, so the screenshot fallback is explicit.
  if (!hero || !cid) {
    return (
      <div className="flex h-full min-h-0 flex-col" data-screenshot-state="empty">
        {Header}
        {LiveFailureNotice}
        <Empty>
          <p className="text-text-muted">
            When the agent uses a browser, the pages it visits show here — a screenshot for each
            step, updated as it navigates (a frame-by-frame reel, not a live video).
          </p>
          <p className="max-w-measure">
            External browser-MCP servers return fenced text, not screenshots — for those this shows
            status, not a faked viewport.
          </p>
        </Empty>
      </div>
    );
  }

  const heroSrc = `${agentHttpBase()}/conversations/${cid}/workspace/${hero}`;
  return (
    <div className="flex h-full min-h-0 flex-col">
      {Header}
      {LiveFailureNotice}
      <div className="flex min-h-0 flex-1 items-center justify-center overflow-auto bg-[oklch(0.15_0.005_260)] p-inline">
        <WorkspaceImage
          key={heroSrc}
          src={heroSrc}
          className="max-h-full max-w-full rounded border border-hairline object-contain"
        />
      </div>
      {shots.length > 1 && (
        <div className="flex shrink-0 items-center gap-hair overflow-x-auto border-t border-hairline px-body py-hair">
          {shots.map((p, i) => {
            const src = `${agentHttpBase()}/conversations/${cid}/workspace/${p}`;
            return (
              <button
                key={`${p}-${i}`}
                type="button"
                onClick={() => setPicked(p)}
                className={cn(
                  "shrink-0 overflow-hidden rounded border transition-colors",
                  p === hero ? "border-accent" : "border-hairline hover:border-hairline-strong",
                )}
              >
                <img src={src} alt="" className="h-12 w-auto object-cover" />
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function FileRow({ file, cid }: { file: WorkspaceFile; cid: string | null }) {
  const isSheet = /\.xlsx$/i.test(file.path);
  const Icon = isSheet ? FileSpreadsheet : /\.html?$/i.test(file.path) ? FileCode2 : FileText;
  // The declared-artifact route (encodeURI preserves any subdir slashes); honest —
  // only a real link when there's a conversation to fetch against.
  const href = cid
    ? `${agentHttpBase()}/conversations/${cid}/artifacts/${encodeURI(file.path)}`
    : null;
  const inner = (
    <>
      <Icon className={cn("size-4 shrink-0", isSheet ? "text-accent" : "text-text-faint")} aria-hidden />
      <span className="min-w-0 flex-1">
        <span className="block truncate font-ui text-[0.82rem] text-text">
          {file.path.split("/").pop()}
        </span>
        <span className="block truncate font-mono text-[0.7rem] text-text-faint">
          {file.path} · {fmtBytes(file.bytes)}
        </span>
      </span>
      {href && <Download className="size-3.5 shrink-0 text-text-faint" aria-hidden />}
    </>
  );
  const base = "flex items-center gap-inline rounded-card border border-hairline bg-surface-0 px-inline py-hair";
  return href ? (
    <a href={href} download className={cn(base, "transition-colors hover:border-hairline-strong")}>
      {inner}
    </a>
  ) : (
    <div className={base}>{inner}</div>
  );
}

function ArtifactsPane({
  events,
  status,
  cid,
  untrusted,
  onSteer,
}: {
  events: AgentEvent[];
  status: ConversationStatus;
  cid: string | null;
  untrusted: boolean;
  /** W-26 — steer the agent from the artifact-preview selection ("Discuss with
   * agent"). Undefined when steering isn't available (no false affordance). */
  onSteer?: (text: string) => void;
}) {
  const files = useMemo(() => deriveFiles(events), [events]);
  const selectedDeliverable = useMemo(() => deriveDeliverable(events), [events]);
  const selectedHtmlPath =
    selectedDeliverable && /\.html?$/i.test(selectedDeliverable.path)
      ? selectedDeliverable.path
      : undefined;
  // §4.1 C-EDIT-1: inject selection agent for trusted (non-untrusted) iframes.
  const srcDoc = useMemo(
    () =>
      deriveSrcDoc(
        files,
        untrusted ? undefined : SELECTION_AGENT_SCRIPT,
        undefined,
        selectedHtmlPath,
      ),
    [files, selectedHtmlPath, untrusted],
  );
  const committedStaticPreview =
    status === "FINISHED" &&
    !untrusted &&
    cid !== null &&
    selectedDeliverable?.kind === "app" &&
    events.some((event) => event.kind === "workspace_version");
  const committedGeneration = useMemo(() => {
    let treeDigest = "unversioned";
    for (let i = events.length - 1; i >= 0; i -= 1) {
      const event = events[i];
      if (event?.kind === "workspace_version") {
        treeDigest = event.tree_digest;
        break;
      }
    }
    return `${selectedDeliverable?.id ?? "none"}:${treeDigest}`;
  }, [events, selectedDeliverable?.id]);
  const [staticPreviewSrc, setStaticPreviewSrc] = useState<PreviewLaunch | null>(null);
  const staticMintRef = useRef<{
    key: string;
    promise: Promise<PreviewLaunch | null>;
  } | null>(null);
  useEffect(() => {
    let cancelled = false;
    if (!committedStaticPreview || !cid || srcDoc === null) {
      setStaticPreviewSrc(null);
      staticMintRef.current = null;
      return;
    }
    const key = `${cid}:committed:${committedGeneration}`;
    const promise =
      staticMintRef.current?.key === key
        ? staticMintRef.current.promise
        : pathPreviewBootstrapUrl(cid, "/");
    staticMintRef.current = { key, promise };
    void promise
      .then((url) => {
        if (!cancelled) setStaticPreviewSrc(url);
      })
      .catch(() => {
        if (!cancelled) setStaticPreviewSrc(null);
      });
    return () => {
      cancelled = true;
    };
  }, [cid, committedGeneration, committedStaticPreview, srcDoc]);

  // §4.1 C-EDIT-1: ref + selection state for the artifact preview iframe.
  const artifactIframeRef = useRef<HTMLIFrameElement | null>(null);
  const {
    armed: artifactArmed,
    arm: artifactArm,
    disarm: artifactDisarm,
    selection: artifactSelection,
    walkUp: artifactWalkUp,
    resetSelection: artifactResetSelection,
  } = useElementSelect(
    artifactIframeRef,
    staticPreviewSrc ? new URL(staticPreviewSrc.url).origin : "null",
  );

  if (files.length === 0)
    return <Empty>Outputs the agent produces — files, spreadsheets, pages — show up here.</Empty>;
  return (
    <div className="flex h-full min-h-0 flex-col">
      {srcDoc != null && (
        <div className="flex min-h-0 flex-[2] flex-col border-b border-hairline">
          <div className="flex shrink-0 items-center justify-between border-b border-hairline px-body py-hair font-mono text-[0.74rem] text-text-faint">
            <span>preview</span>
            {untrusted && (
              <span className="font-ui text-text-faint">scripts disabled (untrusted)</span>
            )}
          </div>
          {/* §4.1 C-EDIT-1: relative wrapper for SelectionOverlay positioning. */}
          <div className="relative min-h-0 flex-1">
            {staticPreviewSrc ? (
              <PreviewLaunchFrame
                ref={artifactIframeRef}
                title="Artifact preview"
                launch={staticPreviewSrc}
                sandbox="allow-scripts allow-same-origin"
                className="h-full w-full border-0 bg-white"
              />
            ) : (
              <iframe
                ref={artifactIframeRef}
                title="Artifact preview"
                srcDoc={srcDoc}
                // untrusted (shared/imported run) → empty sandbox, no scripts: a script
                // here could reach this instance's open-CORS APIs.
                sandbox={untrusted ? "" : "allow-scripts"}
                className="h-full w-full border-0 bg-white"
              />
            )}
            <SelectionOverlay
              armed={artifactArmed}
              untrusted={untrusted}
              selection={artifactSelection}
              onArm={artifactArm}
              onDisarm={artifactDisarm}
              onWalkUp={artifactWalkUp}
              // W-26 — Discuss is offered whenever steering is available (onSteer
              // defined). Delivers the named-element context to the agent, then clears.
              onDiscuss={
                onSteer
                  ? (sel) => {
                      onSteer(formatSelectionContext(sel));
                      artifactResetSelection();
                      artifactDisarm();
                    }
                  : undefined
              }
            />
          </div>
        </div>
      )}
      <ul className="flex min-h-0 flex-1 flex-col gap-hair overflow-auto p-body">
        {files.map((f) => (
          <li key={f.path}>
            <FileRow file={f} cid={cid} />
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Console — de-emphasized: the same command+output history view ExecutionCanvas's
 * Terminal renders (success/failure color-coded), no live tmux polling. */
function ConsolePane({ events }: { events: AgentEvent[] }) {
  const entries = useMemo(() => deriveTerminal(events), [events]);
  if (entries.length === 0) return <Empty>Commands the agent runs appear here.</Empty>;
  return (
    <div className="h-full overflow-auto bg-[oklch(0.15_0.005_260)] px-body py-inline font-mono text-[0.78rem] leading-relaxed">
      {entries.map((e) => (
        <div key={e.id} className="mb-inline">
          <div className="flex items-center gap-hair text-supported">
            <span className="text-text-faint">$</span>
            <span className="text-[oklch(0.85_0.02_150)]">{e.command}</span>
            {e.running && <span className="animate-pulse text-text-faint">▋</span>}
          </div>
          {e.output && (
            <pre
              className={cn(
                "mt-px whitespace-pre-wrap",
                e.success ? "text-[oklch(0.78_0.005_260)]" : "text-unsupported",
              )}
            >
              {e.output.length > 4000 ? e.output.slice(0, 4000) + "\n…" : e.output}
            </pre>
          )}
        </div>
      ))}
    </div>
  );
}

/** W-43 — Agent History: the FULL ActivityFeed surfaced as an inspector tab, so the
 * complete narrative (and every inline artifact/download/screenshot/deck-export
 * affordance it carries) stays reachable even when the left chat pane is collapsed to
 * the stage card (Verbose Agent Chat OFF). Derived from the same event stream. */
function HistoryPane({ events, status, cid }: { events: AgentEvent[]; status: ConversationStatus; cid: string | null }) {
  // No pending-confirmation context in the inspector → pass null; the gate panels
  // live on the chat pane, this is the read-only chronological log.
  const activity = useMemo(() => deriveActivity(events, null, status), [events, status]);
  if (activity.length === 0)
    return <Empty>The agent's step-by-step history — messages, tool calls, and outputs — shows here.</Empty>;
  return (
    <div className="h-full overflow-auto px-body py-inline">
      <ActivityFeed items={activity} conversationId={cid ?? undefined} />
    </div>
  );
}

const BASE_TABS: { id: TabId; label: string; icon: typeof Globe }[] = [
  { id: "browser", label: "Browser", icon: Globe },
  { id: "artifacts", label: "Artifacts", icon: Package },
  { id: "console", label: "Console", icon: SquareTerminal },
  { id: "history", label: "Agent History", icon: History },
];

export function AgentCanvas({
  events,
  status,
  cid,
  untrusted = false,
  onSteer,
}: {
  events: AgentEvent[];
  status: ConversationStatus;
  cid: string | null;
  /** Third-party events (shared/imported run) → harden the artifact preview iframe. */
  untrusted?: boolean;
  /** W-26 — steer the agent from the artifact-preview "Discuss with agent" action.
   * Undefined when steering isn't available (no false affordance). */
  onSteer?: (text: string) => void;
}) {
  // A screenshot to show → Browser is the hero; else artifacts exist → Artifacts;
  // else Browser (empty state). Initial only — don't yank the user's tab as the
  // stream grows (mirrors ExecutionCanvas's useMemo([]) pattern).
  const initial: TabId = useMemo(
    () =>
      collectScreenshots(events).length > 0
        ? "browser"
        : deriveFiles(events).length > 0
          ? "artifacts"
          : "browser",
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );
  const [tab, setTab] = useState<TabId>(initial);

  const hasScreenshots = collectScreenshots(events).length > 0;
  useEffect(() => {
    if (hasScreenshots) setTab("browser");
  }, [hasScreenshots]);

  // A2: the Edit/Export Slides tab appears ONLY when a deck with an editable AuthoredDeck
  // sidecar exists AND there's a conversation to edit against (no false affordance).
  const editableDeckBase = useMemo(() => latestEditableDeckBase(events), [events]);

  // BW-15: when a fresh slides_generate just completed, auto-foreground the
  // Edit/Export Slides tab (mirrors the hasScreenshots→Browser auto-switch). Fire
  // ONLY on the absent→present transition of editableDeckBase — a ref-remembered
  // prev base keeps it from re-firing on every re-render (so it doesn't fight a
  // later manual tab click). Declared AFTER the hasScreenshots effect so, in a
  // commit where both transition, the deck switch wins for a slides build.
  const prevDeckBaseRef = useRef<string | null>(null);
  useEffect(() => {
    const prev = prevDeckBaseRef.current;
    prevDeckBaseRef.current = editableDeckBase;
    if (!prev && editableDeckBase && cid) setTab("deck");
  }, [editableDeckBase, cid]);

  const tabs = useMemo(
    () =>
      editableDeckBase && cid
        ? [...BASE_TABS, { id: "deck" as TabId, label: "Edit/Export Slides", icon: Presentation }]
        : BASE_TABS,
    [editableDeckBase, cid],
  );

  // If the editable deck disappears (e.g. events pruned) while its tab is active,
  // fall back to Artifacts so we never sit on a dead tab.
  useEffect(() => {
    if (tab === "deck" && !(editableDeckBase && cid)) setTab("artifacts");
  }, [tab, editableDeckBase, cid]);

  return (
    <Tabs.Root
      value={tab}
      onValueChange={(v) => setTab(v as TabId)}
      className="flex h-full min-h-0 flex-col"
    >
      <Tabs.List className="flex shrink-0 items-center gap-px border-b border-hairline px-inline">
        {tabs.map((t) => (
          <Tabs.Trigger
            key={t.id}
            value={t.id}
            className={cn(
              "flex items-center gap-hair px-inline py-inline font-ui text-[0.78rem] text-text-muted transition-colors",
              "border-b-2 border-transparent hover:text-text",
              "data-[state=active]:border-accent data-[state=active]:text-text",
            )}
          >
            <t.icon className="size-3.5" aria-hidden />
            {t.label}
          </Tabs.Trigger>
        ))}
      </Tabs.List>
      <div className="min-h-0 flex-1">
        <Tabs.Content value="browser" className="h-full focus:outline-none">
          <BrowserPane events={events} status={status} cid={cid} />
        </Tabs.Content>
        <Tabs.Content value="artifacts" className="h-full focus:outline-none">
          <ArtifactsPane
            events={events}
            status={status}
            cid={cid}
            untrusted={untrusted}
            onSteer={onSteer}
          />
        </Tabs.Content>
        <Tabs.Content value="console" className="h-full focus:outline-none">
          <ConsolePane events={events} />
        </Tabs.Content>
        <Tabs.Content value="history" className="h-full focus:outline-none">
          <HistoryPane events={events} status={status} cid={cid} />
        </Tabs.Content>
        {editableDeckBase && cid && (
          <Tabs.Content value="deck" className="h-full focus:outline-none">
            <DeckEditorPane cid={cid} base={editableDeckBase} />
          </Tabs.Content>
        )}
      </div>
    </Tabs.Root>
  );
}
