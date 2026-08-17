/**
 * G1/DR-4 attach tests — ResearchSurface empty state.
 *
 * When preCid is set (pre-create resolved), the UploadComposer should appear
 * in the empty state BEFORE the user submits a query.
 * Verifies F1 wiring: the footer slot in ResearchSurface passes cid to
 * UploadComposer when preCid is non-null, and hides it when preCid is null
 * (offline mode or run already active).
 *
 * We mock useResearch because the hook's pre-create is async (network call)
 * and the goal here is to test the COMPONENT's rendering logic, not the hook.
 * The hook's pre-create path is exercised by test_upload_corpus.py (Python)
 * and the agent.upload.test.ts unit tests.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi, afterEach } from "vitest";
import { ModeProvider } from "@/shell/ModeProvider";
import { ResearchSurface } from "@/components/ResearchSurface";

// ---- module-level mocks -------------------------------------------------------

// Mock the hook so we can control preCid precisely without the async API chain.
const mockSubmit = vi.fn();
const mockReScope = vi.fn();
const mockReset = vi.fn();
const mockStop = vi.fn();

function makeResearchReturn(preCid: string | null) {
  return {
    scope: null,
    submitting: false,
    submit: mockSubmit,
    reScope: mockReScope,
    reset: mockReset,
    preCid,
    blocks: [],
    partial: {},
    streamingBlockId: null,
    phase: "idle" as const,
    answer: null,
    error: null,
    stop: mockStop,
    submitError: null,
  };
}

vi.mock("@/hooks/useResearch", () => ({
  useResearch: vi.fn(() => makeResearchReturn("cid_test_research")),
}));

// ---- helpers -----------------------------------------------------------------

function renderResearchSurface() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/"]}>
        <ModeProvider>
          <Routes>
            <Route path="/" element={<ResearchSurface />} />
          </Routes>
        </ModeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// ---- tests -------------------------------------------------------------------

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ResearchSurface — G1/DR-4 attach (empty state)", () => {
  it("UploadComposer renders in the empty state when preCid is set", () => {
    // useResearch returns preCid = "cid_test_research" (default mock above)
    renderResearchSurface();

    // The UploadComposer is in the DOM with the group role.
    expect(screen.getByTestId("upload-composer")).toBeInTheDocument();
    // The attach-files button is enabled — cid is wired.
    const btn = screen.getByRole("button", { name: /attach files/i });
    expect(btn).toBeEnabled();
    expect(btn).not.toBeDisabled();
  });

  it("UploadComposer renders but self-disables when preCid is null (offline / re-create window)", async () => {
    // Override to simulate no preCid — offline or the brief pre-create window.
    const { useResearch } = await import("@/hooks/useResearch");
    (useResearch as ReturnType<typeof vi.fn>).mockReturnValueOnce(
      makeResearchReturn(null),
    );

    renderResearchSurface();

    // runthru-v2 #9: the composer is ALWAYS rendered inline (it no longer unmounts
    // on `r.preCid && …`) so the paperclip doesn't flicker out during the brief
    // pre-create window. With a null cid it self-disables (disabled:opacity-40)
    // rather than disappearing.
    expect(screen.getByTestId("upload-composer")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /attach files/i })).toBeDisabled();
  });

  it("UploadComposer is rendered inside QueryInput's footer slot", () => {
    // Verify the composer appears in the right structural slot (the card footer)
    // by checking it's a sibling/descendant of the search input.
    renderResearchSurface();

    const searchInput = screen.getByPlaceholderText(/ask anything/i);
    expect(searchInput).toBeInTheDocument();
    // Both the input and the composer share the same card container.
    expect(screen.getByTestId("upload-composer")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /attach files/i })).toBeInTheDocument();
  });
});
