/**
 * G1/DR-4 attach tests — DeepResearchSurface empty state.
 *
 * When agentLive() is true and a pre-create returns a cid, the UploadComposer
 * should appear in the empty state of the Deep Research surface BEFORE the user
 * submits a query. Verifies F1 (pre-create-on-mount) + the footer slot wiring.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { ModeProvider } from "@/shell/ModeProvider";
import { DeepResearchSurface } from "./DeepResearchSurface";
import * as clientModule from "@/api/client";

// ---- module-level mocks -------------------------------------------------------

vi.mock("@/api/agent", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/api/agent")>();
  return {
    ...mod,
    // Used by killConversation in useDeepResearch — no-op here.
    killConversation: vi.fn().mockResolvedValue(undefined),
    // subscribeConversation never fires in the empty state (no session).
    subscribeConversation: vi.fn().mockReturnValue({ close: () => {} }),
  };
});

vi.mock("@/api/deepResearch", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/api/deepResearch")>();
  return {
    ...mod,
    // Pre-create fires in useDeepResearch with { query: "", depthTier, recencyWindow }.
    createDeepResearchConversation: vi.fn().mockResolvedValue("cid_test_deep"),
  };
});

// ---- helpers -----------------------------------------------------------------

function renderSurface() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/"]}>
        <ModeProvider>
          <Routes>
            <Route path="/" element={<DeepResearchSurface />} />
          </Routes>
        </ModeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// ---- tests -------------------------------------------------------------------

describe("DeepResearchSurface — G1/DR-4 attach (empty state)", () => {
  beforeEach(() => {
    window.localStorage.clear();
    // Make agentLive() return true so the pre-create effect fires.
    vi.spyOn(clientModule, "agentLive").mockReturnValue(true);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("UploadComposer appears in the empty state once the pre-create resolves", async () => {
    renderSurface();

    // The composer appears once createDeepResearchConversation resolves the preCid.
    await waitFor(
      () => expect(screen.getByTestId("upload-composer")).toBeInTheDocument(),
      { timeout: 3000 },
    );
    // The attach-files button is enabled — cid is wired.
    expect(screen.getByRole("button", { name: /attach files/i })).toBeEnabled();
  });

  it("UploadComposer is absent when agentLive is false (offline/fixture mode)", async () => {
    vi.spyOn(clientModule, "agentLive").mockReturnValue(false);
    renderSurface();

    // Allow async effects to settle; the composer must never appear.
    await new Promise((r) => setTimeout(r, 100));
    expect(screen.queryByTestId("upload-composer")).not.toBeInTheDocument();
  });

  it("createDeepResearchConversation is called on mount with empty query", async () => {
    const { createDeepResearchConversation } = await import("@/api/deepResearch");
    renderSurface();

    await waitFor(
      () => expect(createDeepResearchConversation).toHaveBeenCalled(),
      { timeout: 3000 },
    );
    // The pre-create fires with an EMPTY query (just a placeholder conversation record).
    const [firstCallArg] = (createDeepResearchConversation as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(firstCallArg).toMatchObject({ query: "" });
  });
});
