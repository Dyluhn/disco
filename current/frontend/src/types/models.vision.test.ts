import { describe, expect, it } from "vitest";
import { staticVisionStatus } from "./models";

describe("static vision capability hint", () => {
  it("distinguishes conclusive text-only from unknown", () => {
    expect(staticVisionStatus("text-embedding-3-large")).toBe("text-only");
    expect(staticVisionStatus("opaque-provider-model-7b")).toBe("unknown");
  });

  it("recognizes only conclusive visual families", () => {
    expect(staticVisionStatus("openai/gpt-4o-mini")).toBe("vision");
    expect(staticVisionStatus("qwen3-vl-8b")).toBe("vision");
  });
});
