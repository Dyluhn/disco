import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { rrfAnswer } from "@/fixtures/answers";
const calls = vi.hoisted(() => ({ read: vi.fn(), create: vi.fn(), subscribe: vi.fn() }));
vi.mock("@/api/client", async (orig) => ({
  ...(await orig<typeof import("@/api/client")>()), agentLive: () => true,
}));
vi.mock("@/api/agent", async (orig) => ({
  ...(await orig<typeof import("@/api/agent")>()), createBuildConversation: calls.create,
}));
vi.mock("@/api/conversations", async (orig) => ({
  ...(await orig<typeof import("@/api/conversations")>()),
  listConversations: async () => [{ id: "conv_saved", title: "Saved search", surface: "research", status: "FINISHED", created_at: "2026-09-24", owner_id: "local" }],
}));
vi.mock("@/api/research", async (orig) => ({
  ...(await orig<typeof import("@/api/research")>()), getResearchHistory: calls.read, subscribeResearch: calls.subscribe,
}));
import App from "@/App";
beforeEach(() => {
  vi.clearAllMocks();
  calls.read.mockResolvedValue({ conversation_id: "conv_saved", turns: [{ scope: { query: "Saved search", conversation_id: "conv_saved", request_id: "first" }, answer: rrfAnswer }], latest_request: null });
});
it("History opens the canonical Search route and reload keeps the saved answer without a query", async () => {
  window.history.replaceState({}, "", "/history");
  const first = render(<App />);
  await userEvent.click(await screen.findByRole("button", { name: /^Saved search Finished/ }));
  await waitFor(() => expect(window.location.pathname).toBe("/research/conv_saved"));
  expect(await screen.findByRole("heading", { name: "Saved search" })).toBeInTheDocument();
  expect(await screen.findByText(/without any score normalization/i)).toBeInTheDocument();
  first.unmount();
  render(<App />);
  expect(await screen.findByRole("heading", { name: "Saved search" })).toBeInTheDocument();
  expect(calls.create).not.toHaveBeenCalled();
  expect(calls.subscribe).not.toHaveBeenCalled();
});

it("a completed live Search gets its canonical URL without creating a replacement conversation", async () => {
  window.history.replaceState({}, "", "/");
  calls.create.mockResolvedValue("conv_saved");
  calls.subscribe.mockReturnValue({ cancel: vi.fn() });
  render(<App />);
  await userEvent.type(await screen.findByPlaceholderText(/ask anything/i), "Saved search{Enter}");
  await waitFor(() => expect(calls.subscribe).toHaveBeenCalledTimes(1));
  act(() => calls.subscribe.mock.calls[0][1]({ type: "final", answer: rrfAnswer }));
  await waitFor(() => expect(window.location.pathname).toBe("/research/conv_saved"));
  expect(await screen.findByRole("heading", { name: "Saved search" })).toBeInTheDocument();
  expect(calls.create).toHaveBeenCalledTimes(1);
  expect(calls.create).toHaveBeenCalledWith(null, "research");
  expect(calls.subscribe).toHaveBeenCalledTimes(1);
});
