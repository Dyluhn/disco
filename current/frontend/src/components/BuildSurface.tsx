/**
 * The Build (Agent) surface — the split-pane "Sidecar" (BoD §13.4/§13.5/§8.2): a control
 * pane (the Project-Manager view: plain-language Activity Feed + confidence gradient, the
 * confirmation gate, the Steering Wheel, status + the kill switch) BESIDE a live execution
 * canvas/Inspector (Files · Terminal · Preview · Live). The technical detail lives in the
 * canvas; the left pane stays a readable narrative — never a black box, never a debug log.
 *
 * Data-flow discipline: reads only the useBuild() hook; never fetches directly. Amendment
 * A3: the sandbox-backend/title lookup and the preview-bootstrap URL both go through
 * `@/api/agent` (via the `./buildSurface` hooks/components below), never `@/api/client`.
 *
 * Decomposition (PKG-12-FE-BUILD): this file used to be one 793-line, 680-logical-line,
 * complexity-88 component. The pre-start empty state, the header, the activity feed (+ its
 * ERROR/plan-gate/running-activity branches), and the gate/composer footer regions now live
 * as child components under `./buildSurface/`; the feed auto-scroll effects, the element-
 * mention relay, the status-derived gating flags, and the conversation-summary fetch are
 * custom hooks in the same directory. This file composes them — see each child file's own
 * header comment for the rationale on *why* it was split out that way.
 */

import { useEffect, useMemo, useState } from "react";
import { useBuild } from "@/hooks/useBuild";
import { useBuildNotifications } from "@/hooks/useBuildNotifications";
import {
  deriveActivity,
  deriveDeliverable,
  deriveLiveSignal,
  latestAgentMessage,
} from "@/lib/buildTrace";
import { useReplay } from "@/lib/useReplay";
import { isolationForBackend } from "@/lib/isolation";
import { publishRunStatus } from "@/lib/runStatusBridge";
import { useVerboseAgentChat } from "@/lib/useVerboseAgentChat";
import { useDownloadProject, useExportManifest, useProjectRelease } from "@/hooks/useProjects";
import { ResizableSplit } from "@/components/build/ResizableSplit";
import { ExecutionCanvas } from "@/components/build/ExecutionCanvas";
import { AgentCanvas } from "@/components/build/AgentCanvas";
import { ElementMentionChip } from "@/components/build/ElementMentionChip";
import { BuildEmptyState } from "./buildSurface/BuildEmptyState";
import { BuildSurfaceHeader } from "./buildSurface/BuildSurfaceHeader";
import { BuildActivityFeed } from "./buildSurface/BuildActivityFeed";
import { BuildActivityFeedContent } from "./buildSurface/BuildActivityFeedContent";
import { BuildDeliverableSection } from "./buildSurface/BuildDeliverableSection";
import { BuildDecisionGates } from "./buildSurface/BuildDecisionGates";
import { BuildAskGate } from "./buildSurface/BuildAskGate";
import { BuildComposerSection } from "./buildSurface/BuildComposerSection";
import { useConversationSummary } from "./buildSurface/useConversationSummary";
import { useFeedAutoScroll } from "./buildSurface/useFeedAutoScroll";
import { useElementMentionRelay } from "./buildSurface/useElementMentionRelay";
import { useBuildSurfaceFlags } from "./buildSurface/useBuildSurfaceFlags";
import type { FramingCopy } from "./buildSurface/types";

/** This component backs two surfaces over shared UI/runtime machinery: "build"
 * (software) and "agent" (general tasks). Their backend prompts and completion
 * policy differ, while the gate, sandbox, persistence, and UI state model remain
 * shared. The default is "build", so existing Build call sites stay unchanged. */
export type BuildFraming = "build" | "agent";

