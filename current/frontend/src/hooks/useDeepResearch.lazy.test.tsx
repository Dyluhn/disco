/** Deep Research must not mint empty History rows on mount. An attachment may
 * lazily create one cid; the final controls are patched onto that same cid
 * before kickoff so the uploaded files are not abandoned. */

import type { ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/api/client", async (orig) => ({
  ...(await orig<typeof import("@/api/client")>()),
  agentLive: () => true,
}));

const createDeepResearchConversation = vi.fn<
  typeof import("@/api/deepResearch").createDeepResearchConversation
>(async () => "conv_deep_lazy");
vi.mock("@/api/deepResearch", () => ({
  createDeepResearchConversation: (
    ...args: Parameters<
      typeof import("@/api/deepResearch").createDeepResearchConversation
    >
  ) =>
    createDeepResearchConversation(...args),
  exportReportAsMarkdown: vi.fn(),
  exportReport: vi.fn(async () => true),
}));

const patchConversationSettings = vi.fn<
  typeof import("@/api/agent").patchConversationSettings
>(async () => undefined);
vi.mock("@/api/agent", () => ({
  killConversation: vi.fn(async () => undefined),
  patchConversationSettings: (
    ...args: Parameters<typeof import("@/api/agent").patchConversationSettings>
  ) =>
    patchConversationSettings(...args),
}));

vi.mock("./useDeepResearchStream", () => ({
  useDeepResearchStream: () => ({
    status: "IDLE",
    events: [],
    report: null,
    error: null,
    cancel: vi.fn(),
  }),
}));

import { useDeepResearch } from "./useDeepResearch";

function wrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

describe("useDeepResearch lazy conversation lifecycle", () => {
  afterEach(() => {
    createDeepResearchConversation.mockClear();
    patchConversationSettings.mockClear();
  });

  it("does not create a conversation on mount", async () => {
    const { result } = renderHook(() => useDeepResearch(), { wrapper: wrapper() });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(createDeepResearchConversation).not.toHaveBeenCalled();
    expect(result.current.selectedSources).toEqual([]);
  });

  it("keeps an upload cid and applies the final controls before kickoff", async () => {
    const { result } = renderHook(() => useDeepResearch(), { wrapper: wrapper() });

    let uploadCid: string | null = null;
    await act(async () => {
      uploadCid = (await result.current.ensurePreCid?.()) ?? null;
    });
    expect(uploadCid).toBe("conv_deep_lazy");
    await waitFor(() => expect(createDeepResearchConversation).toHaveBeenCalledTimes(1));

    act(() => {
      result.current.setLeaderId("driver-final");
      result.current.setDepthTier("exhaustive");
      result.current.setRecencyWindow("week");
      result.current.setSelectedSources(["arxiv", "ddgs"]);
    });
    act(() => result.current.submit("research this carefully"));

    await waitFor(() =>
      expect(patchConversationSettings).toHaveBeenCalledWith("conv_deep_lazy", {
        modelOverride: "driver-final",
        depthTier: "exhaustive",
        recencyWindow: "week",
        sources: ["arxiv", "ddgs"],
      }),
    );
    await waitFor(() => expect(result.current.cid).toBe("conv_deep_lazy"));
    expect(createDeepResearchConversation).toHaveBeenCalledTimes(1);
  });

  it("coalesces submit with an attachment cid creation already in flight", async () => {
    let resolveCreate!: (cid: string) => void;
    createDeepResearchConversation.mockImplementationOnce(
      () => new Promise<string>((resolve) => { resolveCreate = resolve; }),
    );
    const { result } = renderHook(() => useDeepResearch(), { wrapper: wrapper() });

    let attachment!: Promise<string | null>;
    act(() => {
      attachment = result.current.ensurePreCid!();
      result.current.submit("research the attachment");
    });
    await waitFor(() => expect(createDeepResearchConversation).toHaveBeenCalledTimes(1));

    await act(async () => {
      resolveCreate("conv_shared");
      await attachment;
    });
    await waitFor(() => expect(result.current.cid).toBe("conv_shared"));
    expect(createDeepResearchConversation).toHaveBeenCalledTimes(1);
    expect(patchConversationSettings).toHaveBeenCalledWith(
      "conv_shared",
      expect.objectContaining({ depthTier: "standard_deep" }),
    );
  });
});
