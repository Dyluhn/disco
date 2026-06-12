/**
 * The execution canvas / Inspector (BoD §13.4, §13.5 Labs-style multi-pane): the agent's
 * work made VISIBLE beside the control pane, so this is never a "black box with no live
 * preview." Files + Terminal are REAL (derived from the event stream). Preview (live app
 * iframe) and Live view (noVNC) are honestly flagged as pending their backends — shown,
 * not faked (no false affordances).
 */

import { useEffect, useMemo, useRef, useState } from "react";
import * as Tabs from "@radix-ui/react-tabs";
import { useQueryClient } from "@tanstack/react-query";
import { ExternalLink, FileCode2, MonitorPlay, PenLine, RotateCw, SquareTerminal } from "lucide-react";
import { cn } from "@/lib/cn";
import { deriveFiles, deriveSrcDoc, deriveTerminal } from "@/lib/buildTrace";
import { previewHostUrl } from "@/api/client";
import { restartPreview } from "@/api/agent";
import { useBuildPreview } from "@/hooks/useBuildPreview";
import { useSessions } from "@/hooks/useSessions";
import type { SessionInfo, SessionView } from "@/hooks/useSessions";
import type { StreamingFile } from "@/hooks/useBuildStream";
import type { AgentEvent, ConversationStatus } from "@/types/agent";

type TabId = "files" | "terminal" | "preview";

/** The live watch-it-write pane: the file the driver is composing RIGHT NOW,
 *  content growing with a blinking cursor. Auto-scrolls to follow the tail so the
 *  newest line is always visible (the whole point — see it isn't hung). */
function StreamingFileView({ file }: { file: StreamingFile }) {
  const tailRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    // follow the writing edge; cheap because content only ever appends
    tailRef.current?.scrollIntoView({ block: "end" });
  }, [file.content]);
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center justify-between border-b border-accent/30 bg-accent/5 px-body py-hair font-mono text-[0.74rem]">
        <span className="flex items-center gap-hair text-accent">
          <PenLine className="size-3 shrink-0 animate-pulse" aria-hidden />
          <span className="truncate text-text">{file.path || "writing…"}</span>
        </span>
        <span className="text-text-faint">{file.content.length} B · writing</span>
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        <pre className="whitespace-pre-wrap px-body py-inline font-mono text-[0.78rem] leading-relaxed text-text">
          {file.content}
          <span className="ml-px inline-block animate-pulse text-accent">▋</span>
          <div ref={tailRef} />
        </pre>
      </div>
    </div>
  );
}

function FilesPane({
  events,
  streamingFile,
}: {
  events: AgentEvent[];
  streamingFile: StreamingFile | null;
}) {
  const files = useMemo(() => deriveFiles(events), [events]);
  const [active, setActive] = useState(0);
  // While a write is streaming, it is the hero — show the live buffer regardless
  // of which file was selected. The final ActionEvent retires it (streamingFile →
  // null) and the file then appears in the list as a normal, complete entry.
  if (streamingFile) return <StreamingFileView file={streamingFile} />;
  if (files.length === 0)
    return <Empty>No files written yet. Files the agent creates appear here.</Empty>;
  const file = files[Math.min(active, files.length - 1)];
  return (
    <div className="flex h-full min-h-0">
      <ul className="w-44 shrink-0 overflow-y-auto border-r border-hairline py-hair">
        {files.map((f, i) => (
          <li key={f.path}>
            <button
              type="button"
              onClick={() => setActive(i)}
              className={cn(
                "flex w-full items-center gap-hair truncate px-inline py-hair text-left font-mono text-[0.76rem] transition-colors",
                i === active ? "bg-surface-2 text-text" : "text-text-muted hover:text-text",
              )}
            >
              <FileCode2 className="size-3 shrink-0" aria-hidden />
              <span className="truncate">{f.path}</span>
            </button>
          </li>
        ))}
      </ul>
      <div className="min-w-0 flex-1 overflow-auto">
        <div className="flex items-center justify-between border-b border-hairline px-body py-hair font-mono text-[0.74rem] text-text-faint">
          <span>{file.path}</span>
          <span>{file.bytes} B</span>
        </div>
        <pre className="overflow-auto whitespace-pre-wrap px-body py-inline font-mono text-[0.78rem] leading-relaxed text-text">
          {file.content || "(empty)"}
        </pre>
      </div>
    </div>
  );
}

