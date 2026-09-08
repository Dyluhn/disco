import { beforeEach, expect, it, vi } from "vitest";
const backend = vi.hoisted(() => ({
  agentLive: vi.fn(), isLive: vi.fn(), agentSend: vi.fn(), apiSend: vi.fn(),
}));
vi.mock("./client", () => ({ ...backend, apiGet: vi.fn(), fixtureDelay: vi.fn() }));
import { deleteConversation } from "./conversations";
beforeEach(() => {
  vi.clearAllMocks();
  backend.isLive.mockReturnValue(true);
});
it("deletes through the runtime owner so active research drains before file cleanup", async () => {
  backend.agentLive.mockReturnValue(true);
  await deleteConversation("conv_owned");
  expect(backend.agentSend).toHaveBeenCalledWith("DELETE", "/conversations/conv_owned");
  expect(backend.apiSend).not.toHaveBeenCalled();
});
it("keeps the library-only fallback when no Agent server is connected", async () => {
  backend.agentLive.mockReturnValue(false);
  await deleteConversation("conv_owned");
  expect(backend.apiSend).toHaveBeenCalledWith("DELETE", "/api/conversations/conv_owned");
});
