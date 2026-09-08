/**
 * The follow-up grounding ledger, driven by the REAL events that carried it.
 *
 * `__fixtures__/follow-up-grounding.json` is not hand-written: it is the
 * verbatim ActionEvent + assistant MessageEvent pair the agent-server appended
 * on 2026-09-02 for two live follow-ups on saved deep-research reports (hosted
 * `driver-muse-spark`, 88–90 saved source passages each) — one that produced an
 * answer and one that produced a wall.
 *
 * The point of the ledger is that these two cases are told apart by a COUNT the
 * server made, not by matching the wall's prose. The old spec assertion
 * (`answer.length > 80`) passed on a 128-character refusal.
 *
 * Run: npx vitest run src/lib/deepResearchFollowUpGrounding.fixture.test.ts
 */
import { describe, expect, it } from "vitest";
import capture from "./__fixtures__/follow-up-grounding.json";
import { computeFollowUpGrounding } from "@/lib/deepResearchTraceParts/activity";
import type { AgentEvent } from "@/types/agent";

const answered = capture.answered as unknown as AgentEvent[];
const refused = capture.refused as unknown as AgentEvent[];

describe("computeFollowUpGrounding, from the captured live follow-ups", () => {
  it("reads the answered run's tally off the event the server appended", () => {
    const ledger = computeFollowUpGrounding(answered);
    expect(ledger).toEqual({
      statements: 11,
      supported: 10,
      weak: 1,
      removed: 0,
      refused: false,
    });
  });

  it("keeps the weak statement the old policy would have dropped in silence", () => {
    const ledger = computeFollowUpGrounding(answered);
    // 1 weak of 11: under `supported`-only retention that sentence vanished
    // from the answer with nothing recorded anywhere that it had.
    expect(ledger?.weak).toBeGreaterThan(0);
    expect(ledger?.removed).toBe(0);
  });

  it("marks the wall refused, and the wall is a message like any other", () => {
    const ledger = computeFollowUpGrounding(refused);
    expect(ledger?.refused).toBe(true);
    expect(ledger?.statements).toBe(0);
    const message = refused.find((event) => event.kind === "message");
    expect(message).toBeDefined();
    // Length alone cannot tell this from an answer — which is the whole reason
    // the ledger exists.
    expect(
      (message as { message: { content: string } }).message.content.length,
    ).toBeGreaterThan(80);
  });

  it("returns null when no follow-up has been grounded yet", () => {
    expect(computeFollowUpGrounding([])).toBeNull();
  });

  it("drops a malformed payload rather than rendering half of it", () => {
    const broken = JSON.parse(JSON.stringify(answered)) as AgentEvent[];
    const action = broken[0] as { tool_call: { arguments: Record<string, unknown> } };
    delete action.tool_call.arguments.supported;
    expect(computeFollowUpGrounding(broken)).toBeNull();
  });
});
