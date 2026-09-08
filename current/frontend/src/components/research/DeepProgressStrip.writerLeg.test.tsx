/**
 * The strip describes ONE leg, everywhere — not just in the heartbeat reading.
 *
 * F1 fixed the reading: once a writer-leg fact is the newest thing reported,
 * the turn line becomes "Research done · N turns" and the subquestion is
 * dropped. The strip then rendered one more research-leg line under it that the
 * reading does not own — `stats.lastThought`, which is a gap rationale when it
 * came from the research leg — so "Writing the report" still sat above
 * "found X, still missing Y". A writer's own thought (a finished section) is a
 * writer-leg fact and stays.
 *
 * Frames are F1's writer-leg vocabulary (`deepResearchHeartbeat.writerLeg.test.ts`):
 * a finished research leg followed by a writer phase / review round /
 * draft-stage `model_activity`.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { deriveActivity, type DeepStats } from "@/lib/deepResearchTrace";
import type { ActionEvent, AgentEvent } from "@/types/agent";
import { DeepProgressStrip } from "./DeepProgressStrip";

const T0 = Date.parse("2026-09-02T10:00:00.000Z");
const RATIONALE = "Found pilot-line capacity, still missing cell cycle life";

function iso(offsetSeconds: number): string {
  return new Date(T0 + offsetSeconds * 1000).toISOString();
}

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
    timestamp: iso(offsetSeconds),
    thought: "",
    tool_call: { tool_name: name, arguments: args },
  };
}

/** The state 72-writing.png captured: a finished research leg, then the writer. */
function researchThenWriter(...tail: AgentEvent[]): AgentEvent[] {
  return [
    action(
      "t1",
      "turn",
      { n: 16, of: 16, phase: "searching", subquestion: "pilot line capacity in Georgia" },
      0,
    ),
    action("s1", "search", { query: "SK On pilot production timeline" }, 1),
    ...tail,
  ];
}

const BASE_STATS: DeepStats = {
  searches: 22,
  refusals: 0,
  sectionsDone: 0,
  sourcesDiscovered: 42,
  elapsedSeconds: 600,
  phase: "writing",
  activeSection: null,
  lastThought: null,
  lastThoughtLeg: null,
  carriedSources: null,
};

function renderStrip(events: AgentEvent[], stats: Partial<DeepStats>) {
  return render(
    <DeepProgressStrip
      brief={null}
      trace={[]}
      stats={{ ...BASE_STATS, ...stats }}
      status="RUNNING"
      activity={deriveActivity(events)}
      followUpStatus={null}
    />,
  );
}

describe("DeepProgressStrip during the writer leg", () => {
  it("drops the research-leg rationale once the writer phase leads", () => {
    renderStrip(researchThenWriter(action("p1", "phase", { phase: "writing" }, 30)), {
      lastThought: RATIONALE,
      lastThoughtLeg: "research",
    });
    expect(screen.getByText(/^Writing the report/)).toBeInTheDocument();
    expect(screen.getByText("Research done · 16 turns")).toBeInTheDocument();
    expect(screen.queryByText(RATIONALE)).not.toBeInTheDocument();
  });

  it("does the same for a review round", () => {
    renderStrip(researchThenWriter(action("rv1", "review", { k: 1, of: 3 }, 60)), {
      lastThought: RATIONALE,
      lastThoughtLeg: "research",
    });
    expect(screen.getByText(/^Review 1 of 3/)).toBeInTheDocument();
    expect(screen.queryByText(RATIONALE)).not.toBeInTheDocument();
  });

  it("does the same when only a writer-stage model_activity is newer", () => {
    renderStrip(
      researchThenWriter(
        action(
          "m1",
          "model_activity",
          { stage: "draft", tokens_streamed: 640, seconds: 42.5, call_ordinal: 3 },
          40,
        ),
      ),
      { lastThought: RATIONALE, lastThoughtLeg: "research" },
    );
    expect(screen.queryByText(RATIONALE)).not.toBeInTheDocument();
  });

  it("keeps the writer's own thought — a finished section is not searching", () => {
    renderStrip(researchThenWriter(action("p1", "phase", { phase: "writing" }, 30)), {
      lastThought: "Finished “Cost trajectory”",
      lastThoughtLeg: "writer",
    });
    expect(screen.getByText("Finished “Cost trajectory”")).toBeInTheDocument();
  });

  it("leaves the rationale alone while the research leg is still the newest thing", () => {
    renderStrip(researchThenWriter(), {
      phase: "researching",
      lastThought: RATIONALE,
      lastThoughtLeg: "research",
    });
    expect(screen.getByText("Turn 16 of 16 · searching")).toBeInTheDocument();
    expect(screen.getByText(RATIONALE)).toBeInTheDocument();
  });
});
