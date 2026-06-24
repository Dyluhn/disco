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
import { deriveActivity, deriveFiles, deriveSrcDoc, deriveTerminal, deriveLiveSignal } from "@/lib/buildTrace";
import type { WorkspaceFile } from "@/lib/buildTrace";
import { agentGet, agentSend, agentHttpBase, previewHostUrl } from "@/api/client";
import { useElementSelect } from "@/hooks/useElementSelect";
import { SelectionOverlay } from "@/components/build/canvas/SelectionOverlay";
import { ActivityFeed } from "@/components/build/ActivityFeed";
import { formatSelectionContext } from "@/lib/resolvers/appResolver";
import { SELECTION_AGENT_SCRIPT } from "@/lib/selectionAgent";
import { useLiveBrowserConfig } from "@/hooks/useModels";
import { DeckEditorPane } from "@/components/build/DeckEditorPane";
import type { AgentEvent, ConversationStatus } from "@/types/agent";

type TabId = "browser" | "artifacts" | "console" | "history" | "deck";

/** The base name of the most-recent deck that carries an editable AuthoredDeck
 * sidecar (A2.0 sets structured.editable_source on the slides_generate C2 path).
 * That sidecar is what the in-app deck editor reads — its absence means the deck
 * is a Marp/markdown deck (no editable source), so the Edit/Export Slides tab must
 * not appear (no false affordance). Latest editable deck wins. Null when none. */
