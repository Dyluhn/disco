/**
 * useBuild — terminal-state model swap. When a conversation is ERRORED / FINISHED /
 * STUCK / PAUSED, the model picker is live and the chosen model must actually drive the
 * next turn. The hook PATCHes the (newly-picked) model BEFORE the resume / replan / steer
 * re-kick — so the new model lands before the loop composes (the #24 silently-ignored fix)
 * — and seeds the picker from the conversation's current pinned model on resume.
 */

import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/api/client", async (orig) => ({
  ...(await orig<typeof import("@/api/client")>()),
  agentLive: () => true,
  agentHttpBase: () => "",
}));

const patchConversationSettings = vi.fn(async () => undefined);
vi.mock("@/api/agent", () => ({
  createBuildConversation: vi.fn(async () => "conv_created"),
  killConversation: vi.fn(async () => undefined),
  patchConversationSettings: (...a: unknown[]) => patchConversationSettings(...a),
}));

// A controllable stream stub: the parent surface drives status; resume/requestPlan/steer
// are spies so we can assert the hook calls them AFTER the model patch.
const streamResume = vi.fn();
const streamRequestPlan = vi.fn();
const streamSteer = vi.fn();
let streamStatus = "ERROR";
vi.mock("./useBuildStream", () => ({
  useBuildStream: () => ({
    status: streamStatus,
    cancel: vi.fn(),
    resume: streamResume,
    requestPlan: streamRequestPlan,
    steer: streamSteer,
  }),
}));

import { useBuild } from "./useBuild";

function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactElement }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

beforeEach(() => {
  streamStatus = "ERROR";
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ json: async () => ({ model_override: "model-from-server" }) })) as unknown as typeof fetch,
  );
});

afterEach(() => {
  patchConversationSettings.mockClear();
  streamResume.mockClear();
  streamRequestPlan.mockClear();
  streamSteer.mockClear();
  vi.unstubAllGlobals();
});

describe("useBuild — terminal-state model swap", () => {
  it("PATCHes the picked model BEFORE resume on an errored conversation", async () => {
    const { result } = renderHook(() => useBuild("conv_err"), { wrapper: wrapper() });

    await act(async () => {
      result.current.setModelId("model-b");
    });
    await act(async () => {
      await result.current.resume();
    });

    expect(patchConversationSettings).toHaveBeenCalledWith("conv_err", {
      modelOverride: "model-b",
    });
    expect(streamResume).toHaveBeenCalledTimes(1);
    // Order: the patch must land before the re-kick.
    expect(patchConversationSettings.mock.invocationCallOrder[0]).toBeLessThan(
      streamResume.mock.invocationCallOrder[0],
    );
  });

  it("PATCHes the model before replanning (requestPlan) on a FINISHED conversation", async () => {
    streamStatus = "FINISHED";
    const { result } = renderHook(() => useBuild("conv_done"), { wrapper: wrapper() });

    await act(async () => {
      result.current.setModelId("model-c");
    });
    await act(async () => {
      await result.current.requestPlan("add a dark theme");
    });

    expect(patchConversationSettings).toHaveBeenCalledWith("conv_done", {
      modelOverride: "model-c",
    });
    expect(streamRequestPlan).toHaveBeenCalledWith("add a dark theme");
  });

  it("does NOT patch the model while RUNNING — even when the picker was touched", async () => {
    streamStatus = "RUNNING";
    const { result } = renderHook(() => useBuild("conv_run"), { wrapper: wrapper() });

    await act(async () => {
      result.current.setModelId("model-x"); // touched, but mid-run
    });
    await act(async () => {
      await result.current.steer("keep going but smaller");
    });

    expect(patchConversationSettings).not.toHaveBeenCalled();
    expect(streamSteer).toHaveBeenCalledWith("keep going but smaller");
  });

  it("PATCHes null (RESET to default) when the user explicitly picks DEFAULT on a terminal conv", async () => {
    streamStatus = "FINISHED";
    const { result } = renderHook(() => useBuild("conv_reset"), { wrapper: wrapper() });

    // The conversation was seeded to its current model; the user then explicitly chooses
    // DEFAULT (the picker emits null). That is a deliberate reset, NOT a no-op.
    await act(async () => {
      result.current.setModelId(null);
    });
    await act(async () => {
      await result.current.resume();
    });

    expect(patchConversationSettings).toHaveBeenCalledWith("conv_reset", {
      modelOverride: null,
    });
    expect(streamResume).toHaveBeenCalledTimes(1);
  });

  it("does NOT patch when the picker was UNTOUCHED on a terminal conv (leave model as-is)", async () => {
    streamStatus = "FINISHED";
    const { result } = renderHook(() => useBuild("conv_untouched"), { wrapper: wrapper() });

    // No setModelId — the user didn't change the picker (even though it was seeded).
    await act(async () => {
      await result.current.resume();
    });

    expect(patchConversationSettings).not.toHaveBeenCalled();
    expect(streamResume).toHaveBeenCalledTimes(1);
  });

  it("seeds the picker from the conversation's current model on resume", async () => {
    const { result } = renderHook(() => useBuild("conv_resume"), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.modelId).toBe("model-from-server"));
  });

  it("a user pick is NOT clobbered by the resume seed", async () => {
    const { result } = renderHook(() => useBuild("conv_resume2"), { wrapper: wrapper() });
    await act(async () => {
      result.current.setModelId("user-pick");
    });
    // The seed fetch resolves after the pick; the guard keeps the user's choice.
    await new Promise((r) => setTimeout(r, 0));
    expect(result.current.modelId).toBe("user-pick");
  });
});
