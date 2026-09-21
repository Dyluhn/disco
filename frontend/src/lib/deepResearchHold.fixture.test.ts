/**
 * The `hold` payload, end to end, from a VERBATIM producer payload.
 *
 * `__fixtures__/hold-action.json` was not hand-written: it is the exact dict
 * `retrieval.deep_research._progress_events.emit_hold` passed to `emit`, dumped
 * from a run of the real function. That matters for the bug this file pins —
 * `queued_queries` is a LIST OF QUERY STRINGS and the TS mirror declared
 * `number`, so `parseHold` coerced it to 0 and the hold panel's "N queries
 * waiting to run" clause could never render. A hand-written object would have
 * been written from the same wrong belief and agreed with itself.
 *
 * The Python side of the same fixture is
 * `development/harness/tests/test_contract.py::test_hold_payload_shape_matches_ts_fixture`,
 * which re-runs the producer and fails if the field types move.
 */
import { describe, expect, it } from "vitest";
import { holdCopy } from "@/lib/deepResearchHold";
import { deriveActivity, deriveLiveTrace } from "@/lib/deepResearchTrace";
import type { ActionEvent, AgentEvent } from "@/types/agent";
import holdAction from "./__fixtures__/hold-action.json";

const AT = "2026-09-02T08:31:52.000Z";

function holdEvents(): AgentEvent[] {
  const event: ActionEvent = {
    id: "evt_hold_1",
    kind: "action",
    timestamp: AT,
    thought: "",
    tool_call: {
      tool_name: holdAction.tool_name,
      arguments: holdAction.arguments as unknown as Record<string, unknown>,
    },
  };
  return [event];
}

describe("hold payload from the real producer", () => {
  it("carries the queued queries as strings, not a count", () => {
    expect(Array.isArray(holdAction.arguments.queued_queries)).toBe(true);
    const hold = deriveActivity(holdEvents()).hold;
    expect(hold).not.toBeNull();
    expect(hold?.payload.queued_queries).toEqual([
      "SK On pilot line capacity Georgia 2026",
      "LFP cell cycle life field data",
      "solid-state separator supplier qualification",
    ]);
  });

  it("renders the count in the hold panel's state line", () => {
    const hold = deriveActivity(holdEvents()).hold;
    expect(hold).not.toBeNull();
    const copy = holdCopy(hold!, Date.parse(AT));
    expect(copy.stateNow).toBe("Turn 7 of 16 · 42 sources kept · 3 queries waiting to run");
  });

  it("hands the panel the queued queries whole, so nothing has to truncate them", () => {
    const hold = deriveActivity(holdEvents()).hold;
    expect(holdCopy(hold!, Date.parse(AT)).queuedWork).toEqual(
      holdAction.arguments.queued_queries,
    );
  });

  it("renders the queries themselves in the hold's trace row", () => {
    const [row] = deriveLiveTrace(holdEvents(), "RUNNING");
    expect(row.label).toBe("Waiting for search engines to cool down");
    expect(row.detail).toBe(
      'brave, mojeek · 3 queries queued: “SK On pilot line capacity Georgia 2026”, ' +
        '“LFP cell cycle life field data”, “solid-state separator supplier qualification”',
    );
  });

  it("says nothing about queued work when the producer queued none", () => {
    const events = holdEvents();
    const action = events[0] as ActionEvent;
    action.tool_call!.arguments = { ...action.tool_call!.arguments, queued_queries: [] };
    const hold = deriveActivity(events).hold;
    expect(hold?.payload.queued_queries).toEqual([]);
    const copy = holdCopy(hold!, Date.parse(AT));
    expect(copy.stateNow).toBe("Turn 7 of 16 · 42 sources kept");
    expect(copy.queuedWork).toEqual([]);
  });
});
