/**
 * The Build (Agent) surface — the split-pane "Sidecar" (BoD §13.4/§13.5/§8.2): a control
 * pane (the Project-Manager view: plain-language Activity Feed + confidence gradient, the
 * confirmation gate, the Steering Wheel, status + the kill switch) BESIDE a live execution
 * canvas/Inspector (Files · Terminal · Preview · Live). The technical detail lives in the
 * canvas; the left pane stays a readable narrative — never a black box, never a debug log.
 *
 * Data-flow discipline: reads only the useBuild() hook; never fetches.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useBuild } from "@/hooks/useBuild";
import { useBuildNotifications } from "@/hooks/useBuildNotifications";
import { cn } from "@/lib/cn";
import {
  deriveActivity,
  deriveDeliverable,
  deriveLiveSignal,
  firstUserTask,
  latestAgentMessage,
} from "@/lib/buildTrace";
import { useReplay } from "@/lib/useReplay";
import { isolationForBackend } from "@/lib/isolation";
import { agentFetch, agentLive, pathPreviewBootstrapUrl } from "@/api/client";
import { EmptyState, ErrorState } from "@/components/states";
import { Markdown } from "@/components/Markdown";
import { QueryInput } from "@/components/QueryInput";
import { publishRunStatus } from "@/lib/runStatusBridge";
import { ChevronDown, Download, Loader2 } from "lucide-react";
import { prependElementMention } from "@/lib/elementMention";
import type { ElementMentionPayload } from "@/lib/elementMention";
import { useDownloadProject, useExportManifest } from "@/hooks/useProjects";
import { ActivityFeed } from "@/components/build/ActivityFeed";
import { AgentStageCard } from "@/components/build/AgentStageCard";
import { useVerboseAgentChat } from "@/lib/useVerboseAgentChat";
import { LiveSignalBar } from "@/components/build/LiveSignalBar";
import { ResizableSplit } from "@/components/build/ResizableSplit";
import { AgentStatusBar } from "@/components/build/AgentStatusBar";
import { BuildModelPicker } from "@/components/build/BuildModelPicker";
import { ConfirmationPanel } from "@/components/build/ConfirmationPanel";
import { ExecutionCanvas } from "@/components/build/ExecutionCanvas";
import { AgentCanvas } from "@/components/build/AgentCanvas";
import { ConnectionsStrip } from "@/components/build/ConnectionsStrip";
import { PlanPanel } from "@/components/build/PlanPanel";
import { DeliverablePanel } from "@/components/build/DeliverablePanel";
import { ElementMentionChip } from "@/components/build/ElementMentionChip";
import { SuggestionChips } from "@/components/SuggestionChips";
import { SteerInput } from "@/components/build/SteerInput";
import { UploadComposer } from "@/components/build/BuildSurface";
import { ImportProjectDialog } from "@/components/build/ImportProjectDialog";
import { AlternativesGate } from "@/components/build/AlternativesGate";
import { AskPanel } from "@/components/build/AskPanel";
import { ClarifyPanel } from "@/components/build/ClarifyPanel";
import { QuestionsV2Panel } from "@/components/build/QuestionsV2Panel";
import { ReplayScrubber } from "@/components/build/ReplayScrubber";
import { ScheduleSection } from "@/components/settings/ScheduleSection";
import { openFreshPreview } from "@/lib/previewLaunch";
import { useToast } from "@/components/toastApi";

/** This surface backs two framings of the SAME agent machinery: "build" (software)
 * and "agent" (general tasks). Only presentational strings differ; everything else
 * — loop, gate, sandbox, persistence — is identical, so the agent surface reuses
 * this component via a thin wrapper rather than a forked copy. The default is
 * "build", so every existing call site renders byte-identically. */
export type BuildFraming = "build" | "agent";

const FRAMING: Record<
  BuildFraming,
  {
    placeholder: string;
    replanPlaceholder: string;
    startError: string;
    heroTitle: string;
    heroSubtitle: string;
  }
