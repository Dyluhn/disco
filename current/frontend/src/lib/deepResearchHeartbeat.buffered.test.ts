/**
 * A buffered driver is not a stalled one.
 *
 * `model_activity` comes from the SSE path, so a driver that makes one buffered
 * POST (the Responses API, and the one-shot fallback for a server that refuses
 * to stream) reported nothing for the whole call and the heartbeat read
 * "Nothing reported for N" for minutes at a time. True of the transport, and
 * read by everyone as a hung model.
 *
 * The producer now reports once at call start with nothing delivered and
 * `streams: false`. `__fixtures__/buffered-model-activity.json` is that exact
 * payload, dumped from a real run of the producer against a `/responses`
 * endpoint — the provider's own stream-vs-buffered decision, not a guess from a
 * model name. Regenerate it with
 * `AI-Work/.../F2-evidence/item6-freeze-buffered-frame.py`; the Python side is
 * `test_research_model_activity.py::test_a_buffered_driver_reports_once_at_call_start…`.
 */
import { describe, expect, it } from "vitest";
import buffered from "./__fixtures__/buffered-model-activity.json";
import {
  heartbeatReading,
  modelIsBuffered,
  signalReading,
} from "@/lib/deepResearchHeartbeat";
import { deriveActivity } from "@/lib/deepResearchTrace";
import type { ActionEvent, AgentEvent } from "@/types/agent";

const T0 = Date.parse("2026-09-02T10:00:00.000Z");

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

/** The producer's frame, at the instant the call opened. */
function bufferedCall(offsetSeconds: number): ActionEvent {
  return action(
    "m1",
    buffered.tool_name,
    buffered.arguments as unknown as Record<string, unknown>,
    offsetSeconds,
  );
}

function reading(events: AgentEvent[], atSeconds: number) {
  return heartbeatReading(deriveActivity(events), T0 + atSeconds * 1000);
}

describe("a buffered driver's single heartbeat", () => {
  it("survives the parser as `streams: false`", () => {
    const activity = deriveActivity([bufferedCall(0)]);
    expect(activity.modelActivity?.payload.streams).toBe(false);
    expect(activity.modelActivity?.payload.tokens_streamed).toBe(0);
    expect(modelIsBuffered(activity)).toBe(true);
  });

  it("says the driver does not report progress, with the age of the call", () => {
    // Two minutes into the review call, which on a streamed driver would be
    // eight heartbeats and here is one report and then nothing.
    const r = reading([action("p1", "phase", { phase: "writing" }, 0), bufferedCall(1)], 121);
    expect(r.now).toBe(
      "Waiting for model reply (this driver does not report progress while it works) · 2:00",
    );
  });

  it("never claims that about a streamed call", () => {
    // The same review stage, streamed: the phase line owns the now-line and the
    // heartbeat is only its duration source, exactly as before.
    const streamed = action(
      "m2",
      "model_activity",
      { stage: "review", tokens_streamed: 640, seconds: 42.5, call_ordinal: 1 },
      1,
    );
    const activity = deriveActivity([action("p1", "phase", { phase: "writing" }, 0), streamed]);
    expect(modelIsBuffered(activity)).toBe(false);
    expect(heartbeatReading(activity, T0 + 121_000).now).toBe("Writing the report · 2:01");
  });
});

// ---- F5 item 2: the chip beside that line must not contradict it -------------
//
// The strip said "Waiting for model reply (this driver does not report progress while it
// works) · 2:00" and the activity chip next to it said "no signal for 2:00".
// Both were computed from the same event; only the chip did not know the
// transport, so it read the expected quiet as a stall.

describe("the activity chip on a buffered driver", () => {
  const events = [action("p1", "phase", { phase: "writing" }, 0), bufferedCall(1)];
  const activity = deriveActivity(events);

  it("reports the call, not a stall, once past the silent threshold", () => {
    const signal = signalReading(
      activity.lastEventAt,
      T0 + 121_000,
      activity.lastEventAt!,
      modelIsBuffered(activity),
    );
    expect(signal.level).toBe("buffered");
    expect(signal.label).toBe("Waiting for model reply · 2:00");
  });

  it("agrees with the strip line beside it — same fact, same clock", () => {
    const now = T0 + 121_000;
    const strip = heartbeatReading(activity, now).now;
    const chip = signalReading(
      activity.lastEventAt,
      now,
      activity.lastEventAt!,
      modelIsBuffered(activity),
    );
    expect(strip).toContain("2:00");
    expect(chip.label).toContain("2:00");
    expect(chip.label).not.toContain("no signal");
  });

  it("still calls a STREAMED driver's silence a stall", () => {
    const streamed = deriveActivity([
      action("p1", "phase", { phase: "writing" }, 0),
      action("m2", "model_activity", { stage: "review", tokens_streamed: 640, seconds: 42.5, call_ordinal: 1 }, 1),
    ]);
    const signal = signalReading(
      streamed.lastEventAt,
      T0 + 121_000,
      streamed.lastEventAt!,
      modelIsBuffered(streamed),
    );
    expect(signal.level).toBe("silent");
    expect(signal.label).toBe("no signal for 2:00");
  });
});


it.each([
  ["search", { query: "primary documentation" }],
  ["turn", { n: 1, of: 8, phase: "planning" }],
  ["phase", { phase: "writing" }],
])("does not label a completed model call as active after %s", (kind, payload) => {
  const activity = deriveActivity([bufferedCall(0), action("next", kind, payload, 10)]);
  expect(modelIsBuffered(activity)).toBe(false);
  const signal = signalReading(activity.lastEventAt, T0 + 40_000, T0, modelIsBuffered(activity));
  expect(signal.level).toBe("quiet");
});
