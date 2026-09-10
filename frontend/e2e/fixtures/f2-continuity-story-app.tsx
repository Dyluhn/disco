/**
 * F2 story — run continuity, rendered from CAPTURED FRAMES so a screenshot
 * proves each change reached the screen.
 *
 * Nothing here is hand-authored data:
 *   `hold-action.json`              the dict `emit_hold` handed to `emit`.
 *   `l29-stop-frames.json`          the WebSocket frames of L29's live Stop.
 *   `l29-resume-frames.json`        the checkpoint / PAUSED / RUNNING of the
 *                                   live Stop-then-Resume proof.
 *   `buffered-model-activity.json`  the one `model_activity` a Responses-API
 *                                   driver emits, dumped from the producer.
 * The writer-leg panel is F1's own writer vocabulary (turn → search → phase →
 * draft-stage heartbeat), which is the state S1's `72-writing.png` captured.
 *
 * Not part of the shipped app — same pattern as `f1-truth-story-app.tsx`,
 * mounted only by a Playwright spec against the fixture-mode dev server.
 */
import { DeepProgressStrip } from "@/components/research/DeepProgressStrip";
import { DeepResearchHoldPanel } from "@/components/research/DeepResearchHoldPanel";
import { DeepResearchRunView } from "@/components/research/deepResearchSurfaceParts/DeepResearchRunView";
import type { useDeepResearch } from "@/hooks/useDeepResearch";
import {
  deriveActivity,
  deriveLiveTrace,
  deriveResearchCheckpoint,
  deriveStats,
} from "@/lib/deepResearchTrace";
import type { ActionEvent, AgentEvent, ConversationStatus } from "@/types/agent";
import bufferedActivity from "@/lib/__fixtures__/buffered-model-activity.json";
import holdAction from "@/lib/__fixtures__/hold-action.json";
import stopCapture from "@/lib/__fixtures__/l29-stop-frames.json";
import resumeCapture from "@/lib/__fixtures__/l29-resume-frames.json";

/**
 * The strip's ages are live by design, so the synthetic frames below are
 * stamped RELATIVE TO NOW — otherwise every panel renders "no signal for 3h"
 * and the screenshot shows a clock artefact instead of the change. The frames
 * themselves are unchanged; only the instant they are anchored to moves.
 */
const T0 = Date.now();

function action(
  id: string,
  name: string,
  args: Record<string, unknown>,
  secondsAgo: number,
): ActionEvent {
  return {
    id,
    kind: "action",
    seq: 1,
    timestamp: new Date(T0 - secondsAgo * 1000).toISOString(),
    thought: "",
    tool_call: { tool_name: name, arguments: args },
  };
}

const HOLD_EVENTS: AgentEvent[] = [
  action("evt_hold", holdAction.tool_name, holdAction.arguments as Record<string, unknown>, 0),
];

/** A finished research leg, then the writer — with the research leg's own gap
 *  rationale still the newest `lastThought` the stats carry. */
const WRITER_EVENTS: AgentEvent[] = [
  action(
    "evt_turn",
    "turn",
    { n: 16, of: 16, phase: "searching", subquestion: "pilot line capacity in Georgia" },
    75,
  ),
  action("evt_search", "search", { query: "SK On pilot production timeline" }, 74),
  action("evt_phase", "phase", { phase: "writing" }, 45),
  action(
    "evt_ma",
    "model_activity",
    { stage: "draft", tokens_streamed: 512, reasoning_tokens: 512, seconds: 40.2, call_ordinal: 3 },
    35,
  ),
];

const STOP_EVENTS = stopCapture.events as unknown as AgentEvent[];
const RESUME_EVENTS = resumeCapture.events as unknown as AgentEvent[];
const CHECKPOINT = RESUME_EVENTS.find((event) => event.kind === "research_checkpoint")!;

/** The stopped run as the run view sees it: L29's own frames, plus the real
 *  checkpoint frame (this capture omits its own — 401 KB). */
