/**
 * The stopping wall, driven by the REAL frames that produced it.
 *
 * `__fixtures__/l29-stop-frames.json` was not hand-written: it is the verbatim
 * WebSocket frame sequence a Firefox/Playwright run captured on 2026-09-02 when
 * Stop was pressed during the writer phase of a live `quick` deep-research run
 * — the writer phase being exactly where Stop used to do nothing at all. The
 * events go through the production derivers (`deriveActivity`, `deriveStats`)
 * and then through the production copy, so a change to either shows up here as
 * changed WORDS rather than as a passing shape assertion.
 *
 * Run: npx vitest run src/lib/deepResearchStopping.fixture.test.ts
 */
import { describe, expect, it } from "vitest";
import capture from "./__fixtures__/l29-stop-frames.json";
import { deriveActivity, deriveStats } from "@/lib/deepResearchTrace";
import { stoppingCopy, stoppingForLabel } from "@/lib/deepResearchStopping";
import type { AgentEvent } from "@/types/agent";

const events = capture.events as unknown as AgentEvent[];
/** Everything up to (not including) the run's own terminal status. */
const beforeTerminal = events.filter((event) => event.kind !== "status");
/** The instant of the newest frame in the capture, so ages are real spans. */
const CAPTURED_AT = Date.parse("2026-09-02T08:54:54.000Z");

describe("the stopping wall, from the captured live run", () => {
  it("derives the Stop marker the server appended, and nothing terminal", () => {
    const activity = deriveActivity(beforeTerminal);
    expect(activity.stopRequested).not.toBeNull();
    expect(activity.stopRequested?.payload.requested_at).toBe(
      "2026-09-02T08:53:53.542998+00:00",
    );
  });

  it("clears the moment the run writes its own ending", () => {
    // The last frame in the capture is the run's terminal PAUSED/"stopped".
    const activity = deriveActivity(events);
    expect(activity.stopRequested).toBeNull();
    expect(events.at(-1)).toMatchObject({ kind: "status", status: "PAUSED", detail: "stopped" });
  });

  it("radiates all four parts, each traceable to a captured event", () => {
    const activity = deriveActivity(beforeTerminal);
    const sources = capture.measured_over_the_whole_run.sources_discovered;
    const copy = stoppingCopy(activity, CAPTURED_AT, sources);
    expect(copy).not.toBeNull();

    // WHY — the marker's own timestamp, not the browser's clock.
    expect(copy!.why).toMatch(/^You pressed Stop at /);

    // STATE — the step in flight, in the heartbeat's own vocabulary. The
    // capture's newest reported fact is the draft stream's `model_activity`
    // under `phase{writing}`, so the wall says the writer is producing.
    expect(copy!.stateNow).toContain("Writing the report");

    // …and how alive the stream is, by the same rule the strip uses. The last
    // heartbeat in the capture is ~1 s before it ends, so this reads live.
    expect(copy!.signal).toBe("Still reporting");

    // NEXT — the checkpoint, and the source count the run actually observed.
    expect(copy!.next).toContain("writes a checkpoint");
    expect(copy!.next).toContain(`the ${sources} sources it has found are kept`);
    expect(copy!.next).toContain("Resume continues from them");

    // ALLOWED — wait, or Kill, and what Kill costs in one clause.
    expect(copy!.allowed).toContain("Wait for the step to finish");
    expect(copy!.allowed).toContain("no checkpoint");
  });

  it("promises only the sources the run has actually reported", () => {
    // The capture is the run's TAIL, so the slice's own observation total is 0
    // — and the wall then says what it can honestly say instead of "0 sources".
    const activity = deriveActivity(beforeTerminal);
    expect(deriveStats(events).sourcesDiscovered).toBe(0);
    expect(stoppingCopy(activity, CAPTURED_AT, 0)!.next).toContain(
      "whatever it has found is kept",
    );
    // One source is singular, not "1 sources".
    expect(stoppingCopy(activity, CAPTURED_AT, 1)!.next).toContain(
      "the 1 source it has found",
    );
  });

  it("ages the request off the marker's own timestamp", () => {
    const activity = deriveActivity(beforeTerminal);
    // 08:53:53.5 → 08:54:54.0 is the measured minute the draft in flight took.
    expect(stoppingForLabel(activity, CAPTURED_AT)).toBe("asked 1:00 ago");
    // Nothing to age against before the marker exists.
    expect(stoppingForLabel(deriveActivity([]), CAPTURED_AT)).toBeNull();
  });

  it("says nothing at all when Stop has not been pressed", () => {
    const beforeStop = beforeTerminal.filter(
      (event) => event.kind !== "action" || event.tool_call?.tool_name !== "stop_requested",
    );
    expect(stoppingCopy(deriveActivity(beforeStop), CAPTURED_AT, 26)).toBeNull();
  });
});
