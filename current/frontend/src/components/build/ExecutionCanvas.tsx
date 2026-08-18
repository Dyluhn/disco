/**
 * The execution canvas / Inspector (BoD §13.4, §13.5 Labs-style multi-pane): the agent's
 * work made VISIBLE beside the control pane, so this is never a "black box with no live
 * preview." Files + Terminal are REAL (derived from the event stream). Preview (live app
 * iframe) and Live view (noVNC) are honestly flagged as pending their backends — shown,
 * not faked (no false affordances).
 *
 * This file is the parent composer: it owns the tab state + the shared
 * `useSessions` / `useBuildPreview` polling (which must live ABOVE the panes
 * because Radix unmounts inactive tab content) and threads the derived data into
 * the cohesive pane child components under `./canvas/`.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import * as Tabs from "@radix-ui/react-tabs";
import { FileCode2, Gauge, MonitorPlay, SquareTerminal } from "lucide-react";
import { cn } from "@/lib/cn";
import { deriveFiles, deriveSrcDoc, deriveTerminal } from "@/lib/buildTrace";
import { useSnapToAction } from "@/lib/useSnapToAction";
import { useBuildPreview } from "@/hooks/useBuildPreview";
import { useProjectManifest } from "@/hooks/useProjects";
import { useSessions } from "@/hooks/useSessions";
import type { StreamingFile } from "@/hooks/useBuildStream";
import type { ElementMentionPayload } from "@/lib/elementMention";
import type { SelectionRef } from "@/lib/selectionBridge";
import type { AgentEvent, ConversationStatus } from "@/types/agent";
import { FilesPane } from "./canvas/FilesPane";
import { TerminalPane } from "./canvas/TerminalPane";
import { PreviewPane } from "./canvas/PreviewPane";
import { CockpitPane } from "./canvas/CockpitPane";
import { previewActiveStatus } from "./canvas/previewPaneParts/helpers";

type TabId = "files" | "terminal" | "preview" | "cockpit";

const TABS: { id: TabId; label: string; icon: typeof FileCode2 }[] = [
  { id: "files", label: "Files", icon: FileCode2 },
  { id: "terminal", label: "Terminal", icon: SquareTerminal },
  { id: "preview", label: "Preview", icon: MonitorPlay },
  { id: "cockpit", label: "Cockpit", icon: Gauge },
];

export function ExecutionCanvas({
  events,
  status,
  cid,
  streamingFile = null,
  untrusted = false,
  onSteer,
  onSelectionEdit,
  onElementMention,
}: {
  events: AgentEvent[];
  status: ConversationStatus;
  cid: string | null;
  streamingFile?: StreamingFile | null;
  /** Third-party events (shared/imported run) → harden the preview iframe. */
  untrusted?: boolean;
  /** Steer the agent (free-form) from the preview-pane "Discuss" affordance.
   * Undefined when steering isn't available (no conversation / static view). */
  onSteer?: (text: string) => void;
  /** P8 click-to-edit — submit a selected element's ref + change to the host. */
  onSelectionEdit?: (
    ref: SelectionRef,
    instruction: string,
    humanLabel?: string,
  ) => void;
  /** Element mention — attach the next clicked preview element to the next chat message. */
  onElementMention?: (payload: ElementMentionPayload) => void;
}) {
  // Recognize the canonical app handoff independently from later file
  // deliverables. Runtime apps may hand off a directory or server entry rather
  // than an HTML file, so srcdoc eligibility is not the Preview authority.
  function _hasPreviewableApp(evts: AgentEvent[]): boolean {
    const files = deriveFiles(evts);
    return (
      evts.some((event) => event.kind === "deliverable" && event.artifact_kind === "app") ||
      deriveSrcDoc(files) != null ||
      files.some((f) => !f.content && /\.html$/i.test(f.path))
    );
  }

  // Cluster 5: when there's a renderable artifact, Preview is the hero — the
  // user's first instinct should be to WATCH it build, not read logs. Else
  // Terminal if anything ran, else Files.
  const initial: TabId = useMemo(
    () =>
      _hasPreviewableApp(events)
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
  const { snapToAction, setSnapToAction } = useSnapToAction();
  const wasStreaming = useRef(false);
  useEffect(() => {
    const now = streamingFile != null;
    if (snapToAction && now && !wasStreaming.current) setTab("files");
    wasStreaming.current = now;
  }, [snapToAction, streamingFile]);
  // Auto-switch to Preview at the capstone: when the run FINISHES and there's a
  // renderable artifact, pull focus to the result (the deliverable handoff — the
  // user wants to SEE the finished app, not stare at the file tree). Only on the
  // rising edge into FINISHED, so it never yanks the user mid-run or repeatedly.
  const wasFinished = useRef(false);
  useEffect(() => {
    const finished = status === "FINISHED";
    if (finished && !wasFinished.current && _hasPreviewableApp(events)) {
      setTab("preview");
    }
    wasFinished.current = finished;
  }, [status, events]);

  const active = previewActiveStatus(status);

  // Sessions polling lives HERE, not in TerminalPane: Radix unmounts inactive
  // tab content, and the tab-label busy dot must keep updating while the user
  // is on Files/Preview (that's the dot's entire job — pane-local polling
  // would freeze it at its last-seen value on switch-away). The cheap
  // /sessions list polls whenever the build is active; the heavier /view
  // capture-pane polls only while the Terminal tab is actually shown. The
  // Cockpit tab also needs /view (to render the selected session's live
  // capture-pane tail) — same cheap reuse, just one more "is view visible"
  // caller.
  const cockpitWantsView = tab === "cockpit";
  const { sessions, selectedName, setSelectedName, view } = useSessions(
    cid,
    active,
    tab === "terminal" || cockpitWantsView,
  );
  // The Cockpit also surfaces bound-port data from the same useBuildPreview
  // the Preview pane uses — shared query cache, no extra fetch.
  const previewQuery = useBuildPreview(cid, active);
  const manifestQuery = useProjectManifest(cid);
  const manifestFiles = manifestQuery.data?.files ?? [];
  const anySessionBusy = sessions.some((s) => s.busy);

  return (
    <Tabs.Root
      value={tab}
      onValueChange={(v) => setTab(v as TabId)}
      // #26: expose the active tab so a streaming test can assert the watch-it-write
      // rising-edge focus (a file write pulls focus to "files") deterministically,
      // without reaching into Radix internals.
      data-active-tab={tab}
      className="flex h-full min-h-0 flex-col"
    >
      <div className="flex shrink-0 items-center overflow-x-auto border-b border-hairline px-inline">
        <Tabs.List className="flex min-w-0 shrink-0 items-center gap-px">
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
        <button
          type="button"
          role="switch"
          aria-checked={snapToAction}
          aria-label="Snap to action — automatically show Files when the agent starts writing"
          title="Automatically switch to Files when the agent starts writing. Turn this off to keep the tab you selected."
          data-disco-control="build.snap-to-action-toggle"
          onClick={() => setSnapToAction(!snapToAction)}
          className={cn(
            "ml-auto flex shrink-0 items-center gap-hair rounded-full border px-inline py-px font-ui text-[0.7rem] transition-colors",
            snapToAction
              ? "border-accent/50 bg-accent/5 text-accent"
              : "border-hairline text-text-faint hover:text-text-muted",
          )}
        >
          <span
            aria-hidden
            className={cn(
              "size-1.5 rounded-full",
              snapToAction ? "bg-accent" : "bg-text-faint",
            )}
          />
          Snap to action
        </button>
      </div>
      <div className="min-h-0 flex-1">
        <Tabs.Content value="files" className="h-full focus:outline-none">
          <FilesPane
            events={events}
            streamingFile={streamingFile}
            manifestFiles={manifestFiles}
          />
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
          <PreviewPane
            status={status}
            cid={cid}
            events={events}
            untrusted={untrusted}
            onSteer={onSteer}
            onSelectionEdit={onSelectionEdit}
            onElementMention={onElementMention}
          />
        </Tabs.Content>
        <Tabs.Content value="cockpit" className="h-full focus:outline-none">
          <CockpitPane
            events={events}
            sessions={sessions}
            selectedName={selectedName}
            setSelectedName={setSelectedName}
            view={view}
            data={previewQuery.data}
          />
        </Tabs.Content>
      </div>
    </Tabs.Root>
  );
}
