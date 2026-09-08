/**
 * A checkpoint is resumable state, not history.
 *
 * `deriveResearchCheckpoint` returned the newest `research_checkpoint` in the
 * log no matter what came after it, so after a resume the surface kept showing
 * "Research paused before a report was written · N sources retained. Resume to
 * continue…" over a run that was visibly working — `live2/stop-captures.json`,
 * capture `93-resumed`: phase `running`, signal `live`, `dr.stop` back on the
 * control row, and that sentence still on the page.
 *
 * The events here are verbatim (`__fixtures__/l29-resume-frames.json`): the
 * checkpoint, the PAUSED it lands with, and the RUNNING the resumed run opens
 * with, captured from the live Stop-then-Resume proof.
 *
 * Run: npx vitest run src/lib/deepResearchCheckpointLifetime.test.ts
 */
import { describe, expect, it } from "vitest";
import capture from "./__fixtures__/l29-resume-frames.json";
import { deriveResearchCheckpoint } from "@/lib/deepResearchTrace";
import type { AgentEvent } from "@/types/agent";

const events = capture.events as unknown as AgentEvent[];
const checkpoint = events.find((event) => event.kind === "research_checkpoint")!;
const paused = events.find(
  (event) => event.kind === "status" && event.status === "PAUSED",
)!;
const running = events.find(
  (event) => event.kind === "status" && event.status === "RUNNING",
)!;

describe("deriveResearchCheckpoint — resumable state, not history", () => {
  it("stands while the run is PAUSED at it", () => {
    expect(deriveResearchCheckpoint([checkpoint, paused])?.id).toBe(checkpoint.id);
  });

  it("is gone once the run resumes", () => {
    // The exact sequence the live run wrote: checkpoint → PAUSED → RUNNING.
    expect(deriveResearchCheckpoint(events)).toBeNull();
    expect(deriveResearchCheckpoint([checkpoint, paused, running])).toBeNull();
  });

  it("is gone once a report lands", () => {
    const report = { kind: "report", id: "rep-1", seq: 999 } as unknown as AgentEvent;
    expect(deriveResearchCheckpoint([checkpoint, paused, report])).toBeNull();
  });

  it("is gone once a kill ends the run", () => {
    // A killed run has nothing to resume from, and its own terminal says so —
    // a checkpoint panel beside that notice would contradict it.
    const killed = {
      kind: "status",
      id: "st-killed",
      seq: 999,
      status: "IDLE",
      detail: "killed",
    } as unknown as AgentEvent;
    expect(deriveResearchCheckpoint([checkpoint, paused, killed])).toBeNull();
  });

  it("a PAUSED that came BEFORE the checkpoint does not hide it", () => {
    // The scan walks back to the newest status; an older one is not it.
    expect(deriveResearchCheckpoint([paused, checkpoint, paused])?.id).toBe(checkpoint.id);
  });

  it("the live resume wrote no observation between PAUSED and RUNNING", () => {
    // The other half of the same capture: rule 6 in the durable log. Resume
    // used to pair every deep-research progress marker with "This action was
    // interrupted by a server restart — its outcome is UNKNOWN."
    expect(capture.observations_between_paused_and_running).toBe(0);
    expect(events.filter((event) => event.kind === "observation")).toEqual([]);
  });
});