> = {
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

function preferredScrollBehavior(): ScrollBehavior {
  if (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  ) {
    return "auto";
  }
  return "smooth";
}

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
  const toast = useToast();
  const copy = FRAMING[framing];
  const [draft, setDraft] = useState("");

  // BP-15: fetch the real sandbox backend name from the server state endpoint.
  // useBuildStream doesn't expose sandbox_backend yet, so we read it once via HTTP
  // when the conversation id is known. The backend name is stable for a run's lifetime.
  const [sandboxBackend, setSandboxBackend] = useState<string | null>(null);
  // The stored (auto-titled) clean conversation title, fetched alongside the
  // sandbox backend. On RESUME the H1 prefers this over the raw first prompt
  // (which is the giant wall of text the user originally typed). null until the
  // async auto-titler lands → the H1 falls back to a TRUNCATED first task.
  const [storedTitle, setStoredTitle] = useState<string | null>(null);
  useEffect(() => {
    if (!b.cid || !agentLive()) return;
    void agentFetch(`/conversations/${b.cid}/state`)
      .then((r) => r.json())
      .then((s: { sandbox_backend?: string; title?: string | null }) => {
        setSandboxBackend(s.sandbox_backend ?? null);
        setStoredTitle(s.title ?? null);
      })
      .catch(() => {});
  }, [b.cid]);
  const download = useDownloadProject();
  const exportManifest = useExportManifest();
  // RP-06 replay: when not live (RUNNING), allow stepping through event history.
  const isReplaying = b.started && b.status !== "RUNNING";
  const replay = useReplay(b.events, !isReplaying);
  const visibleEvents = useMemo(
    () => (isReplaying ? b.events.slice(0, replay.position) : b.events),
    [b.events, isReplaying, replay.position],
  );
  const activity = useMemo(
    () => deriveActivity(visibleEvents, b.pendingActionId, b.status),
    [visibleEvents, b.pendingActionId, b.status],
  );
  const liveSignal = useMemo(
    () => deriveLiveSignal(b.events, b.status),
    [b.events, b.status],
  );
  const finalMessage = useMemo(() => latestAgentMessage(visibleEvents), [visibleEvents]);
  const deliverable = useMemo(() => deriveDeliverable(visibleEvents), [visibleEvents]);
  const [elementMention, setElementMention] = useState<ElementMentionPayload | null>(null);
  // W-01: on resume, useBuild seeds the task with the internal "(resumed)"
  // sentinel. The H1 must read the PROJECT, never the literal sentinel — and
  // never the giant raw first prompt (firstUserTask returns the whole first user
  // message, which on a resumed deck build is a wall of text). Precedence on
  // resume: stored clean auto-title → a TRUNCATED first task (so even the fallback
  // is never the wall) → "Resumed project". A fresh run keeps its real task as-is.
  const recoveredTask = useMemo(() => firstUserTask(b.events), [b.events]);
  const taskLabel = useMemo(() => {
    if (b.task !== "(resumed)") return b.task; // fresh run — unchanged
    if (storedTitle && storedTitle.trim()) return storedTitle.trim();
    if (recoveredTask) {
      const trimmed = recoveredTask.trim();
      return trimmed.length > 80 ? `${trimmed.slice(0, 80).trimEnd()}…` : trimmed;
    }
    return "Resumed project";
  }, [b.task, storedTitle, recoveredTask]);

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

  // Auto-scroll the feed to the bottom ONLY on a "you need to look now" moment —
  // never on every event (that would yank the user off whatever they're reading).
  // Two triggers: (1) the agent needs input (a gate just opened), or (2) a
  // capstone happened (the run finished/stuck/errored, or a deliverable landed).
  // We detect the TRANSITION into those, so re-renders mid-state don't re-scroll.
  const feedScrollRef = useRef<HTMLDivElement>(null);
  const prevStatusRef = useRef(b.status);
  const prevDeliverableRef = useRef<string | null>(null);
  // W-41: a docked "scroll to latest" chevron — shown only when the operator
  // has scrolled up far enough (>200px from the bottom) to have lost the live
  // tail; hidden once they're back at the bottom. The feed only auto-follows when
  // already near the bottom (below), so without this affordance a user reading
  // history has no quick way back to the live edge.
  const [showScrollDown, setShowScrollDown] = useState(false);
  const onFeedScroll = useCallback(() => {
    const el = feedScrollRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setShowScrollDown(distanceFromBottom > 200);
  }, []);
  const scrollFeedToBottom = useCallback(() => {
    const el = feedScrollRef.current;
    if (!el) return;
    if (typeof el.scrollTo === "function") {
      el.scrollTo({ top: el.scrollHeight, behavior: preferredScrollBehavior() });
    } else {
      el.scrollTop = el.scrollHeight;
    }
    setShowScrollDown(false);
  }, []);
  useEffect(() => {
    const el = feedScrollRef.current;
    const prevStatus = prevStatusRef.current;
    const prevDeliverable = prevDeliverableRef.current;
    const deliverableId = deliverable?.id ?? null;
    prevStatusRef.current = b.status;
    prevDeliverableRef.current = deliverableId;
    if (!el) return;

    const needsInput =
      b.status === "WAITING_FOR_CONFIRMATION" ||
      b.status === "AWAITING_USER_DECISION" ||
      b.status === "AWAITING_USER_QUESTION" ||
      b.status === "AWAITING_PLAN_APPROVAL";
    const capstone = b.status === "FINISHED" || b.status === "STUCK" || b.status === "ERROR";
    const statusTransitioned = b.status !== prevStatus;
    const newDeliverable = deliverableId !== null && deliverableId !== prevDeliverable;

    // W-40: the plan-approval gate renders the PlanPanel FIRST, at the TOP of the
    // feed — so scrolling to the bottom (as every other gate does) pushes the
    // approval prompt off-screen, and after the first iteration only the top-left
    // spinner hints that input is needed. Special-case it to pull the operator to
    // the TOP. Confirm/decision/question gates + capstones still scroll to bottom.
    if (statusTransitioned && b.status === "AWAITING_PLAN_APPROVAL") {
      if (typeof el.scrollTo === "function") {
        el.scrollTo({ top: 0, behavior: preferredScrollBehavior() });
      } else {
        el.scrollTop = 0;
      }
    } else if ((statusTransitioned && (needsInput || capstone)) || newDeliverable) {
      // Smooth in the browser; fall back to the scrollTop property (jsdom has no
      // scrollTo) so the behavior is testable.
      if (typeof el.scrollTo === "function") {
        el.scrollTo({ top: el.scrollHeight, behavior: preferredScrollBehavior() });
      } else {
        el.scrollTop = el.scrollHeight;
      }
    }
  }, [b.status, deliverable?.id]);

  // Continuous "follow the stream" auto-scroll: as the feed grows during a run,
  // keep the latest line in view — but ONLY when the user is already near the
  // bottom (within 120px). If they've scrolled up to read history, we don't yank
  // them back down. Keyed on the activity length so it fires per new line.
  useEffect(() => {
    const el = feedScrollRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (distanceFromBottom < 120) {
      el.scrollTop = el.scrollHeight;
      setShowScrollDown(false);
    } else {
      // W-41: content grew while the user is scrolled up — re-evaluate so the
      // chevron appears as the tail moves further out of view.
      setShowScrollDown(distanceFromBottom > 200);
    }
  }, [activity.length, b.streamingFile?.content]);
  // Steer is available whenever a conversation EXISTS to steer — including
  // STUCK/FINISHED, since the engine reopens on send_message (engine.py:671).
  // Plan-approval is the only state where steering is wrong (the plan IS the
  // input). Keeping SteerInput disabled mid-confirmation matches the prior shape.
  const steerable =
    b.status === "RUNNING" ||
    b.status === "WAITING_FOR_CONFIRMATION" ||
    b.status === "AWAITING_USER_DECISION" ||
    b.status === "STUCK" ||
    b.status === "FINISHED" ||
    b.status === "PAUSED";
  const settled = b.status === "FINISHED" || b.status === "IDLE" || b.status === "STUCK";
  // W-43 — Verbose Agent Chat. Default ON (full feed). When OFF, once the first plan
  // is approved (a plan exists and we're past the approval gate), collapse the
  // running narrative to the compact AgentStageCard. The gates (confirm/decision/
  // question) and the deliverable download/open panel render OUTSIDE this branch, so
  // they always show; the full feed stays reachable via the card's expand and the
  // inspector's "Agent History" tab (codex P1 — no affordance is hidden).
  const { verbose: verboseChat } = useVerboseAgentChat();
  const collapseFeed = !verboseChat && b.plan != null && !b.awaitingPlan;
  // Front-door "drafting…" moment: the run is live but no plan exists yet (the
  // agent is exploring + composing the first plan). Show a skeleton so the surface
  // never reads as frozen between submit and the plan-approval gate.
  const draftingPlan = b.status === "RUNNING" && !b.plan;
  const terminalIncomplete = b.status === "STUCK" || b.status === "ERROR";

  const consumeElementMention = useCallback(
    (text: string) => {
      const content = prependElementMention(text, elementMention);
      if (elementMention) setElementMention(null);
      return content;
    },
    [elementMention],
  );
  const steerWithMention = useCallback(
    (text: string) => b.steer(consumeElementMention(text)),
    [b, consumeElementMention],
  );
  const requestPlanWithMention = useCallback(
    (text: string) => b.requestPlan(consumeElementMention(text)),
    [b, consumeElementMention],
  );
  const elementMentionChip = (
    <ElementMentionChip mention={elementMention} onRemove={() => setElementMention(null)} />
  );

  if (!b.started) {
    return (
      <div className="flex min-h-full flex-col pt-section">
        <main className="flex flex-1 flex-col items-center justify-center gap-major px-body pb-[12vh]">
          <EmptyState title={copy.heroTitle} subtitle={copy.heroSubtitle} />
          <div className="w-full max-w-measure">
            <div className="mb-inline flex items-center justify-between gap-inline">
              <BuildModelPicker value={b.modelId} onChange={b.setModelId} />
              <div className="flex items-center gap-hair">
                <button
                  type="button"
                  role="switch"
                  aria-checked={b.assistChoice}
                  aria-label="Assist tier (weak-model compensations)"
                  data-disco-control="build.assist-toggle"
                  onClick={() => b.setAssistChoice(!b.assistChoice)}
                  title="Assist: weak-model compensations (file-state reinforcement, simplified tool surface + plan handling). Turn ON for a genuinely small/weak model. Leave OFF for capable models — it is NEVER auto-enabled, even for local models like Qwen 27B."
                  className={cn(
                    "flex items-center gap-hair rounded-full border px-inline py-px font-ui text-[0.72rem] transition-colors",
                    b.assistChoice
                      ? "border-accent/50 bg-accent/5 text-accent"
                      : "border-hairline text-text-faint hover:text-text-muted",
                  )}
                >
                  {b.assistChoice ? "assist: on" : "assist: off"}
                </button>
                <button
                  type="button"
                  role="switch"
                  aria-checked={b.autonomousChoice}
                  aria-label="Autonomous mode (headless run)"
                  data-disco-control="build.autonomous-toggle"
                  onClick={() => b.setAutonomousChoice(!b.autonomousChoice)}
                  title="Autonomous: the agent runs headless — it won't ask you questions, auto-approves its own plan, and stops cleanly instead of waiting for you. Best for unattended runs; for tricky tasks leave it off so the agent can ask."
                  className={cn(
                    "flex items-center gap-hair rounded-full border px-inline py-px font-ui text-[0.72rem] transition-colors",
                    b.autonomousChoice
                      ? "border-accent/50 bg-accent/5 text-accent"
                      : "border-hairline text-text-faint hover:text-text-muted",
                  )}
                >
                  {b.autonomousChoice ? "autonomous: on" : "autonomous: off"}
                </button>
              </div>
            </div>
            <QueryInput
              onSubmit={b.submit}
              busy={b.submitting}
              autoFocus
              placeholder={copy.placeholder}
              value={draft}
              onValueChange={setDraft}
              footer={
                /* G1/DR-4 + W-07: UploadComposer in the empty state. Always
                   rendered (no longer gated on preCid, which HID the attach while
                   the eager mount-create was in flight). It self-enables via
                   ensureCid — Attach is usable before a cid exists, lazily
                   creating the build conversation the first message will run. */
                <div className="flex items-center gap-inline">
                  <UploadComposer cid={b.preCid} ensureCid={b.ensurePreCid} />
                  {framing === "build" && <ImportProjectDialog />}
                </div>
              }
            />
            <p className="mt-inline text-center font-ui text-[0.78rem] text-text-faint">
              The agent works in a sandbox and shows its plan.{" "}
              {b.autonomousChoice
                ? "It runs headless — risky steps auto-approve and it won't stop to ask."
                : "Risky steps pause for your approval."}
            </p>
            {/* Agent surface: foreground the MCP tools it can reach (its reason for being). */}
            {framing === "agent" && <ConnectionsStrip />}
            {b.submitError && (
              <p role="alert" className="mt-inline text-center font-ui text-[0.8rem] text-unsupported">
                {b.submitError instanceof Error ? b.submitError.message : copy.startError}
              </p>
            )}
          </div>
          <SuggestionChips surface={framing} onPick={setDraft} />
        </main>
      </div>
    );
  }

  // ── Control pane (chat) — the Project-Manager view ─────────────────────
  const chatPane = (
    <>
        <div className="flex flex-col gap-inline px-body pt-section">
          <AgentStatusBar
            status={b.status}
            isolation={isolationForBackend(sandboxBackend)}
            sandboxState={b.sandboxState ?? undefined}
            connectionState={b.connectionState}
            autonomous={b.autonomous}
            assist={b.assist}
            onKill={b.kill}
            onStop={b.cancel}
            onResume={b.canResume ? b.resume : undefined}
            events={b.events}
            modelId={b.modelId}
            seq={b.maxSeq}
          />
          <div className="flex items-center justify-between gap-inline">
            <h1 className="font-display text-[1.3rem] font-medium leading-tight tracking-tight text-text">
              {taskLabel}
            </h1>
            {/* Export the saved project as a zip — only available for resumed
                projects and finished builds (a snapshot must exist on disk). */}
            {b.cid && (b.resumed || b.status === "FINISHED") && (
              <div className="flex shrink-0 flex-col items-end gap-hair">
                <button
                  type="button"
                  onClick={() => b.cid && download.mutate(b.cid)}
                  disabled={download.isPending}
                  aria-label="Download the project as a zip"
                  data-disco-control="build.export-zip"
                  title="Download a zip of the project files"
                  className="flex items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:text-text disabled:opacity-40"
                >
                  <Download className="size-3.5" aria-hidden />
                  {download.isPending ? "Preparing…" : "Export"}
                </button>
                {download.error && (
                  <span role="alert" className="font-ui text-[0.7rem] text-unsupported">
                    Download failed — try again
                  </span>
                )}
              </div>
            )}
          </div>
        </div>

        <div className="mt-section flex min-h-0 flex-1 flex-col px-body">
          <div className="flex items-baseline justify-between">
            <h2 className="font-ui text-[0.72rem] font-medium uppercase tracking-wide text-text-faint">
              Activity
            </h2>
            <span
              className="font-ui text-[0.68rem] text-text-faint"
              title="The agent works one step at a time; an up-front plan preview lands with the planner step."
            >
              live task list
            </span>
          </div>
          {/* RP-06: replay scrubber — step through the event log when not live. */}
          {isReplaying && b.events.length > 0 && (
            <ReplayScrubber replay={replay} />
          )}
          <div
            ref={feedScrollRef}
            onScroll={onFeedScroll}
            data-testid="build-activity-feed"
            className="mt-inline min-h-0 flex-1 overflow-y-auto pb-inline lg:pr-hair"
          >
            {b.status === "ERROR" ? (
              // Recovery: "Try again" RESUMES the errored conversation — it re-kicks
              // the loop from the full persisted history (events + workspace snapshot
              // survive), it does NOT reset/discard progress. The backend flips
              // ERROR → RUNNING and continues; the existing WS subscription streams the
              // new events in over the preserved view. The model picker lets the user
              // switch to a DIFFERENT model before retrying (the failed model may BE the
              // cause); b.resume patches the pick first so the retry runs on the new one.
              <div className="flex flex-col gap-section">
                <ErrorState message={b.error ?? "The agent run failed."} onRetry={b.resume} />
                <div className="flex flex-col items-start gap-hair rounded-control border border-hairline bg-surface-1 p-body">
                  <p className="font-ui text-[0.78rem] text-text-muted">
                    Try a different model before retrying:
                  </p>
                  <BuildModelPicker value={b.modelId} onChange={b.setModelId} />
                </div>
              </div>
            ) : b.awaitingPlan && b.plan ? (
              // Plan gate is the hero: review + Approve/Revise before any work runs.
              <div className="flex flex-col gap-section">
                <PlanPanel
                  plan={b.plan}
                  onApprove={b.approvePlan}
                  onRevise={b.requestPlan}
                />
                {activity.length > 0 && <ActivityFeed items={activity} conversationId={b.cid ?? undefined} />}
              </div>
            ) : (
              <>
                {/* front-door: drafting the first plan (live, no plan yet) */}
                {draftingPlan && (
                  <div
                    aria-label="Drafting a plan"
                    className="mb-section flex flex-col gap-hair rounded-control border border-hairline bg-surface-1 p-body"
                  >
                    <div className="flex items-center gap-hair font-ui text-[0.84rem] text-text-muted">
                      <Loader2 className="size-3.5 animate-spin" aria-hidden />
                      Drafting a plan — exploring the workspace and shaping the steps…
                    </div>
                    <div className="mt-hair flex flex-col gap-hair" aria-hidden>
                      <div className="h-2.5 w-2/3 animate-pulse rounded bg-surface-2" />
                      <div className="h-2.5 w-5/6 animate-pulse rounded bg-surface-2" />
                      <div className="h-2.5 w-1/2 animate-pulse rounded bg-surface-2" />
                    </div>
                  </div>
                )}
                {/* read-only capstone tracker: steps check off as the agent reports
                    them. Sticky to the top of the scroll area so the plan + progress
                    stay visible while the activity feed scrolls beneath it. */}
                {b.plan && (
                  <div
                    data-testid="build-sticky-plan-card"
                    className="sticky top-0 z-20 -mx-body bg-bg px-body pb-section pt-px"
                  >
                    {/* runthru-v2 (#3): capable models that emit declarative progress
                        snapshots get a live checklist; otherwise (small models, or none
                        yet) the honest status chip — never a lying empty checklist. */}
                    <PlanPanel
                      plan={b.plan}
                      progress={b.buildProgress.size ? b.buildProgress : undefined}
                      status={b.status}
                    />
                  </div>
                )}
                {collapseFeed ? (
                  // Quiet mode: one glanceable stage card (click-to-expand reveals the
                  // full feed inline, preserving every inline artifact affordance).
                  <AgentStageCard
                    events={b.events}
                    status={b.status}
                    activity={activity}
                    conversationId={b.cid ?? undefined}
                  />
                ) : (
                  <>
                    <ActivityFeed items={activity} conversationId={b.cid ?? undefined} />
                    <div className="mt-inline">
                      <LiveSignalBar signal={liveSignal} />
                    </div>
                  </>
                )}
                {finalMessage &&
                  (b.status === "FINISHED" ||
                    b.status === "STUCK" ||
                    b.status === "AWAITING_USER_DECISION") && (
                    <div
                      className={cn(
                        "mt-section rounded-card border px-body py-inline text-[0.95rem]",
                        b.status === "STUCK"
                          ? "border-warn/40 bg-warn/5"
                          : "border-hairline bg-surface-1",
                      )}
                    >
                      {b.status === "STUCK" && (
                        <p className="mb-hair font-ui text-[0.74rem] uppercase tracking-wide text-warn">
                          Agent's latest reply
                        </p>
                      )}
                      <Markdown>{finalMessage}</Markdown>
                    </div>
                  )}
              </>
            )}
          </div>
          {/* F15/W-41: the jump-to-latest control occupies its own flex row.
              Keeping it outside the scroll viewport reserves real layout space
              at every width/zoom and prevents it from covering transcript text. */}
          {showScrollDown && (
            <div
              data-testid="build-scroll-to-latest-dock"
              className="flex shrink-0 items-center justify-center py-hair"
            >
              <button
                type="button"
                onClick={scrollFeedToBottom}
                aria-label="Scroll to latest activity"
                title="Scroll to latest activity"
                data-disco-control="build.scroll-to-bottom"
                className="flex items-center justify-center rounded-full border border-hairline bg-surface-1 p-inline text-text-muted shadow-sm transition-colors hover:bg-surface-2 hover:text-text"
              >
                <ChevronDown className="size-4" aria-hidden />
              </button>
            </div>
          )}
        </div>

        {/* gate · steer · re-plan — pinned under the feed */}
        <div className="flex flex-col gap-inline border-t border-hairline px-body py-inline">
          {/* finished-artifact handoff: open the live app / download the files.
              WALK-09: gate on FINISHED — `serve` emits a DeliverableEvent mid-run
              (before the plan/build ends) so `deriveDeliverable` returns non-null
              while the manifest record doesn't exist yet → Manifest button 404s and
              Open 503s. Only show the panel once the run is truly done. */}
          <DeliverablePanel
            deliverable={b.status === "FINISHED" ? deliverable : null}
            cid={b.cid}
            onOpen={
              b.status === "FINISHED" && b.cid && deliverable?.kind === "app"
                ? () =>
                    openFreshPreview(
                      () => pathPreviewBootstrapUrl(b.cid!, "/"),
                      (reason) =>
                        toast.show({
                          title:
                            reason === "popup_blocked"
                              ? "Preview popup blocked"
                              : "Couldn’t open preview",
                          body:
                            reason === "popup_blocked"
                              ? "Allow popups for this site, then try again."
                              : "Isolated preview access could not be established. Try again.",
                        }),
                    )
                : undefined
            }
            onDownload={() => b.cid && download.mutate(b.cid)}
            onExportManifest={() => b.cid && exportManifest.mutate(b.cid)}
          />
          {b.pendingAction && (
            <ConfirmationPanel action={b.pendingAction} onApprove={b.confirm} onReject={b.reject} />
          )}
          {b.awaitingDecision && b.pendingAlternatives && (
            <AlternativesGate
              alternatives={b.pendingAlternatives}
              onPick={b.pickAlternative}
            />
          )}
          {b.awaitingQuestion && b.pendingQuestionsV2 && (
            <QuestionsV2Panel
              question={b.pendingQuestionsV2.question}
              items={b.pendingQuestionsV2.items.map((it) => ({
                id: it.id,
                question: it.question,
                options: it.options,
                allow_free_text: it.allow_free_text,
              }))}
              onAnswer={b.answer}
            />
          )}
          {b.awaitingQuestion && !b.pendingQuestionsV2 && b.pendingClarify && (
            <ClarifyPanel
              question={b.pendingClarify.question}
              items={b.pendingClarify.items.map((it) => ({
                id: it.id,
                question: it.question,
                type: it.type,
                options: it.options,
              }))}
              onAnswer={b.answer}
            />
          )}
          {b.awaitingQuestion && !b.pendingQuestionsV2 && !b.pendingClarify && (
            <AskPanel
              question={b.pendingQuestion?.message?.content ?? finalMessage ?? "The agent has a question."}
              onAnswer={b.answer}
            />
          )}
          {/* BP-11: uploads are legal whenever the conversation exists — INCLUDING
              a fresh IDLE one (upload-then-build is the order's primary flow), so
              this renders outside the steerable gate. ERROR is the one exclusion:
              the backend 409s uploads into a dead sandbox. */}
          {b.cid && b.status !== "ERROR" && <UploadComposer cid={b.cid} />}
          {steerable && (
            <div className="flex flex-col gap-hair">
              {terminalIncomplete && (
                <p className="font-ui text-[0.74rem] text-warn">
                  The run stopped before the plan was complete. Send a message to
                  steer the agent back in, or use "Plan a change…" below to
                  re-enter plan mode.
                </p>
              )}
              <SteerInput
                onSteer={steerWithMention}
                disabled={b.status === "WAITING_FOR_CONFIRMATION"}
                attachment={elementMentionChip}
              />
            </div>
          )}
          {settled && (
            <div className="flex flex-col gap-hair">
              <BuildModelPicker value={b.modelId} onChange={b.setModelId} />
              {/* re-enter plan mode: a focused, diff-style change is planned + re-approved */}
              <QueryInput
                onSubmit={requestPlanWithMention}
                placeholder={copy.replanPlaceholder}
                controlId="replan-send"
                footer={!steerable ? elementMentionChip : undefined}
              />
            </div>
          )}
          {/* RP-08: schedule this conversation to re-run on a cron cadence. Only
              meaningful once it's settled and has a persisted conversation id. */}
          {settled && b.cid && (
            <div className="border-t border-hairline pt-inline">
              <ScheduleSection conversationId={b.cid} />
            </div>
          )}
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
