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
import { Download, FileCode2, FileSpreadsheet, FileText, Globe, Package, SquareTerminal } from "lucide-react";
import { cn } from "@/lib/cn";
import { deriveFiles, deriveSrcDoc, deriveTerminal, deriveLiveSignal } from "@/lib/buildTrace";
import type { WorkspaceFile } from "@/lib/buildTrace";
import { agentGet, agentHttpBase } from "@/api/client";
import { useElementSelect } from "@/hooks/useElementSelect";
import { SelectionOverlay } from "@/components/build/canvas/SelectionOverlay";
import { SELECTION_AGENT_SCRIPT } from "@/lib/selectionAgent";
import { useLiveBrowserConfig } from "@/hooks/useModels";
import type { AgentEvent, ConversationStatus } from "@/types/agent";

type TabId = "browser" | "artifacts" | "console";

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
      <span className="font-ui text-[0.74rem] text-text-faint italic">screenshot no longer available</span>
    );
  return (
    <img src={src} alt="browser screenshot" onError={() => setFailed(true)} className={className} />
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

  // Live browser (noVNC) state
  const { data: liveBrowserCfg } = useLiveBrowserConfig();
  const liveBrowserEnabled = liveBrowserCfg?.enabled ?? false;
  const [liveView, setLiveView] = useState<{ url: string; novnc_path: string } | null>(null);
  const [liveLoading, setLiveLoading] = useState(false);
  const [liveError, setLiveError] = useState<string | null>(null);

  const toggleLive = async () => {
    if (liveView) {
      setLiveView(null);
      return;
    }
    if (!cid) return;
    setLiveLoading(true);
    setLiveError(null);
    try {
      const data = await agentGet<{ url: string; novnc_path: string; port: number }>(
        `/conversations/${encodeURIComponent(cid)}/browser/live-url`,
      );
      setLiveView({ url: data.url, novnc_path: data.novnc_path });
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Failed to start live view";
      setLiveError(msg);
    } finally {
      setLiveLoading(false);
    }
  };

  const Header =
    url || driving || (liveBrowserEnabled && cid) ? (
      <div className="flex shrink-0 items-center justify-between gap-inline border-b border-hairline px-body py-hair">
        <span className="truncate font-mono text-[0.74rem] text-text-faint" title={url ?? undefined}>
          {url ?? "browser"}
        </span>
        <div className="flex shrink-0 items-center gap-inline">
          {driving && (
            <span className="flex shrink-0 items-center gap-hair font-ui text-[0.72rem] text-accent">
              <span className="size-1.5 animate-pulse rounded-full bg-accent" aria-hidden />
              driving…
            </span>
          )}
          {liveBrowserEnabled && cid && (
            <button
              type="button"
              onClick={toggleLive}
              disabled={liveLoading}
              aria-pressed={!!liveView}
              title={liveView ? "Close live view" : "Open live browser view (noVNC)"}
              className={cn(
                "flex shrink-0 items-center gap-hair rounded-control border px-inline py-px font-ui text-[0.72rem] transition-colors",
                liveView
                  ? "border-accent/50 bg-accent/10 text-accent"
                  : "border-hairline text-text-faint hover:border-hairline-strong hover:text-text",
                liveLoading && "opacity-60",
              )}
            >
              <span
                className={cn(
                  "size-1.5 rounded-full",
                  liveView ? "animate-pulse bg-accent" : "bg-text-faint",
                )}
                aria-hidden
              />
              {liveLoading ? "Starting…" : "Live"}
            </button>
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
        {liveError && (
          <p className="shrink-0 font-ui text-[0.78rem] text-warn px-body py-hair">
            Live view unavailable: {liveError}
          </p>
        )}
        <div className="flex min-h-0 flex-1 flex-col">
          <iframe
            title="Live browser (noVNC)"
            src={iframeSrc}
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
  // → an honest empty state, never a broken <img>.
  if (!hero || !cid) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        {Header}
        {liveError && (
          <p className="shrink-0 font-ui text-[0.78rem] text-warn px-body py-hair">
            Live view unavailable: {liveError}
          </p>
        )}
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
      {liveError && (
        <p className="shrink-0 font-ui text-[0.78rem] text-warn px-body py-hair">
          Live view unavailable: {liveError}
        </p>
      )}
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
}: {
  events: AgentEvent[];
  cid: string | null;
  untrusted: boolean;
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

const TABS: { id: TabId; label: string; icon: typeof Globe }[] = [
  { id: "browser", label: "Browser", icon: Globe },
  { id: "artifacts", label: "Artifacts", icon: Package },
  { id: "console", label: "Console", icon: SquareTerminal },
];

export function AgentCanvas({
  events,
  status,
  cid,
  untrusted = false,
}: {
  events: AgentEvent[];
  status: ConversationStatus;
  cid: string | null;
  /** Third-party events (shared/imported run) → harden the artifact preview iframe. */
  untrusted?: boolean;
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

  return (
    <Tabs.Root
      value={tab}
      onValueChange={(v) => setTab(v as TabId)}
      className="flex h-full min-h-0 flex-col"
    >
      <Tabs.List className="flex shrink-0 items-center gap-px border-b border-hairline px-inline">
        {TABS.map((t) => (
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
          <ArtifactsPane events={events} cid={cid} untrusted={untrusted} />
        </Tabs.Content>
        <Tabs.Content value="console" className="h-full focus:outline-none">
          <ConsolePane events={events} />
        </Tabs.Content>
      </div>
    </Tabs.Root>
  );
}
