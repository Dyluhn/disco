import { describe, expect, it } from "vitest";

import type { StreamFrame } from "@/types/grounded";
import {
  initialResearchStreamState,
  researchStreamReducer,
} from "./useResearchStream";

describe("research stream wire compatibility", () => {
  it("keeps Search mounted while a no-answer round reformulates", () => {
    const running = researchStreamReducer(initialResearchStreamState, {
      type: "reset",
    });
    const reformulating = researchStreamReducer(running, {
      type: "frame",
      frame: { type: "phase", phase: "reformulating" },
    });

    expect(reformulating.phase).toBe("running");
    expect(reformulating.stage).toBe("reformulating");
    expect(reformulating.blocks).toEqual([]);
  });

  it("ignores a future advisory frame instead of returning undefined", () => {
    const state = researchStreamReducer(initialResearchStreamState, {
      type: "frame",
      frame: { type: "future-progress", detail: "still working" } as unknown as StreamFrame,
    });

    expect(state).toBe(initialResearchStreamState);
  });

  it("turns a malformed final frame into a visible error state", () => {
    const state = researchStreamReducer(initialResearchStreamState, {
      type: "frame",
      frame: { type: "final", answer: { query: "q" } } as unknown as StreamFrame,
    });

    expect(state.phase).toBe("error");
    expect(state.error).toContain("missing its structured blocks");
    expect(state.blocks).toEqual([]);
  });
});