const FRAMING: Record<BuildFraming, FramingCopy> = {
  build: {
    placeholder: "Describe what you want the agent to build or do…",
    replanPlaceholder: "Plan a change to this build…",
    startError: "Couldn't start the build — the server didn't respond. Try again.",
    heroTitle: "Build",
    heroSubtitle: "Real software, live preview.",
  },
  agent: {
    placeholder: "Describe a task for the agent to carry out…",
    replanPlaceholder: "Plan a change to this task…",
    startError: "Couldn't start the agent — the server didn't respond. Try again.",
    heroTitle: "Agent",
    heroSubtitle: "Hands-on tasks and workflows.",
  },
};

export function BuildSurface({
  resumeCid,
  framing = "build",
  seedTask,
  seedContext,
}: {
  resumeCid?: string | null;
  framing?: BuildFraming;
  seedTask?: string | null;
  seedContext?: string | null;
} = {}) {
  const b = useBuild(resumeCid, framing, seedTask, seedContext);
  const copy = FRAMING[framing];
  const [draft, setDraft] = useState("");

  // BP-15/W-01: the real sandbox backend name + the stored (auto-titled) clean
  // conversation title, fetched once the conversation id is known (Amendment A3
  // — routed through @/api/agent, never @/api/client directly).
  const { sandboxBackend, storedTitle } = useConversationSummary(b.cid);
  const download = useDownloadProject();
  const exportManifest = useExportManifest();
  // WO-9: the release verdict drives the capability-only Self-host panel in the
  // finished-handoff region. Gated on FINISHED (like the DeliverablePanel) so the
  // fetch only fires once a persisted snapshot exists to assess; the panel renders
  // purely from this data, so Build and Agent get the IDENTICAL UI by construction.
  const release = useProjectRelease(b.status === "FINISHED" ? (b.cid ?? null) : null);

  const { verbose: verboseChat } = useVerboseAgentChat();
  const { isReplaying, steerable, settled, collapseFeed, draftingPlan, terminalIncomplete } =
    useBuildSurfaceFlags(b, verboseChat);

  const replay = useReplay(b.events, !isReplaying);
  const visibleEvents = useMemo(
    () => (isReplaying ? b.events.slice(0, replay.position) : b.events),
    [b.events, isReplaying, replay.position],
  );
  const activity = useMemo(
    () => deriveActivity(visibleEvents, b.pendingActionId, b.status),
    [visibleEvents, b.pendingActionId, b.status],
  );
  const liveSignal = useMemo(() => deriveLiveSignal(b.events, b.status), [b.events, b.status]);
  const finalMessage = useMemo(() => latestAgentMessage(visibleEvents), [visibleEvents]);
  const deliverable = useMemo(() => deriveDeliverable(visibleEvents), [visibleEvents]);

  // On resume, wait for the canonical stored title. Replayed events can arrive
  // before the state request; rendering their raw first message in that gap
  // briefly exposed hidden build briefs and user prompt markup in the H1.
  const taskLabel = useMemo(() => {
    if (b.task !== "(resumed)") return b.task; // fresh run — unchanged
    if (storedTitle && storedTitle.trim()) return storedTitle.trim();
    return "Resumed project";
  }, [b.task, storedTitle]);

  // Attention when tabbed away: badge the title + (best-effort) OS-notify when the
  // run finishes or needs the user while the tab is hidden. Use the clean taskLabel
  // (stored title on resume) so the tab badge isn't the "(resumed)" sentinel.
  useBuildNotifications(b.status, taskLabel);

  // Gap #4: publish this surface's live run status to the W6 E2E bridge so the
  // harness can await RUNNING / AWAITING_* / FINISHED. (AgentSurface delegates to
  // BuildSurface, so this covers both build and agent.) Clear on unmount.
  useEffect(() => {
    publishRunStatus(b.status);
    return () => publishRunStatus(null);
  }, [b.status]);

  const { feedScrollRef, onFeedScroll, showScrollDown, scrollFeedToBottom } = useFeedAutoScroll(
    b.status,
    deliverable?.id ?? null,
    activity.length,
    b.streamingFile?.content,
  );

  const { elementMention, setElementMention, steerWithMention, requestPlanWithMention } =
    useElementMentionRelay(b);
  const elementMentionChip = (
    <ElementMentionChip mention={elementMention} onRemove={() => setElementMention(null)} />
  );

  if (!b.started) {
    return <BuildEmptyState framing={framing} copy={copy} b={b} draft={draft} setDraft={setDraft} />;
  }

  const isolation = isolationForBackend(sandboxBackend);

  // ── Control pane (chat) — the Project-Manager view ─────────────────────
  const chatPane = (
    <>
      <BuildSurfaceHeader
        b={b}
        isolation={isolation}
        taskLabel={taskLabel}
        download={download}
        onDownloadClick={() =>
          b.cid && download.mutate({ id: b.cid, binding: null, title: taskLabel })
        }
        framing={framing}
      />
      <BuildActivityFeed
        isReplaying={isReplaying}
        replay={replay}
        eventsLength={b.events.length}
        feedScrollRef={feedScrollRef}
        onFeedScroll={onFeedScroll}
        showScrollDown={showScrollDown}
        scrollFeedToBottom={scrollFeedToBottom}
      >
        <BuildActivityFeedContent
          b={b}
          activity={activity}
          liveSignal={liveSignal}
          finalMessage={finalMessage}
          draftingPlan={draftingPlan}
          collapseFeed={collapseFeed}
          framing={framing}
        />
      </BuildActivityFeed>

      {/* gate · steer · re-plan — pinned under the feed. UI-17: on a finished
          run this region is tall (deliverable card + self-host + composer +
          schedules) and the chat pane clips its overflow at `lg`. Let it
          shrink and scroll ITSELF there, so it neither starves the Activity
          section above it (which then painted over this card) nor hides its
          own bottom past the clip. Below `lg` the pane still overflows to the
          ordinary page scroll — unchanged. */}
      <div
        data-testid="build-pane-footer"
        className="flex flex-col gap-inline border-t border-hairline px-body py-inline lg:min-h-0 lg:overflow-y-auto"
      >
        <BuildDeliverableSection
          b={b}
          deliverable={deliverable}
          download={download}
          exportManifest={exportManifest}
          release={release}
          downloadTitle={taskLabel}
        />
        <BuildDecisionGates b={b} />
        <BuildAskGate b={b} finalMessage={finalMessage} />
        <BuildComposerSection
          b={b}
          copy={copy}
          steerable={steerable}
          settled={settled}
          terminalIncomplete={terminalIncomplete}
          steerWithMention={steerWithMention}
          requestPlanWithMention={requestPlanWithMention}
          elementMentionChip={elementMentionChip}
          framing={framing}
        />
      </div>
    </>
  );

  return (
    <ResizableSplit
      chat={chatPane}
      inspector={
        // Build → the software IDE inspector (Files/Terminal/Preview). Agent → the
        // lighter operator canvas (Browser/Artifacts/Console). Build is unchanged.
        framing === "agent" ? (
          <AgentCanvas
            events={visibleEvents}
            status={b.status}
            cid={b.cid}
            // W-26 — steer the agent from the artifact-preview "Discuss with agent"
            // action. Same wire as the steer composer; only when steering is available.
            onSteer={steerable ? steerWithMention : undefined}
          />
        ) : (
          <ExecutionCanvas
            events={visibleEvents}
            status={b.status}
            cid={b.cid}
            streamingFile={b.streamingFile}
            // Discuss affordance → free-form steer. Same wire as the steer composer.
            onSteer={steerable ? steerWithMention : undefined}
            // P8 click-to-edit: the preview-pane edit box submits the selected
            // element's typed ref + change; the host scopes the targeted edit.
            onSelectionEdit={steerable ? b.selectionEdit : undefined}
            onElementMention={setElementMention}
          />
        )
      }
    />
  );
}
