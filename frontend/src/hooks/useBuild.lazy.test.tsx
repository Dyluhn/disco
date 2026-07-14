/**
 * BW-08: pre-create is LAZY. Mounting useBuild on the empty Build state must NOT
 * POST /conversations — the old eager mount-create minted a fresh server row on
 * every visit (the 0-event "(untitled)" ghosts that flooded History/Projects).
 * The conversation is created only on a real signal: submit (or an actual upload
 * via ensurePreCid).
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
const patchConversationSettings = vi.fn(async () => undefined);
vi.mock("@/api/agent", () => ({
  createBuildConversation: (...a: unknown[]) => createBuildConversation(...a),
  killConversation: vi.fn(async () => undefined),
  patchConversationSettings: (...a: unknown[]) => patchConversationSettings(...a),
}));

// useBuildStream subscribes off the session; with session=null it's inert, but
// stub it so the test never reaches for a live WebSocket.
vi.mock("./useBuildStream", () => ({
  useBuildStream: () => ({ cancel: vi.fn() }),
}));

import { useBuild } from "./useBuild";

function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactElement }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

describe("useBuild — BW-08 lazy pre-create", () => {
  afterEach(() => {
    createBuildConversation.mockClear();
    patchConversationSettings.mockClear();
  });

  it("does NOT create a conversation on mount", async () => {
    renderHook(() => useBuild(), { wrapper: wrapper() });
    // Give any (errant) mount effect a chance to fire.
    await new Promise((r) => setTimeout(r, 0));
    expect(createBuildConversation).not.toHaveBeenCalled();
  });

  it("creates the conversation only on submit", async () => {
    const { result } = renderHook(() => useBuild(), { wrapper: wrapper() });
    expect(createBuildConversation).not.toHaveBeenCalled();

    await act(async () => {
      await result.current.submit("build me a fizzbuzz");
    });

    await waitFor(() => expect(createBuildConversation).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(result.current.cid).toBe("conv_created"));
  });

  it("coalesces submit with an attachment cid creation already in flight", async () => {
    let resolveCreate!: (cid: string) => void;
    createBuildConversation.mockImplementationOnce(
      () => new Promise<string>((resolve) => { resolveCreate = resolve; }),
    );
    const { result } = renderHook(() => useBuild(), { wrapper: wrapper() });

    let attachment!: Promise<string | null>;
    act(() => {
      attachment = result.current.ensurePreCid!();
      void result.current.submit("build from the attachment");
    });
    await waitFor(() => expect(createBuildConversation).toHaveBeenCalledTimes(1));

    await act(async () => {
      resolveCreate("conv_shared");
      await attachment;
    });
    await waitFor(() => expect(result.current.cid).toBe("conv_shared"));
    expect(createBuildConversation).toHaveBeenCalledTimes(1);
    expect(patchConversationSettings).toHaveBeenCalledWith(
      "conv_shared",
      expect.objectContaining({ modelOverride: null }),
    );
  });
});
