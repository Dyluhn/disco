import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ResearchSurface } from "@/components/ResearchSurface";
import { UploadComposer } from "@/components/build/BuildSurface";
import { NeedMoreCard } from "@/components/research/NeedMoreCard";
import { ModeProvider } from "@/shell/ModeProvider";
import { useMode } from "@/shell/mode";
import type { ReportEvent } from "@/types/agent";

const apiMocks = vi.hoisted(() => ({
  uploadFiles: vi.fn(),
  createBuildConversation: vi.fn(),
}));

vi.mock("@/api/agent", async () => {
  const actual = await vi.importActual<typeof import("@/api/agent")>("@/api/agent");
  return {
    ...actual,
    uploadFiles: apiMocks.uploadFiles,
    createBuildConversation: apiMocks.createBuildConversation,
  };
});

vi.mock("@/hooks/useResearch", () => ({
  useResearch: () => ({
    scope: null,
    blocks: [],
    streamingBlockId: null,
    answer: null,
    partial: "",
    phase: "idle",
    submitting: false,
    submit: vi.fn(),
    reScope: vi.fn(),
    reset: vi.fn(),
    stop: vi.fn(),
    preCid: null,
    ensurePreCid: vi.fn(async () => "conv_standard_upload"),
    runCid: null,
    submitError: null,
  }),
}));

vi.mock("@/hooks/useDriverModels", () => ({
  useLastSelectedModel: () => ({ data: null }),
  // DriverModelNotice resolves its display label from the driver catalogue.
  useDriverModels: () => ({ data: undefined }),
}));

vi.mock("@/components/ModelLeaderPill", () => ({
  ModelLeaderPill: () => <button type="button">Model</button>,
}));

function renderWithProviders(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ModeProvider>{ui}</ModeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function ModeProbe() {
  const { mode } = useMode();
  return <div data-testid="mode-probe">{mode}</div>;
}

const report: ReportEvent = {
  id: "r1",
  kind: "report",
  query: "solid-state battery market",
  summary: "Summary [[p1]]",
  sections: [],
  passages: [
    {
      id: "p1",
      source_url: "https://example.test/source",
      source_title: "Source",
      text: "Grounding text",
    },
  ],
  all_hits: [],
  unsupported_count: 0,
  bounded_by: null,
  depth_tier: "standard_deep",
};

describe("AuthorB unbiased gate — W-06/W-07/W-13/W-24 research UI", () => {
  beforeEach(() => {
    apiMocks.uploadFiles.mockReset();
    apiMocks.uploadFiles.mockResolvedValue({ saved: [{ name: "brief.txt", bytes: 12 }], rejected: [] });
    apiMocks.createBuildConversation.mockReset();
    apiMocks.createBuildConversation.mockResolvedValue("conv_slides_authorb");
  });

  it("W-06 keeps typed draft text when toggling standard search to Deep Research and back", async () => {
    const user = userEvent.setup();
    await act(() => {
      renderWithProviders(<ResearchSurface />);
    });

    const draft = "compare battery commercialization timelines";
    await user.type(screen.getByLabelText("Ask Disco a question"), draft);

    await user.click(screen.getByRole("radio", { name: "Deep Research" }));
    expect(await screen.findByLabelText("Ask Disco a question")).toHaveValue(draft);

    await user.click(screen.getByRole("radio", { name: "Search" }));
    expect(await screen.findByLabelText("Ask Disco a question")).toHaveValue(draft);
  });

  it("W-07 Attach is usable before a cid exists and uploads through the lazy cid pipeline", async () => {
    const ensureCid = vi.fn(async () => "conv_lazy_upload");
    const uploaded = vi.fn();
    const { container } = render(<UploadComposer cid={null} ensureCid={ensureCid} onUploaded={uploaded} />);

    expect(screen.getByRole("button", { name: "Attach files" })).toBeEnabled();

    const input = container.querySelector("input[type='file']") as HTMLInputElement;
    const file = new File(["hello"], "note.txt", { type: "text/plain" });
    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() => expect(apiMocks.uploadFiles).toHaveBeenCalledTimes(1));
    expect(ensureCid).toHaveBeenCalledTimes(1);
    expect(apiMocks.uploadFiles).toHaveBeenCalledWith("conv_lazy_upload", [file]);
    expect(uploaded).toHaveBeenCalledTimes(1);
  });

  it("W-13/W-24 Build Slides handoff creates an autonomous agent run and flips the surface toggle to Agent", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <>
        <ModeProbe />
        <NeedMoreCard report={report} cid="dr_cid" onFollowUp={() => {}} followUpBusy={false} />
      </>,
    );

    expect(screen.getByTestId("mode-probe")).toHaveTextContent("search");
    await user.click(screen.getByRole("button", { name: /build/i }));

    await waitFor(() =>
      expect(apiMocks.createBuildConversation).toHaveBeenCalledWith(
        null,
        "agent",
        true,
        null,
        // quiet rides the chat-verbosity preference, which now defaults to
        // quiet-on (verbose off).
        true,
      ),
    );
    expect(screen.getByTestId("mode-probe")).toHaveTextContent("agent");
  });
});
