/**
 * NeedMoreCard — vitest unit tests.
 *
 * Tests:
 *  (a) All three buttons render
 *  (b) Ask-Follow-Up toggles the input open/closed repeatedly
 *  (c) Export modal opens, shows MD/PDF/DOCX options, closes, REOPENS
 *  (d) After pressing all three, every control is still responsive
 *      (the "breaks once everything's pressed" regression)
 *  (e) Audio button shows the honest "not-wired" state, not fake success
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { NeedMoreCard } from "./NeedMoreCard";
import type { ReportEvent } from "@/types/agent";

// ── Mocks ─────────────────────────────────────────────────────────────────────

vi.mock("@/hooks/useExportCapabilities", () => ({
  useExportCapabilities: () => ({ md: true, pdf: false, docx: false }),
}));

vi.mock("@/api/deepResearch", () => ({
  exportReportAsMarkdown: vi.fn(),
  exportReport: vi.fn().mockResolvedValue(true),
  serializeReportToMarkdown: vi.fn().mockReturnValue("# Report\n\nContent"),
}));

// ── Fixtures ──────────────────────────────────────────────────────────────────

const STUB_REPORT: ReportEvent = {
  id: "evt_report_test",
  kind: "report",
  source: "agent",
  seq: 99,
  query: "What is the future of solid-state batteries?",
  summary: "Solid-state batteries look promising.",
  sections: [
    {
      id: "s1",
      title: "Current State",
      markdown: "## Current State\n\nIn progress.",
      confidence: "mixed",
      disputed_notes: [],
      cited_passage_ids: [],
      unsupported_count: 0,
    },
  ],
  passages: [],
  all_hits: [],
  unsupported_count: 0,
  bounded_by: null,
  depth_tier: "standard_deep",
};

const STUB_CID = "conv_test_abc123";

// ── Helpers ───────────────────────────────────────────────────────────────────

function renderCard(overrides?: Partial<React.ComponentProps<typeof NeedMoreCard>>) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const onFollowUp = vi.fn();
  const result = render(
    <QueryClientProvider client={qc}>
      <NeedMoreCard
        report={STUB_REPORT}
        cid={STUB_CID}
        onFollowUp={onFollowUp}
        followUpBusy={false}
        {...overrides}
      />
    </QueryClientProvider>,
  );
  return { ...result, onFollowUp };
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("NeedMoreCard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // (a) All three buttons render
  it("renders all three action buttons", () => {
    renderCard();
    expect(screen.getByRole("button", { name: /Ask a Follow-Up/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Export as/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Audio Overview/i })).toBeInTheDocument();
  });

  it("renders the 'Need More?' heading", () => {
    renderCard();
    expect(screen.getByRole("heading", { name: /Need More/i })).toBeInTheDocument();
  });

  // (b) Ask-Follow-Up toggles the input open/closed repeatedly
  it("Ask-Follow-Up toggles the follow-up input open and closed multiple times", async () => {
    const user = userEvent.setup();
    renderCard();

    const toggleBtn = screen.getByRole("button", { name: /Ask a Follow-Up/i });

    // Initially closed
    expect(screen.queryByRole("region", { name: /Follow-up input/i })).not.toBeInTheDocument();

    // First press: opens
    await user.click(toggleBtn);
    expect(screen.getByRole("region", { name: /Follow-up input/i })).toBeInTheDocument();
    expect(
      screen.getByPlaceholderText(/Expand on section/i),
    ).toBeInTheDocument();
    expect(toggleBtn).toHaveAttribute("aria-expanded", "true");

    // Second press: closes
    await user.click(toggleBtn);
    expect(screen.queryByRole("region", { name: /Follow-up input/i })).not.toBeInTheDocument();
    expect(toggleBtn).toHaveAttribute("aria-expanded", "false");

    // Third press: opens again (the "re-press" robustness check)
    await user.click(toggleBtn);
    expect(screen.getByRole("region", { name: /Follow-up input/i })).toBeInTheDocument();
    expect(toggleBtn).toHaveAttribute("aria-expanded", "true");
  });

  it("typing and submitting in the follow-up panel calls onFollowUp", async () => {
    const user = userEvent.setup();
    const { onFollowUp } = renderCard();

    await user.click(screen.getByRole("button", { name: /Ask a Follow-Up/i }));
    const textarea = screen.getByRole("textbox", { name: /Follow-up question/i });
    await user.type(textarea, "What about the cost curve?");
    await user.keyboard("{Enter}");

    expect(onFollowUp).toHaveBeenCalledWith("What about the cost curve?");
  });

  // (c) Export modal opens, shows MD/PDF/DOCX, closes, REOPENS
  it("Export modal opens showing MD/PDF/DOCX choices, closes, and reopens", async () => {
    const user = userEvent.setup();
    renderCard();

    // Open
    await user.click(screen.getByRole("button", { name: /Export as/i }));

    // Modal is open — check for the dialog content.
    // Accessible names include the subtitle text (e.g. "PDF Needs WeasyPrint on the server")
    // so we use substring regex matches rather than exact names.
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("button", { name: /Markdown/i })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: /PDF/i })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: /DOCX/i })).toBeInTheDocument();

    // Close via the X button
    await user.click(within(dialog).getByRole("button", { name: /Close export dialog/i }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );

    // Reopen (robustness — must not latch closed)
    await user.click(screen.getByRole("button", { name: /Export as/i }));
    await screen.findByRole("dialog");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("Export modal shows PDF/DOCX as disabled when server lacks the toolchain", async () => {
    const user = userEvent.setup();
    renderCard();

    await user.click(screen.getByRole("button", { name: /Export as/i }));
    const dialog = await screen.findByRole("dialog");

    // useExportCapabilities mock returns pdf:false, docx:false
    // Button names include subtitle text so we use substring regex.
    expect(within(dialog).getByRole("button", { name: /PDF/i })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: /DOCX/i })).toBeDisabled();
    // MD is always enabled
    expect(within(dialog).getByRole("button", { name: /Markdown/i })).toBeEnabled();
  });

  // (d) After pressing all three, every control is still responsive
  it("all three controls remain interactive after all have been pressed once", async () => {
    const user = userEvent.setup();
    renderCard();

    // Press Ask a Follow-Up (opens panel)
    await user.click(screen.getByRole("button", { name: /Ask a Follow-Up/i }));
    expect(screen.getByRole("region", { name: /Follow-up input/i })).toBeInTheDocument();

    // Press Export as… (opens modal)
    await user.click(screen.getByRole("button", { name: /Export as/i }));
    const dialog = await screen.findByRole("dialog");
    // Close it
    await user.click(within(dialog).getByRole("button", { name: /Close export dialog/i }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    // Press Audio Overview (goes to unavailable state after stub throws)
    await user.click(screen.getByRole("button", { name: /Audio Overview/i }));
    await waitFor(() =>
      expect(
        screen.getByText(/audio overview backend not yet available/i),
      ).toBeInTheDocument(),
    );

    // ── Regression: verify each control is STILL responsive ──

    // Ask a Follow-Up: toggle closes the panel (it was open)
    const toggleBtn = screen.getByRole("button", { name: /Ask a Follow-Up/i });
    expect(toggleBtn).toBeEnabled();
    await user.click(toggleBtn);
    expect(screen.queryByRole("region", { name: /Follow-up input/i })).not.toBeInTheDocument();
    // Re-open it
    await user.click(toggleBtn);
    expect(screen.getByRole("region", { name: /Follow-up input/i })).toBeInTheDocument();

    // Export: still opens the modal
    const exportBtn = screen.getByRole("button", { name: /Export as/i });
    expect(exportBtn).toBeEnabled();
    await user.click(exportBtn);
    await screen.findByRole("dialog");
    // Close again
    await user.click(screen.getByRole("button", { name: /Close export dialog/i }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    // Audio: the "Try again" reset button is present and clickable
    const resetAudioBtn = screen.getByRole("button", { name: /Reset audio overview state/i });
    expect(resetAudioBtn).toBeEnabled();
    await user.click(resetAudioBtn);
    // Returns to idle: Audio Overview button is back
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Audio Overview/i })).toBeInTheDocument(),
    );
  });

  // (e) Audio button shows honest not-wired state, NOT fake success
  it("Audio Overview enters 'not yet available' state immediately, never fake success", async () => {
    const user = userEvent.setup();
    renderCard();

    await user.click(screen.getByRole("button", { name: /Audio Overview/i }));

    // Should NOT see "Play overview" or "Export MP3" (those are done-state affordances)
    // Should see the honest unavailability message
    await waitFor(() =>
      expect(
        screen.getByText(/audio overview backend not yet available/i),
      ).toBeInTheDocument(),
    );

    // The stub message must appear
    expect(screen.getByText(/not yet wired/i)).toBeInTheDocument();

    // No fake success affordances
    expect(screen.queryByRole("button", { name: /Play overview/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Export MP3/i })).not.toBeInTheDocument();
  });

  it("audio unavailable state is resettable back to idle", async () => {
    const user = userEvent.setup();
    renderCard();

    await user.click(screen.getByRole("button", { name: /Audio Overview/i }));
    await waitFor(() =>
      expect(screen.getByText(/audio overview backend not yet available/i)).toBeInTheDocument(),
    );

    await user.click(screen.getByRole("button", { name: /Reset audio overview state/i }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Audio Overview/i })).toBeInTheDocument(),
    );
    expect(
      screen.queryByText(/audio overview backend not yet available/i),
    ).not.toBeInTheDocument();
  });
});
