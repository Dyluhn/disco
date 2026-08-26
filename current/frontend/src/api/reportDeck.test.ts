import { describe, expect, it } from "vitest";
import { startReportDeck } from "./reportDeck";
import { FIXTURE_CID } from "@/fixtures/agentTrace";

describe("report deck handoff", () => {
  it("returns a typed fixture job without serializing report content client-side", async () => {
    const result = await startReportDeck("conv_source");

    expect(result).toMatchObject({
      ok: true,
      contract: "deck",
      format: "pptx",
    });
    expect(result.conversation_id).toBe(FIXTURE_CID);
    expect(result).not.toHaveProperty("report");
  });
});
