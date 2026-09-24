import { expect, it, vi } from "vitest";
import { referencePackFilePath } from "./referencePacks";
vi.mock("./client", () => ({ agentHttpBase: () => "/agent", agentFetch: vi.fn(), agentGet: vi.fn(), agentLive: vi.fn(), agentSend: vi.fn() }));
it("routes file links through the agent service with encoded path segments", () => {
  expect(referencePackFilePath("rp example", "style guide#.md")).toBe("/agent/api/reference-packs/rp%20example/files/style%20guide%23.md");
});
