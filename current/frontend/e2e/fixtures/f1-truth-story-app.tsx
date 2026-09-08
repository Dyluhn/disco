/**
 * F1 story — the frontend-truth surfaces, rendered from FROZEN PRODUCER
 * PAYLOADS so a screenshot proves the change reached the screen.
 *
 * Nothing here is hand-authored data. `hold-action.json` is the exact dict
 * `retrieval.deep_research._progress_events.emit_hold` handed to `emit`;
 * `deep-research-error-event.json` is an `ErrorEvent.model_dump(mode="json")`
 * from the real `exhaustion_failure → ResearchAgentError → run_failure_for`
 * chain. The report ledger is S1's captured `turn_accounting`
 * ({model_turns: 7, degraded_turns: 0, of: 8}), which is what caught the
 * bounded notice claiming the budget instead of the turns spent.
 *
 * Not part of the shipped app — same pattern as `deck-editor-a11y-app.tsx`,
 * mounted only by a Playwright spec against the fixture-mode dev server.
 */
import { DeepBoundedNotice } from "@/components/research/DeepBoundedNotice";
import { DeepResearchHoldPanel } from "@/components/research/DeepResearchHoldPanel";
import { ActivityFeed } from "@/components/build/ActivityFeed";
import { ErrorState } from "@/components/states";
import { heartbeatReading } from "@/lib/deepResearchHeartbeat";
import { deriveActivity, deriveLiveTrace } from "@/lib/deepResearchTrace";
import type { ActionEvent, AgentEvent, ErrorEvent, ReportEvent } from "@/types/agent";
import errorEvent from "@/lib/__fixtures__/deep-research-error-event.json";
import holdAction from "@/lib/__fixtures__/hold-action.json";

const T0 = Date.parse("2026-09-02T08:31:52.000Z");

function action(
  id: string,
  name: string,
  args: Record<string, unknown>,
  offsetSeconds: number,
): ActionEvent {
  return {
    id,
    kind: "action",
    seq: 1,
    timestamp: new Date(T0 + offsetSeconds * 1000).toISOString(),
    thought: "",
    tool_call: { tool_name: name, arguments: args },
  };
}

/** The hold, exactly as `emit_hold` produced it. */
const HOLD_EVENTS: AgentEvent[] = [
  action("evt_hold", holdAction.tool_name, holdAction.arguments as Record<string, unknown>, 0),
];

/** S1's `72-writing.png` state: a finished research leg, then the writer. */
const WRITER_EVENTS: AgentEvent[] = [
  action(
    "evt_turn",
    "turn",
    { n: 16, of: 16, phase: "searching", subquestion: "pilot line capacity in Georgia" },
    0,
  ),
  action("evt_search", "search", { query: "SK On pilot production timeline" }, 1),
  action("evt_phase", "phase", { phase: "writing" }, 30),
  action(
    "evt_ma",
    "model_activity",
    { stage: "draft", tokens_streamed: 512, reasoning_tokens: 512, seconds: 40.2, call_ordinal: 3 },
    40,
  ),
];

/** One admitted source — the row that used to read "Added 1 sources". */
const OBSERVATION_EVENTS: AgentEvent[] = [
  action("evt_s1", "search", { query: "SK On pilot production timeline" }, 0),
  {
    id: "evt_o1",
    kind: "observation",
    seq: 2,
    timestamp: new Date(T0 + 4000).toISOString(),
    action_id: "evt_s1",
    tool_result: {
      tool_name: "observation",
      success: true,
      content: "",
      structured: { added: 1, total_for_subq: 12 },
    },
  } as AgentEvent,
];

const REPORT: ReportEvent = {
  id: "evt_report",
  kind: "report",
  query: "Solid-state battery commercialization",
  summary: "s",
  sections: [],
  passages: [],
  all_hits: [],
  unsupported_count: 0,
  bounded_by: "sources",
  depth_tier: "quick",
  meta: { turn_accounting: { model_turns: 7, degraded_turns: 0, of: 8 } },
} as unknown as ReportEvent;

const FAILURE = (errorEvent as unknown as ErrorEvent).failure ?? null;

function Panel({ id, title, children }: { id: string; title: string; children: React.ReactNode }) {
  return (
    <section data-f1={id} className="flex flex-col gap-inline">
      <h2 className="font-mono text-[0.7rem] uppercase tracking-wide text-text-faint">{title}</h2>
      {children}
    </section>
  );
}

export function F1TruthStory() {
  const holdActivity = deriveActivity(HOLD_EVENTS);
  const writer = heartbeatReading(deriveActivity(WRITER_EVENTS), T0 + 45_000);
  return (
    <div className="flex flex-col gap-body bg-bg p-body text-text">
      <Panel id="hold" title="1 · hold panel — queued queries (count) + trace row (the queries)">
        <DeepResearchHoldPanel
          hold={holdActivity.hold!}
          nowMs={T0}
          onContinue={() => {}}
          onStop={() => {}}
        />
        <div className="rounded-card border border-hairline bg-surface-1 px-body py-body">
          <ActivityFeed items={deriveLiveTrace(HOLD_EVENTS, "RUNNING")} />
        </div>
      </Panel>

      <Panel id="bounded" title="2 · bounded notice — turns SPENT, not the budget">
        <DeepBoundedNotice report={REPORT} />
      </Panel>

      <Panel id="heartbeat" title="3 + 7 · writer leg — no stale searching line, no stale subquestion">
        <div
          data-f1-heartbeat=""
          className="rounded-card border border-hairline bg-surface-1 px-body py-inline"
        >
          <div className="truncate font-ui text-[0.8rem] text-text">{writer.now}</div>
          {writer.turn && (
            <div className="mt-hair truncate font-mono text-[0.75rem] text-text-faint">
              {writer.turn}
            </div>
          )}
          {writer.subquestion && (
            <div className="mt-hair truncate font-reading text-[0.78rem] italic text-text-muted">
              on “{writer.subquestion}”
            </div>
          )}
        </div>
      </Panel>

      <Panel id="plural" title="4 · pluralisation — one source is one source">
        <div className="rounded-card border border-hairline bg-surface-1 px-body py-body">
          <ActivityFeed items={deriveLiveTrace(OBSERVATION_EVENTS, "RUNNING")} />
        </div>
      </Panel>

      <Panel id="failure" title="6 · error wall — why / state / next / allowed as four parts">
        <ErrorState
          message={(errorEvent as unknown as ErrorEvent).detail ?? ""}
          onRetry={() => {}}
          failure={FAILURE}
        />
      </Panel>
    </div>
  );
}