function TerminalHistoryView({ events }: { events: AgentEvent[] }) {
  const entries = useMemo(() => deriveTerminal(events), [events]);
  if (entries.length === 0)
    return <Empty>No commands run yet. Shell + code output streams here.</Empty>;
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

function TerminalPane({
  events,
  sessions,
  selectedName,
  setSelectedName,
  view,
}: {
  events: AgentEvent[];
  sessions: SessionInfo[];
  selectedName: string | null;
  setSelectedName: (name: string) => void;
  view: SessionView | null;
}) {
  // Purely presentational: the useSessions hook lives in ExecutionCanvas (NOT
  // here) because Radix unmounts inactive tab content — if polling lived in
  // this pane, the tab-label busy dot could never light up while the user is
  // on another tab (its whole purpose), and would freeze stale on switch-away.

  // Near-bottom autoscroll: only follow the tail when the user hasn't scrolled away.
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const atBottomRef = useRef(true);
  function onScroll() {
    const el = scrollRef.current;
    if (!el) return;
    atBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  }
  useEffect(() => {
    if (atBottomRef.current && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [view?.content]);

  if (sessions.length === 0) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        <div className="min-h-0 flex-1">
          <TerminalHistoryView events={events} />
        </div>
        <div className="shrink-0 border-t border-hairline px-body py-hair font-mono text-[0.72rem] text-text-faint">
          history (no live sessions)
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col bg-[oklch(0.15_0.005_260)]">
      {/* Session chips */}
      <div className="flex shrink-0 items-center gap-hair overflow-x-auto border-b border-white/10 px-body py-hair">
        {sessions.map((s) => (
          <button
            key={s.name}
            type="button"
            onClick={() => setSelectedName(s.name)}
            className={cn(
              "flex shrink-0 items-center gap-hair rounded px-inline py-px font-mono text-[0.72rem] transition-colors",
              s.name === selectedName
                ? "bg-white/10 text-text"
                : "text-text-faint hover:text-text",
            )}
          >
            <span
              className={cn(
                "size-1.5 rounded-full",
                s.busy
                  ? "animate-pulse bg-[oklch(0.75_0.18_150)]"
                  : "bg-[oklch(0.55_0.08_150)]",
              )}
              aria-label={s.busy ? "running" : "idle"}
            />
            {s.name}
          </button>
        ))}
      </div>
      {/* Live content pane */}
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="min-h-0 flex-1 overflow-auto px-body py-inline"
      >
        <pre className="whitespace-pre-wrap font-mono text-[0.78rem] leading-relaxed text-[oklch(0.78_0.005_260)]">
          {view?.content ?? ""}
        </pre>
      </div>
    </div>
  );
}

/** One phase-aware pane (replacing the redundant Preview + Live tabs). When the agent's
 * dev server is reachable (via the ACTIVE backend's port exposure — local direct, gVisor
 * over the tailnet), this IS the live interactive preview (an iframe of the real running
 * artifact). Until then it shows the phase-aware state + the honest reason. */
function PreviewPane({
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

  // PRIORITY (the fix): default to the RENDERED view, because the backend's bare
  // `python -m http.server` shows a useless directory LISTING (file paths) when
  // there's no index.html at the served root — which is most of the time. The
  // rendered view shows the real HTML instead. A live dev server (a real bundler
  // app, e.g. Vite) is one click away via the toggle / Open, and is used
  // automatically when there's no renderable file to show.
  const [mode, setMode] = useState<"rendered" | "live">("rendered");
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
          </div>
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

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-full items-center justify-center px-body text-center font-ui text-[0.82rem] text-text-faint">
      {children}
    </div>
  );
}

const TABS: { id: TabId; label: string; icon: typeof FileCode2 }[] = [
  { id: "files", label: "Files", icon: FileCode2 },
  { id: "terminal", label: "Terminal", icon: SquareTerminal },
  { id: "preview", label: "Preview", icon: MonitorPlay },
];

export function ExecutionCanvas({
  events,
  status,
  cid,
  streamingFile = null,
  untrusted = false,
}: {
  events: AgentEvent[];
  status: ConversationStatus;
  cid: string | null;
  streamingFile?: StreamingFile | null;
  /** Third-party events (shared/imported run) → harden the preview iframe. */
  untrusted?: boolean;
}) {
  // Cluster 5: when there's a renderable artifact, Preview is the hero — the
  // user's first instinct should be to WATCH it build, not read logs. Else
  // Terminal if anything ran, else Files.
  const initial: TabId = useMemo(
    () =>
      deriveSrcDoc(deriveFiles(events)) != null
        ? "preview"
        : deriveTerminal(events).length > 0
          ? "terminal"
          : "files",
    // initial only — don't yank the user's tab as the stream grows
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );
  // Controlled tab so a starting write can pull focus to Files (watch-it-write is
  // the hero moment). We only force the switch on the streaming RISING edge — once
  // the user clicks away mid-write, we respect it (no repeated yank per delta).
  const [tab, setTab] = useState<TabId>(initial);
  const wasStreaming = useRef(false);
  useEffect(() => {
    const now = streamingFile != null;
    if (now && !wasStreaming.current) setTab("files");
    wasStreaming.current = now;
  }, [streamingFile]);
  // Auto-switch to Preview at the capstone: when the run FINISHES and there's a
  // renderable artifact, pull focus to the result (the deliverable handoff — the
  // user wants to SEE the finished app, not stare at the file tree). Only on the
  // rising edge into FINISHED, so it never yanks the user mid-run or repeatedly.
  const wasFinished = useRef(false);
  useEffect(() => {
    const finished = status === "FINISHED";
    if (finished && !wasFinished.current && deriveSrcDoc(deriveFiles(events)) != null) {
      setTab("preview");
    }
    wasFinished.current = finished;
  }, [status, events]);

  const active =
    status === "RUNNING" ||
    status === "WAITING_FOR_CONFIRMATION" ||
    status === "FINISHED" ||
    status === "STUCK";

  // Sessions polling lives HERE, not in TerminalPane: Radix unmounts inactive
  // tab content, and the tab-label busy dot must keep updating while the user
  // is on Files/Preview (that's the dot's entire job — pane-local polling
  // would freeze it at its last-seen value on switch-away). The cheap
  // /sessions list polls whenever the build is active; the heavier /view
  // capture-pane polls only while the Terminal tab is actually shown.
  const { sessions, selectedName, setSelectedName, view } = useSessions(
    cid,
    active,
    tab === "terminal",
  );
  const anySessionBusy = sessions.some((s) => s.busy);

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
            {t.id === "files" && streamingFile && (
              <span
                className="size-1.5 animate-pulse rounded-full bg-accent"
                aria-label="writing"
              />
            )}
            {t.id === "terminal" && anySessionBusy && (
              <span
                className="size-1.5 animate-pulse rounded-full bg-accent"
                aria-label="session running"
              />
            )}
          </Tabs.Trigger>
        ))}
      </Tabs.List>
      <div className="min-h-0 flex-1">
        <Tabs.Content value="files" className="h-full focus:outline-none">
          <FilesPane events={events} streamingFile={streamingFile} />
        </Tabs.Content>
        <Tabs.Content value="terminal" className="h-full focus:outline-none">
          <TerminalPane
            events={events}
            sessions={sessions}
            selectedName={selectedName}
            setSelectedName={setSelectedName}
            view={view}
          />
        </Tabs.Content>
        <Tabs.Content value="preview" className="h-full focus:outline-none">
          <PreviewPane status={status} cid={cid} events={events} untrusted={untrusted} />
        </Tabs.Content>
      </div>
    </Tabs.Root>
  );
}
