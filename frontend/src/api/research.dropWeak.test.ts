import { expect, it, vi } from "vitest";
import { streamTiming, subscribeResearch } from "./research";
import type { GroundedAnswer } from "@/types/grounded";
it("Drop weak withholds statements and resets verdict counts while preserving supported citations", async () => {
  vi.useFakeTimers();
  const prior = { ...streamTiming };
  Object.assign(streamTiming, { ttft: 0, token: 0, blockGap: 0 });
  let answer: GroundedAnswer | undefined;
  const handle = subscribeResearch({ query: "RRF", drop_weak: true }, (frame) => {
    if (frame.type === "final") answer = frame.answer;
  });
  await vi.runAllTimersAsync();
  const text = answer!.blocks.filter((block) => block.kind === "prose").map((block) => block.text).join("\n");
  expect(text).not.toContain("a smaller k always improves quality");
  expect(text).not.toContain("RRF requires labelled training data");
  expect(text).toContain("[[rrf_p0]]");
  expect(text).toContain("without any score normalization");
  expect(answer!.claims.every((claim) => claim.verdict === "supported")).toBe(true);
  expect(answer!.unsupported_count).toBe(0);
  handle.cancel();
  Object.assign(streamTiming, prior);
  vi.useRealTimers();
});
