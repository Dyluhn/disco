/**
 * Regression (NON-FROZEN, R4 reachability) — OFFLINE resume of a snapshotted demo
 * project rehydrates to a FINISHED view, so the finished-handoff panels (Self-host /
 * Deliverable) mount without a live agent. This is the offline-fidelity behaviour the
 * frozen candidate / needs-review / download-binding Firefox specs depend on: they
 * `goto` a resume route (a BARE subscribe, no kick) and require the SelfHostPanel
 * visible.
 *
 * It is driven by the fixture project REGISTRY (a snapshotted demo project resumes as
 * finished), never a spec-detecting cid list — so the fresh-run fixture cid, which has
 * no persisted snapshot, must NOT auto-finish (its live plan→build→finish flow is
 * unchanged).
 */

import { describe, expect, it } from "vitest";
import { subscribeConversation } from "./agent";
import type { ConversationState, WSServerFrame } from "@/types/agent";

type StateFrame = { type: "state"; state: ConversationState };
const isStateFrame = (f: WSServerFrame): f is StateFrame => f.type === "state";

/** Subscribe offline, collect the frames emitted over `ms`, then tear down. */
function collectFrames(cid: string, ms: number): Promise<WSServerFrame[]> {
  return new Promise((resolve) => {
    const frames: WSServerFrame[] = [];
    const handle = subscribeConversation(cid, (f) => frames.push(f));
    setTimeout(() => {
      handle.cancel();
      resolve(frames);
    }, ms);
  });
}

describe("offline resume rehydration", () => {
  it("a snapshotted demo project (conv_demo_snake) resumes to FINISHED on a bare subscribe", async () => {
    const frames = await collectFrames("conv_demo_snake", 300);
    const state = frames.find(isStateFrame);
    expect(state).toBeDefined();
    expect(state?.state.execution_status).toBe("FINISHED");
    // The rehydrated state names the RESUMED cid (not the fresh-run FIXTURE_CID).
    expect(state?.state.conversation_id).toBe("conv_demo_snake");
  });

  it("the fresh-run fixture cid (conv_fixture) emits NO finished rehydration on a bare subscribe", async () => {
    const frames = await collectFrames("conv_fixture", 300);
    expect(frames.filter(isStateFrame)).toHaveLength(0);
  });
});
