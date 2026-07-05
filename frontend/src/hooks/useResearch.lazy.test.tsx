/**
 * BW-08 twin: the Research surface (default landing surface, conversations tagged
 * surface="agent") must NOT POST /conversations on mount. The old eager
 * mount-create minted a fresh server row on every page load — the 0-event
 * "(untitled)" ghosts that flooded History/Projects. The conversation is created
 * only on a real signal: submit (or an actual upload via ensurePreCid).
 */

import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/api/client", async (orig) => ({
  ...(await orig<typeof import("@/api/client")>()),
  agentLive: () => true,
}));

const createBuildConversation = vi.fn(async () => "conv_created");
vi.mock("@/api/agent", () => ({
  createBuildConversation: (...a: unknown[]) => createBuildConversation(...a),
}));

const requestResearch = vi.fn(async () => ({ status: "ok" }));
vi.mock("@/api/research", () => ({
  requestResearch: (...a: unknown[]) => requestResearch(...a),
}));

// useResearchStream subscribes off the scope; with scope=null it's inert, but
// stub it so the test never reaches for a live WebSocket.
vi.mock("./useResearchStream", () => ({
  useResearchStream: () => ({ events: [], blocks: [] }),
}));

import { useResearch } from "./useResearch";

function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactElement }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

describe("useResearch — BW-08 twin lazy pre-create", () => {
  afterEach(() => {
    createBuildConversation.mockClear();
    requestResearch.mockClear();
  });

  it("does NOT create a conversation on mount", async () => {
    renderHook(() => useResearch(), { wrapper: wrapper() });
    // Give any (errant) mount effect a chance to fire.
    await new Promise((r) => setTimeout(r, 0));
    expect(createBuildConversation).not.toHaveBeenCalled();
  });

  it("creates the conversation only on submit, tagged surface=agent", async () => {
    const { result } = renderHook(() => useResearch(), { wrapper: wrapper() });
    expect(createBuildConversation).not.toHaveBeenCalled();

    await act(async () => {
      await result.current.submit("what is the speed of light", {
        sources: ["arxiv", "ddgs"],
      });
    });

    await waitFor(() => expect(createBuildConversation).toHaveBeenCalledTimes(1));
    // surface="agent" (the row tag) is the 2nd positional arg.
    expect(createBuildConversation).toHaveBeenCalledWith(null, "agent");
    // The minted cid is threaded onto the research request (and retained as runCid).
    await waitFor(() => expect(result.current.runCid).toBe("conv_created"));
    expect(requestResearch).toHaveBeenCalledWith(
      expect.objectContaining({
        conversation_id: "conv_created",
        sources: ["arxiv", "ddgs"],
      }),
    );
  });

  it("reuses a single cid across submit + reScope (no duplicate create)", async () => {
    const { result } = renderHook(() => useResearch(), { wrapper: wrapper() });

    await act(async () => {
      await result.current.submit("first query");
    });
    await waitFor(() => expect(createBuildConversation).toHaveBeenCalledTimes(1));

    act(() => {
      result.current.reScope({ query: "narrower" });
    });
    // reScope reuses the already-minted preCid — no second POST /conversations.
    expect(createBuildConversation).toHaveBeenCalledTimes(1);
  });
});
