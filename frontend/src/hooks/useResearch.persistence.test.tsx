import type { ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { rrfAnswer } from "@/fixtures/answers";
import type { ReScope, StreamFrame } from "@/types/grounded";

const calls = vi.hoisted(() => ({ read: vi.fn(), create: vi.fn(), subscribe: vi.fn(), cancel: vi.fn() }));
vi.mock("@/api/client", async (orig) => ({
  ...(await orig<typeof import("@/api/client")>()), agentLive: () => true,
}));
vi.mock("@/api/agent", () => ({ createBuildConversation: calls.create }));
vi.mock("@/api/research", async (orig) => ({
  ...(await orig<typeof import("@/api/research")>()),
  getResearchHistory: calls.read,
  subscribeResearch: calls.subscribe,
}));
import { useResearch } from "./useResearch";

const scope: ReScope = {
  query: "Saved question", conversation_id: "conv_saved", request_id: "original",
  sources: ["arxiv"], domains_deny: ["blocked.test"], think: true,
};
const answer = { ...rrfAnswer, query: scope.query, blocks: [...rrfAnswer.blocks, {
  kind: "sheet" as const, id: "sheet-stable", title: "Table", filename: "artifacts/original.xlsx",
  sheet_names: ["Sheet1"], formulas_evaluated: false as const,
}] };
function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}
beforeEach(() => {
  vi.clearAllMocks();
  calls.read.mockResolvedValue({ conversation_id: "conv_saved", turns: [{ scope, answer }], latest_request: { scope } });
  calls.subscribe.mockReturnValue({ cancel: calls.cancel });
});
describe("durable Search restore", () => {
  it("reopens the exact answer, citations, sources, artifacts and follow-ups without creating or running", async () => {
    const first = renderHook(() => useResearch("conv_saved"), { wrapper: wrapper() });
    await waitFor(() => expect(first.result.current.answer).toEqual(answer));
    expect(first.result.current.scope).toEqual(scope);
    expect(first.result.current.runCid).toBe("conv_saved");
    expect(first.result.current.blocks.at(-1)).toEqual(answer.blocks.at(-1));
    first.rerender();
    first.unmount();
    const reopened = renderHook(() => useResearch("conv_saved"), { wrapper: wrapper() });
    await waitFor(() => expect(reopened.result.current.answer).toEqual(answer));
    expect(calls.create).not.toHaveBeenCalled();
    expect(calls.subscribe).not.toHaveBeenCalled();
  });
  it("starts an explicit follow-up in the same conversation and lets older answers reopen without a run", async () => {
    const { result } = renderHook(() => useResearch("conv_saved"), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.answer).toEqual(answer));
    act(() => result.current.submit("Follow up", { sources: ["arxiv"] }));
    await waitFor(() => expect(calls.subscribe).toHaveBeenCalledTimes(1));
    const [next, onFrame] = calls.subscribe.mock.calls[0] as [ReScope, (f: StreamFrame) => void];
    expect(next.conversation_id).toBe("conv_saved");
    expect(next.parent_request_id).toBe("original");
    expect(next.request_id).toBeTruthy();
    expect(next.request_id).not.toBe("original");
    const followup = { ...answer, query: "Follow up" };
    calls.read.mockResolvedValue({ conversation_id: "conv_saved", turns: [{ scope, answer }, { scope: next, answer: followup }], latest_request: { scope: next } });
    act(() => onFrame({ type: "final", answer: followup }));
    await waitFor(() => expect(result.current.turns).toHaveLength(2));
    act(() => result.current.selectTurn("original"));
    expect(result.current.answer).toEqual(answer);
    expect(result.current.scope).toEqual(scope);
    expect(calls.subscribe).toHaveBeenCalledTimes(1);
    expect(calls.create).not.toHaveBeenCalled();
  });
  it("a denied or failed history read cannot silently start a replacement search", async () => {
    calls.read.mockRejectedValue(new Error("Forbidden"));
    const { result } = renderHook(() => useResearch("conv_private"), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.restoreError).toBeTruthy());
    expect(calls.create).not.toHaveBeenCalled();
    expect(calls.subscribe).not.toHaveBeenCalled();
  });
  it("re-scope keeps saved source constraints and uses a fresh request identity", async () => {
    const { result } = renderHook(() => useResearch("conv_saved"), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.answer).toEqual(answer));
    act(() => result.current.reScope({ drop_weak: true }));
    await waitFor(() => expect(calls.subscribe).toHaveBeenCalledTimes(1));
    expect(calls.subscribe.mock.calls[0][0]).toMatchObject({
      conversation_id: "conv_saved", sources: ["arxiv"], domains_deny: ["blocked.test"],
      parent_request_id: "original", drop_weak: true,
    });
    expect(calls.subscribe.mock.calls[0][0].request_id).not.toBe("original");
  });
});

it("retrying a failed follow-up keeps its completed parent instead of referencing the failed request", async () => {
  const { result } = renderHook(() => useResearch("conv_saved"), { wrapper: wrapper() });
  await waitFor(() => expect(result.current.answer).toEqual(answer));
  act(() => result.current.submit("Follow up"));
  await waitFor(() => expect(calls.subscribe).toHaveBeenCalledTimes(1));
  const [failed, onFrame] = calls.subscribe.mock.calls[0] as [ReScope, (f: StreamFrame) => void];
  act(() => onFrame({ type: "error", message: "Unavailable" }));
  act(() => result.current.reScope({}));
  await waitFor(() => expect(calls.subscribe).toHaveBeenCalledTimes(2));
  const retried = calls.subscribe.mock.calls[1][0];
  expect(retried.parent_request_id).toBe("original");
  expect(retried.request_id).not.toBe(failed.request_id);
});
