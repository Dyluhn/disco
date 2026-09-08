/**
 * The heartbeat must describe ONE state.
 *
 * S1's `72-writing.png` caught it describing two at once: the now-line said
 * "Writing the report" with "Turn 16 of 16 · searching" and a research
 * subquestion directly beneath it. Each line was the newest event of its own
 * kind, so each was true in isolation — and together they said the run was
 * searching and writing at the same time. Once a writer-leg fact is the newest
 * thing reported, the turn line reports what the research leg PRODUCED and the
 * subquestion (an angle the run has finished with) is dropped.
 *
 * The event vocabulary here is the engine's own: `turn` / `search` / `phase` /
 * `review` / `rework` / `continuation` / `model_activity`.
 */
import { describe, expect, it } from "vitest";
import { heartbeatReading } from "@/lib/deepResearchHeartbeat";
import { deriveActivity } from "@/lib/deepResearchTrace";
import type { ActionEvent, AgentEvent } from "@/types/agent";

const T0 = Date.parse("2026-09-02T10:00:00.000Z");

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

/** The state 72-writing.png captured: a finished research leg followed by the
 *  writer, with the last `turn` event still sitting in the activity. */
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

function reading(events: AgentEvent[], atSeconds: number) {
  return heartbeatReading(deriveActivity(events), T0 + atSeconds * 1000);
}

describe("heartbeat during the writer leg", () => {
  it("stops saying the run is searching once the writer phase leads", () => {
    const r = reading(researchThenWriter(action("p1", "phase", { phase: "writing" }, 30)), 31);
    expect(r.now).toBe("Writing the report · 1 s");
    expect(r.turn).toBe("Research done · 16 turns");
    expect(r.subquestion).toBeNull();
  });

  it("does the same for a review round", () => {
    const r = reading(researchThenWriter(action("rv1", "review", { k: 1, of: 3 }, 60)), 61);
    expect(r.now).toBe("Review 1 of 3 · 1 s");
    expect(r.turn).toBe("Research done · 16 turns");
    expect(r.subquestion).toBeNull();
  });

  it("does the same when only a writer-stage model_activity is newer", () => {
    // `model_activity` is never the now-line itself — it is the duration source
    // — but a draft-stage heartbeat still means the writer is the live leg.
    const r = reading(
      researchThenWriter(
        action(
          "m1",
          "model_activity",
          { stage: "draft", tokens_streamed: 640, seconds: 42.5, call_ordinal: 3 },
          40,
        ),
      ),
      45,
    );
    expect(r.now).not.toMatch(/searching/);
    expect(r.turn).toBe("Research done · 16 turns");
    expect(r.subquestion).toBeNull();
  });

  it("leaves the research leg alone while it is still the newest thing", () => {
    const r = reading(researchThenWriter(), 2);
    expect(r.now).toBe("Searching: “SK On pilot production timeline” · 1 s");
    expect(r.turn).toBe("Turn 16 of 16 · searching");
    expect(r.subquestion).toBe("pilot line capacity in Georgia");
  });

  it("goes back to the research leg when a new turn opens after a writer round", () => {
    // A continuation round can be followed by more research; the writer no
    // longer leads and the turn line is a turn line again.
    const r = reading(
      [
        ...researchThenWriter(action("rv1", "review", { k: 1, of: 3 }, 60)),
        action("t2", "turn", { n: 17, of: 20, phase: "planning", subquestion: "supply" }, 90),
      ],
      91,
    );
    expect(r.turn).toBeNull(); // the turn IS the now-line
    expect(r.now).toMatch(/^Turn 17 of 20 · planning the next searches/);
    expect(r.subquestion).toBe("supply");
  });

  it("drops the line rather than inventing a count when no turn was reported", () => {
    const r = reading([action("p1", "phase", { phase: "writing" }, 30)], 31);
    expect(r.now).toBe("Writing the report · 1 s");
    expect(r.turn).toBeNull();
  });
});

describe("reasoning-only streams", () => {
  it("says the writer is thinking when every delta was reasoning", () => {
    const r = reading(
      [
        action("p1", "phase", { phase: "writing" }, 30),
        action(
          "m1",
          "model_activity",
          {
            stage: "draft",
            tokens_streamed: 512,
            reasoning_tokens: 512,
            seconds: 40.2,
            call_ordinal: 3,
          },
          40,
        ),
      ],
      45,
    );
    expect(r.now).toBe("Writing the report: thinking, no words yet · 15 s");
  });

  it("says nothing extra once visible words are streaming", () => {
    const r = reading(
      [
        action("p1", "phase", { phase: "writing", words_streamed: 240 }, 30),
        action(
          "m1",
          "model_activity",
          {
            stage: "draft",
            tokens_streamed: 900,
            reasoning_tokens: 512,
            seconds: 60.1,
            call_ordinal: 3,
          },
          40,
        ),
      ],
      45,
    );
    expect(r.now).toBe("Writing the report: 240 words · 15 s");
  });

  it("never claims thinking on a provider with no reasoning channel", () => {
    const r = reading(
      [
        action("p1", "phase", { phase: "writing" }, 30),
        action(
          "m1",
          "model_activity",
          { stage: "draft", tokens_streamed: 512, seconds: 40.2, call_ordinal: 3 },
          40,
        ),
      ],
      45,
    );
    expect(r.now).toBe("Writing the report · 15 s");
  });
});