const STOPPED_EVENTS: AgentEvent[] = [
  ...STOP_EVENTS.filter((event) => event.kind !== "status"),
  CHECKPOINT,
  ...STOP_EVENTS.filter((event) => event.kind === "status"),
];

/** A buffered driver two minutes into the writer's call. */
const BUFFERED_EVENTS: AgentEvent[] = [
  action("evt_phase_b", "phase", { phase: "writing" }, 121),
  action(
    "evt_ma_b",
    bufferedActivity.tool_name,
    bufferedActivity.arguments as Record<string, unknown>,
    120,
  ),
];

/** The run view reads one object; every field on it comes from the captured
 *  frames through the real derivers, so nothing on screen is invented. */
function runFrom(events: AgentEvent[], status: ConversationStatus, statusDetail: string | null) {
  return {
    status,
    statusDetail,
    error: null,
    failure: null,
    activity: deriveActivity(events),
    stats: deriveStats(events),
    trace: deriveLiveTrace(events, status),
    brief: null,
    report: null,
    checkpoint: deriveResearchCheckpoint(events),
    followUpStatus: null,
    query: "What is the measured state of sodium-ion battery cycle life in 2026?",
    retry: () => {},
    runExhaustive: () => {},
    steer: () => {},
    stop: () => {},
    kill: () => {},
  } as unknown as ReturnType<typeof useDeepResearch>;
}

function Panel({ id, title, children }: { id: string; title: string; children: React.ReactNode }) {
  return (
    <section data-f2={id} className="flex flex-col gap-inline">
      <h2 className="font-mono text-[0.7rem] uppercase tracking-wide text-text-faint">{title}</h2>
      {children}
    </section>
  );
}

export function F2ContinuityStory() {
  const holdActivity = deriveActivity(HOLD_EVENTS);
  const writerStats = {
    ...deriveStats(WRITER_EVENTS),
    searches: 22,
    sourcesDiscovered: 42,
    lastThought: "Found pilot-line capacity, still missing cell cycle life",
    lastThoughtLeg: "research" as const,
  };
  return (
    <div className="flex flex-col gap-body bg-bg p-body text-text">
      <Panel id="hold" title="2 · hold panel — the queued queries, whole, under the state line">
        <DeepResearchHoldPanel
          hold={holdActivity.hold!}
          nowMs={T0}
          onContinue={() => {}}
          onStop={() => {}}
        />
      </Panel>

      <Panel id="writer" title="3 · writer leg — nothing on the strip still describes searching">
        <DeepProgressStrip
          brief={null}
          trace={[]}
          stats={writerStats}
          status="RUNNING"
          activity={deriveActivity(WRITER_EVENTS)}
          followUpStatus={null}
        />
      </Panel>

      <Panel id="checkpoint" title="4 · stopped run — the checkpoint stands where the wall was">
        <DeepResearchRunView r={runFrom(STOPPED_EVENTS, "PAUSED", "stopped")} />
      </Panel>

      <Panel id="resumed" title="5 · resumed run — its own turn budget, the pool it carried">
        <DeepProgressStrip
          brief={null}
          trace={[]}
          stats={{ ...deriveStats(RESUME_EVENTS), searches: 0, sourcesDiscovered: 9 }}
          status="RUNNING"
          activity={deriveActivity([action("evt_turn_r", "turn", { n: 1, of: 8, phase: "thinking" }, 12)])}
          followUpStatus={null}
        />
      </Panel>

      <Panel id="buffered" title="6 · buffered driver — silent by transport, and it says so">
        <DeepProgressStrip
          brief={null}
          trace={[]}
          stats={deriveStats(BUFFERED_EVENTS)}
          status="RUNNING"
          activity={deriveActivity(BUFFERED_EVENTS)}
          followUpStatus={null}
        />
      </Panel>
    </div>
  );
}
