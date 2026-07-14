/**
 * G1/DR-4 attach tests — DeepResearchSurface empty state.
 *
 * The UploadComposer appears before a cid exists and lazily creates one only
 * after the user actually selects a file. Merely visiting the surface must not
 * leave an empty conversation in History.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
    uploadFiles: vi.fn().mockResolvedValue({
      saved: [{ name: "notes.txt", bytes: 5 }],
      rejected: [],
    }),
  };
});

vi.mock("@/api/deepResearch", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/api/deepResearch")>();
  return {
    ...mod,
    // Lazy creation fires only after a real attachment signal.
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
  beforeEach(async () => {
    vi.clearAllMocks();
    // restoreAllMocks() also clears implementations on module-level vi.fn
    // mocks, so make each test independent by restoring their behavior here.
    const deepApi = await import("@/api/deepResearch");
    const agentApi = await import("@/api/agent");
    vi.mocked(deepApi.createDeepResearchConversation).mockResolvedValue("cid_test_deep");
    vi.mocked(agentApi.uploadFiles).mockResolvedValue({
      saved: [{ name: "notes.txt", bytes: 5 }],
      rejected: [],
    });
    window.localStorage.clear();
    // Make agentLive() return true so the lazy ensureCid callback is actionable.
    vi.spyOn(clientModule, "agentLive").mockReturnValue(true);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("UploadComposer is immediately actionable without creating a cid on mount", async () => {
    renderSurface();

    expect(screen.getByTestId("upload-composer")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /attach files/i })).toBeEnabled();
    const { createDeepResearchConversation } = await import("@/api/deepResearch");
    expect(createDeepResearchConversation).not.toHaveBeenCalled();
  });

  it("UploadComposer renders but self-disables when agentLive is false (offline/fixture mode)", async () => {
    vi.spyOn(clientModule, "agentLive").mockReturnValue(false);
    renderSurface();

    // The composer is ALWAYS rendered inline. With agentLive false no lazy cid
    // callback is exposed, so the affordance is disabled instead of vanishing.
    expect(screen.getByTestId("upload-composer")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /attach files/i })).toBeDisabled(),
    );
  });

  it("creates exactly one upload cid after a real file selection", async () => {
    const { createDeepResearchConversation } = await import("@/api/deepResearch");
    const { uploadFiles } = await import("@/api/agent");
    renderSurface();

    const input = document.querySelector(
      '[data-disco-control="build.upload-input"]',
    ) as HTMLInputElement;
    const file = new File(["notes"], "notes.txt", { type: "text/plain" });
    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() =>
      expect(createDeepResearchConversation).toHaveBeenCalledTimes(1),
    );
    const [firstCallArg] = (createDeepResearchConversation as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(firstCallArg).toMatchObject({ query: "" });
    await waitFor(() =>
      expect(uploadFiles).toHaveBeenCalledWith("cid_test_deep", [file]),
    );
  });
});