export function latestEditableDeckBase(events: AgentEvent[]): string | null {
  // The LATEST render per base decides editability (mirrors the server's
  // _sidecar_is_current guard): a later non-editable regen of a base (Marp/fallback —
  // base_name but no editable_source) supersedes its earlier editable sidecar, so the
  // stale sidecar must NOT be offered (editing it would overwrite the newer deck).
  const decided = new Set<string>();
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.kind !== "observation") continue;
    const { tool_name, structured, success } = e.tool_result;
    if (tool_name !== "slides_generate" || !success || !structured) continue;
    const editable = structured.editable_source;
    const baseName = typeof structured.base_name === "string" ? structured.base_name : null;
    const base =
      baseName ??
      (typeof editable === "string" && editable
        ? editable.replace(/\.authored\.json$/i, "")
        : null);
    if (!base || decided.has(base)) continue; // a LATER event already decided this base
    decided.add(base); // this is the latest render of `base`
    // The editor route jails the base to a plain name (no "/"); a nested deck like
    // "reports/q2" is downloadable but NOT editable — don't offer a tab that 404s.
    if (base.includes("/")) continue;
    if (typeof editable === "string" && editable) return base; // …and it's editable
    // else: latest render of this base is non-editable → skip; scan other bases
  }
  return null;
}

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
  // conversation switches, the feature is disabled, or the pane unmounts. A scary
  // start-failure is never surfaced — we silently fall back to the screenshot reel.
  const { data: liveBrowserCfg } = useLiveBrowserConfig();
  const liveBrowserEnabled = liveBrowserCfg?.enabled ?? false;
  // liveView carries the cid that OWNS the stack (`ownerCid`) — BrowserPane is not
  // keyed by cid, so on a conversation switch we must tear down the conversation that
  // opened the view, NOT whatever cid is current now (else we stop the wrong sandbox
  // and leak the old VNC stack).
  const [liveView, setLiveView] = useState<
    { url: string; novnc_path: string; ownerCid: string } | null
  >(null);
  const [liveLoading, setLiveLoading] = useState(false);
  // Streamability TRUTH from the side-effect-free /browser/live-ready probe: true only
  // when this backend can actually run + stream the stack (gVisor) AND a sandbox + healthy
  // browser daemon are up. Drives BOTH auto-start (start only when genuinely startable)
  // and auto-stop (when it goes false the session ended → revert to screenshots).
  const [liveReady, setLiveReady] = useState(false);
  // True once the noVNC iframe has actually LOADED — the honest "genuinely streaming"
  // signal gating the green-blink "Live" badge (not merely "we requested a URL").
  const [iframeConnected, setIframeConnected] = useState(false);
  // Mirror live state into a ref so the poll/unmount effects read the latest value
  // without re-subscribing on every change.
  const liveViewRef = useRef(liveView);
  liveViewRef.current = liveView;
  // The cid we've already attempted an auto-start for, so a doomed start (backend can't
  // run the stack) isn't retried every render. Reset when the session genuinely ends so
  // a fresh browse can re-start.
  const startAttemptRef = useRef<string | null>(null);

  // Tell the sandbox to tear the live-view stack down. Best-effort + fire-and-forget:
  // teardown should never block on (or error from) the call.
  const stopLiveStack = (conv: string) => {
    void agentSend("POST", `/conversations/${encodeURIComponent(conv)}/browser/live-stop`).catch(
      () => {},
    );
  };

  // Poll the side-effect-free readiness probe while the feature is enabled and there's a
  // cid. We keep polling EVEN while the view is open, so a session that ENDS (daemon gone
  // / sandbox reaped / unsupported backend) flips ready→false and we tear the view down +
  // revert to screenshots. The probe has no side-effects (no live_start, no port map).
  useEffect(() => {
    if (!liveBrowserEnabled || !cid) {
      setLiveReady(false);
      return;
    }
    let cancelled = false;
    let missStreak = 0;
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
        startAttemptRef.current = null; // allow a fresh auto-start if browsing resumes
      }
    };
    void probe();
    const id = setInterval(() => void probe(), 4000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [liveBrowserEnabled, cid]);

  // AUTO-START: enabled + a cid + genuinely streamable (live-ready true ⇒ the browser
  // session is up on a backend that can run the stack) + not already up/starting → start
  // the stack automatically, the same server path the old button used, with NO user
  // action. A failure falls back SILENTLY to screenshots (no banner, no false affordance).
  useEffect(() => {
    if (!liveBrowserEnabled || !cid || liveView || liveLoading || !liveReady) return;
    if (startAttemptRef.current === cid) return; // already tried for this session
    startAttemptRef.current = cid;
    let cancelled = false;
    setLiveLoading(true);
    void (async () => {
      try {
        const data = await agentGet<{ ready: boolean; novnc_path: string; port: number }>(
          `/conversations/${encodeURIComponent(cid)}/browser/live-url`,
        );
        // SECURITY: build the single-origin proxy URL client-side ({cid8}-6080.localhost).
        // The server intentionally never returns a raw sandbox host:port — that would
        // bypass the auth/cid-scoping proxy. previewHostUrl is the same helper the
        // dev-server preview uses.
        const base = previewHostUrl(cid, data.port, agentHttpBase());
        if (!cancelled && base) {
          setIframeConnected(false);
          setLiveView({ url: base, novnc_path: data.novnc_path, ownerCid: cid });
        }
      } catch {
        // SILENT fallback — the backend couldn't start the stack. Show screenshots; never
        // a "Live view unavailable" banner. startAttemptRef stays set so we don't hammer.
      } finally {
        if (!cancelled) setLiveLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [liveBrowserEnabled, cid, liveView, liveLoading, liveReady]);

  // Tear the OWNING conversation's stack down when the feature is disabled in Settings,
  // OR when the surface switches to a different conversation while a live view is open
  // (BrowserPane isn't keyed by cid, so we must stop liveView.ownerCid, not `cid`).
  useEffect(() => {
    if (!liveView) return;
    if (!liveBrowserEnabled || liveView.ownerCid !== cid) {
      stopLiveStack(liveView.ownerCid);
      setLiveView(null);
      setIframeConnected(false);
      startAttemptRef.current = null;
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
    const iframeSrc = `${liveView.url}${liveView.novnc_path}`;
    return (
      <div className="flex h-full min-h-0 flex-col">
        {Header}
        <div className="flex min-h-0 flex-1 flex-col">
          <iframe
            title="Live browser (noVNC)"
            src={iframeSrc}
            // The iframe genuinely LOADING is the truth behind the green-blink badge.
            onLoad={() => setIframeConnected(true)}
            // allow-same-origin is intentionally absent — the noVNC iframe must
            // NOT be able to reach this page's JS context (cross-origin isolation).
            // allow-scripts is needed for noVNC's WebSocket connection.
            sandbox="allow-scripts allow-forms"
            className="h-full w-full border-0"
            data-testid="novnc-iframe"
          />
        </div>
      </div>
    );
  }

  // No screenshot we can actually load (none captured, or no cid to fetch against)
  // → an honest empty state, never a broken <img>. (Auto-start failure also lands here:
  // a SILENT fallback to screenshots, never a "Live view unavailable" banner.)
  if (!hero || !cid) {
    return (
      <div className="flex h-full min-h-0 flex-col" data-screenshot-state="empty">
        {Header}
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
  cid,
  untrusted,
  onSteer,
}: {
  events: AgentEvent[];
  cid: string | null;
  untrusted: boolean;
  /** W-26 — steer the agent from the artifact-preview selection ("Discuss with
   * agent"). Undefined when steering isn't available (no false affordance). */
  onSteer?: (text: string) => void;
}) {
  const files = useMemo(() => deriveFiles(events), [events]);
  // §4.1 C-EDIT-1: inject selection agent for trusted (non-untrusted) iframes.
  const srcDoc = useMemo(
    () => deriveSrcDoc(files, untrusted ? undefined : SELECTION_AGENT_SCRIPT),
    [files, untrusted],
  );

  // §4.1 C-EDIT-1: ref + selection state for the artifact preview iframe.
  const artifactIframeRef = useRef<HTMLIFrameElement | null>(null);
  const {
    armed: artifactArmed,
    arm: artifactArm,
    disarm: artifactDisarm,
    selection: artifactSelection,
    walkUp: artifactWalkUp,
    resetSelection: artifactResetSelection,
  } = useElementSelect(artifactIframeRef, "null");

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
            <iframe
              ref={artifactIframeRef}
              title="Artifact preview"
              srcDoc={srcDoc}
              // untrusted (shared/imported run) → empty sandbox, no scripts: a script
              // here could reach this instance's open-CORS APIs. Trusted keeps allow-scripts.
              sandbox={untrusted ? "" : "allow-scripts"}
              className="h-full w-full border-0 bg-white"
            />
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
          <ArtifactsPane events={events} cid={cid} untrusted={untrusted} onSteer={onSteer} />
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
