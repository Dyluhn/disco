import { expect, it } from "vitest";
import { deriveActivity } from "@/lib/deepResearchTrace";
import { heartbeatReading, modelRetryRemaining, signalReading } from "@/lib/deepResearchHeartbeat";
import type { ActionEvent } from "@/types/agent";

const now = Date.parse("2026-09-22T00:00:00Z");
function reading(fields: Record<string, unknown>): ActionEvent {
  return {
    id: "reading", kind: "action", seq: 1, timestamp: new Date(now).toISOString(),
    thought: "", tool_call: { tool_name: "model_activity", arguments: {
      stage: "source_reading", seconds: 0, tokens_streamed: 0, call_ordinal: 1,
      source_id: "p1", chunk: 2, chunks: 3, ...fields,
    } },
  };
}

it("replays source reading and distinguishes first-output waiting from generation", () => {
  const activity = deriveActivity([reading({ state: "waiting" })]);
  expect(activity.modelActivity?.payload.source_id).toBe("p1");
  expect(heartbeatReading(activity, now).now).toContain("Waiting for first model output · part 2 of 3");
  const generated = deriveActivity([reading({ tokens_streamed: 12, seconds: 5 })]);
  expect(heartbeatReading(generated, now).now).toContain("receiving notes");
});

it("shows retry backoff after reconnect without reporting generation", () => {
  const event = reading({ state: "backoff", attempt: 2, retry_after_s: 30, http_status: 429 });
  const activity = deriveActivity(JSON.parse(JSON.stringify([event])));
  expect(heartbeatReading(activity, now + 5000).now).toContain("Provider retry 2 · waiting 25 s");
  expect(heartbeatReading(activity, now + 31000).now).toContain("Waiting for provider retry to start");
});


it("does not call a scheduled provider backoff a stall", () => {
  const activity = deriveActivity([reading({ state: "backoff", retry_after_s: 120 })]);
  const remaining = modelRetryRemaining(activity, now + 70000);
  expect(signalReading(now, now + 70000, now, false, remaining).level).toBe("backoff");
  expect(modelRetryRemaining(activity, now + 121000)).toBe(0);
  expect(signalReading(now, now + 121000, now, false, 0).level).toBe("silent");
});
