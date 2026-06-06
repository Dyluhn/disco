/**
 * The execution canvas / Inspector (BoD §13.4, §13.5 Labs-style multi-pane): the agent's
 * work made VISIBLE beside the control pane, so this is never a "black box with no live
 * preview." Files + Terminal are REAL (derived from the event stream). Preview (live app
 * iframe) and Live view (noVNC) are honestly flagged as pending their backends — shown,
 * not faked (no false affordances).
 */

import { useMemo, useState } from "react";
import * as Tabs from "@radix-ui/react-tabs";
import { ExternalLink, FileCode2, MonitorPlay, SquareTerminal } from "lucide-react";
import { cn } from "@/lib/cn";
import { deriveFiles, deriveTerminal } from "@/lib/buildTrace";
import { useBuildPreview } from "@/hooks/useBuildPreview";
import type { AgentEvent, ConversationStatus } from "@/types/agent";

type TabId = "files" | "terminal" | "preview";

function FilesPane({ events }: { events: AgentEvent[] }) {
  const files = useMemo(() => deriveFiles(events), [events]);
  const [active, setActive] = useState(0);
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

function TerminalPane({ events }: { events: AgentEvent[] }) {
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

/** One phase-aware pane (replacing the redundant Preview + Live tabs). When the agent's
 * dev server is reachable (via the ACTIVE backend's port exposure — local direct, gVisor
 * over the tailnet), this IS the live interactive preview (an iframe of the real running
 * artifact). Until then it shows the phase-aware state + the honest reason. */
function PreviewPane({ status, cid }: { status: ConversationStatus; cid: string | null }) {
  const active = status === "RUNNING" || status === "WAITING_FOR_CONFIRMATION";
  const { data } = useBuildPreview(cid, active);

  if (data?.available && data.url) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        <div className="flex shrink-0 items-center justify-between gap-inline border-b border-hairline px-body py-hair">
          <span className="truncate font-mono text-[0.74rem] text-text-faint">{data.url}</span>
          <a
            href={data.url}
            target="_blank"
            rel="noreferrer"
            className="flex items-center gap-hair font-ui text-[0.74rem] text-text-muted transition-colors hover:text-text"
          >
            <ExternalLink className="size-3" aria-hidden /> Open
          </a>
        </div>
        <iframe
          title="Live preview"
          src={data.url}
          sandbox="allow-scripts allow-forms allow-same-origin allow-popups"
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

const TABS: { id: TabId; label: string; icon: typeof FileCode2; soon?: boolean }[] = [
  { id: "files", label: "Files", icon: FileCode2 },
  { id: "terminal", label: "Terminal", icon: SquareTerminal },
  { id: "preview", label: "Preview", icon: MonitorPlay, soon: true },
];

export function ExecutionCanvas({
  events,
  status,
  cid,
}: {
  events: AgentEvent[];
  status: ConversationStatus;
  cid: string | null;
}) {
  // sensible default: Terminal if anything ran, else Files.
  const initial: TabId = useMemo(
    () => (deriveTerminal(events).length > 0 ? "terminal" : "files"),
    // initial only — don't yank the user's tab as the stream grows
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );
  return (
    <Tabs.Root defaultValue={initial} className="flex h-full min-h-0 flex-col">
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
            {t.soon && (
              <span className="rounded-full border border-hairline px-1 text-[0.55rem] uppercase tracking-wide text-text-faint">
                soon
              </span>
            )}
          </Tabs.Trigger>
        ))}
      </Tabs.List>
      <div className="min-h-0 flex-1">
        <Tabs.Content value="files" className="h-full focus:outline-none">
          <FilesPane events={events} />
        </Tabs.Content>
        <Tabs.Content value="terminal" className="h-full focus:outline-none">
          <TerminalPane events={events} />
        </Tabs.Content>
        <Tabs.Content value="preview" className="h-full focus:outline-none">
          <PreviewPane status={status} cid={cid} />
        </Tabs.Content>
      </div>
    </Tabs.Root>
  );
}
