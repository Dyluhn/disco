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
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NeedMoreCard } from "./NeedMoreCard";
import { ModeProvider } from "@/shell/ModeProvider";
import { useMode } from "@/shell/mode";
import type { ReportEvent } from "@/types/agent";

// ── Mocks ─────────────────────────────────────────────────────────────────────

// Mutable export capabilities so individual tests can enable PDF.
// Default mirrors a server without WeasyPrint (pdf:false).
const _caps = vi.hoisted(() => ({
  current: { md: true, pdf: false } as {
    md: boolean;
    pdf: boolean;
  },
}));
vi.mock("@/hooks/useExportCapabilities", () => ({
  useExportCapabilities: () => _caps.current,
}));
vi.mock("@/hooks/useTemplates", () => ({
  useTemplates: () => [
    { id: "disco-light", name: "disco", mode: "light", label: "Disco", description: "Default", accent: "#4077a3", bg: "#fcfcfa", default: true },
    { id: "midnight-dark", name: "midnight", mode: "dark", label: "Midnight", description: "Dark", accent: "#d9a441", bg: "#0d1017", default: false },
  ],
}));

// Epic 12-C: spread the real module before overriding. Amendment A3 moved the
// report export/audio TRANSPORT into this api module (raw fetch is forbidden
// under src/components/ and src/hooks/), so a wholesale replacement would
// resolve those functions to `undefined` for every file in the render tree.
// The four stubs below are unchanged; everything else now passes through to the
// real implementation, which calls agentFetch -> the global `fetch` this file
// already stubs via vi.stubGlobal. No assertion is altered.
vi.mock("@/api/deepResearch", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/deepResearch")>()),
  exportReportAsMarkdown: vi.fn(),
  exportReport: vi.fn().mockResolvedValue(true),
  serializeReportToMarkdown: vi.fn().mockReturnValue("# Report\n\nContent"),
  // requestReportAudio is no longer used in NeedMoreCard (WALK-21: direct fetch
  // with ?mode= param instead). The mock is kept so existing import graph resolves.
  requestReportAudio: vi.fn(),
}));

// Mock the fetch used by AudioSection.generate so we don't need a real
// agent-server in unit tests.  Returns a podcast-mode mp3_url by default.
const _fetchMock = vi.fn().mockResolvedValue({
  ok: true,
  json: vi.fn().mockResolvedValue({
    mp3_url: "/conversations/c1/report/audio/audio_overview_podcast.mp3",
    transcript_url: "/conversations/c1/report/audio/audio_overview_podcast.md",
  }),
});
vi.stubGlobal("fetch", _fetchMock);

// Build a fake SSE Response whose body.getReader() yields the given progress
// events as `data: {json}` frames, then HANGS (read never resolves) so the UI
// stays on the last emitted stage — letting a test observe mid-stream status.
function makeSseHangingResponse(events: Array<Record<string, unknown>>) {
  const enc = new TextEncoder();
  const chunks = events.map((e) => enc.encode(`data: ${JSON.stringify(e)}\n\n`));
  let idx = 0;
  return {
    ok: true,
    headers: { get: () => "text/event-stream" },
    body: {
      getReader() {
        return {
          read() {
            if (idx < chunks.length) {
              return Promise.resolve({ done: false, value: chunks[idx++] });
            }
            return new Promise(() => {}); // hang: hold the last stage on screen
          },
        };
      },
    },
  };
}

// A fake SSE Response that emits the events and then closes (done) — used for the
// happy terminal-event path (e.g. a final "done" frame).
function makeSseResponse(events: Array<Record<string, unknown>>) {
  const enc = new TextEncoder();
  const chunks = events.map((e) => enc.encode(`data: ${JSON.stringify(e)}\n\n`));
  let idx = 0;
  return {
    ok: true,
    headers: { get: () => "text/event-stream" },
    body: {
      getReader() {
        return {
          read() {
            if (idx < chunks.length) {
              return Promise.resolve({ done: false, value: chunks[idx++] });
            }
            return Promise.resolve({ done: true, value: undefined });
          },
        };
      },
    },
  };
}

// A5: spy on navigation + the build-conversation create for the "Build a deck" handoff.
const _navMock = vi.hoisted(() => vi.fn());
vi.mock("react-router-dom", async (orig) => ({
  ...(await orig<typeof import("react-router-dom")>()),
  useNavigate: () => _navMock,
}));
const _createBuild = vi.hoisted(() => vi.fn().mockResolvedValue("conv_new_deck"));
vi.mock("@/api/agent", async (orig) => ({
  ...(await orig<typeof import("@/api/agent")>()),
  createBuildConversation: _createBuild,
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

// W-24: NeedMoreCard now reads the mode context (Build Slides switches the slider
// to Agent), so it MUST render inside a ModeProvider. A tiny readout exposes the
// live mode so a test can assert the switch.
function ModeReadout() {
  const { mode } = useMode();
  return <span data-testid="active-mode">{mode}</span>;
}

function renderCard(overrides?: Partial<React.ComponentProps<typeof NeedMoreCard>>) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const onFollowUp = vi.fn();
  const result = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ModeProvider>
          <ModeReadout />
          <NeedMoreCard
            report={STUB_REPORT}
            cid={STUB_CID}
            onFollowUp={onFollowUp}
            followUpBusy={false}
            {...overrides}
          />
        </ModeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...result, onFollowUp };
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("NeedMoreCard", () => {
  let anchorClick: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.clearAllMocks();
    // Downloads are verified through filename/body/object-URL assertions. Stub
    // only the browser navigation primitive that jsdom intentionally lacks.
    anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => {});
    // Restore default capabilities (pdf off) before each test.
    _caps.current = { md: true, pdf: false };
  });

  afterEach(() => {
    anchorClick.mockRestore();
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

  // A5: the "Build a deck" handoff creates a conversation on the AGENT surface and
  // navigates to it with the serialized report as the seed task. runthru-v2 #7 routes
  // slide-making to the AGENT surface (task framing, not the build/live-preview
  // framing); W-13 runs it AUTONOMOUS so the seeded slides plan auto-approves —
  // createBuildConversation(null, "agent", true) + navigate to /agent/<cid>.
  it("A5/W-13: 'Build a deck' starts an autonomous agent run seeded with the report", async () => {
    const user = userEvent.setup();
    renderCard();

    await user.click(screen.getByRole("button", { name: /Build a deck/i }));

    await waitFor(() => expect(_createBuild).toHaveBeenCalledWith(null, "agent", true, null, expect.any(Boolean)));
    await waitFor(() => expect(_navMock).toHaveBeenCalled());
    const [path, opts] = _navMock.mock.calls[0] as [
      string,
      { state: { seedTask: string; seedContext: string } },
    ];
    expect(path).toBe("/agent/conv_new_deck");
    // R3: the VISIBLE seed is a short one-liner (not the whole report dumped inline).
    expect(opts.state.seedTask).toMatch(/make slides for the deep research report/i);
    expect(opts.state.seedTask).not.toContain("# Report");
    // The serialized report rides along as HIDDEN seedContext (an ENVIRONMENT message).
    expect(opts.state.seedContext).toContain("# Report");
    expect(opts.state.seedContext).toMatch(/slides_generate/);
  });

  // W-24: the slide handoff lands on the AGENT surface, so the 3-way mode slider
  // must switch to Agent — otherwise it stayed on Search while Agent rendered.
  it("W-24: 'Build a deck' switches the mode to Agent", async () => {
    const user = userEvent.setup();
    renderCard();

    // Starts on Search (the slider's default).
    expect(screen.getByTestId("active-mode")).toHaveTextContent("search");

    await user.click(screen.getByRole("button", { name: /Build a deck/i }));

    // After the handoff the mode is Agent (the slider reflects Agent).
    await waitFor(() =>
      expect(screen.getByTestId("active-mode")).toHaveTextContent("agent"),
    );
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

  // (c) Export modal opens, shows MD/PDF (W-12: DOCX removed), closes, REOPENS
  it("Export modal opens showing MD/PDF choices, closes, and reopens", async () => {
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
    // W-12: DOCX export was removed end to end — no DOCX choice in the modal.
    expect(within(dialog).queryByRole("button", { name: /DOCX/i })).not.toBeInTheDocument();

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

  it("gives every report action dialog a Radix-backed accessible name", async () => {
    const user = userEvent.setup();
    const consoleError = vi.spyOn(console, "error");

    try {
      renderCard();

      await user.click(screen.getByRole("button", { name: /Export as/i }));
      const exportDialog = await screen.findByRole("dialog", { name: "Export report" });
      const exportTitleId = exportDialog.getAttribute("aria-labelledby");
      expect(exportTitleId).toBeTruthy();
      expect(document.getElementById(exportTitleId!)).toHaveTextContent("Export report");

      await user.click(
        within(exportDialog).getByRole("button", { name: /Close export dialog/i }),
      );
      await waitFor(() => expect(exportDialog).not.toBeInTheDocument());

      await user.click(screen.getByRole("button", { name: /Audio Overview/i }));
      const audioDialog = await screen.findByRole("dialog", {
        name: "Generate audio overview",
      });
      const audioTitleId = audioDialog.getAttribute("aria-labelledby");
      expect(audioTitleId).toBeTruthy();
      expect(document.getElementById(audioTitleId!)).toHaveTextContent(
        "Generate audio overview",
      );
      expect(audioTitleId).not.toBe(exportTitleId);

      const titleContractErrors = consoleError.mock.calls.filter(
        ([message]) =>
          typeof message === "string" &&
          message.includes("DialogContent") &&
          message.includes("DialogTitle"),
      );
      expect(titleContractErrors).toHaveLength(0);
    } finally {
      consoleError.mockRestore();
    }
  });

  it("Export modal shows PDF as disabled when server lacks WeasyPrint", async () => {
    const user = userEvent.setup();
    renderCard();

    await user.click(screen.getByRole("button", { name: /Export as/i }));
    const dialog = await screen.findByRole("dialog");

    // useExportCapabilities mock returns pdf:false
    // Button names include subtitle text so we use substring regex.
    expect(within(dialog).getByRole("button", { name: /PDF/i })).toBeDisabled();
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

    // Press Audio Overview → opens mode chooser dialog → pick Podcast
    await user.click(screen.getByRole("button", { name: /Audio Overview/i }));
    const modeDialog = await screen.findByRole("dialog", { name: /Generate audio overview/i });
    await user.click(within(modeDialog).getByRole("button", { name: /Podcast style/i }));

    // Done state: AudioPlayer + Export MP3 (real, not a stub)
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Export MP3/i })).toBeInTheDocument(),
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

    // Audio: success → done state; "Regenerate" resets back to idle
    const regenBtn = screen.getByRole("button", { name: /Regenerate/i });
    expect(regenBtn).toBeEnabled();
    await user.click(regenBtn);
    // Returns to idle: Audio Overview button is back
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Audio Overview/i })).toBeInTheDocument(),
    );
  });

  // (e) Audio Overview → mode dialog → Podcast → success shows the player + Export MP3
  it("Audio Overview mode dialog: Podcast choice shows player + Export MP3", async () => {
    const user = userEvent.setup();
    renderCard();

    await user.click(screen.getByRole("button", { name: /Audio Overview/i }));

    // Mode chooser dialog opens
    const modeDialog = await screen.findByRole("dialog", { name: /Generate audio overview/i });
    expect(
      within(modeDialog).getByRole("button", { name: /Podcast style/i }),
    ).toBeInTheDocument();
    expect(
      within(modeDialog).getByRole("button", { name: /Single speaker/i }),
    ).toBeInTheDocument();
    // "alpha" badge is visible on the Podcast option
    expect(within(modeDialog).getByText(/alpha/i)).toBeInTheDocument();

    // Pick Podcast
    await user.click(within(modeDialog).getByRole("button", { name: /Podcast style/i }));

    // Done state: custom AudioPlayer + Export MP3 (real, not a stub)
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Export MP3/i })).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /Regenerate/i })).toBeInTheDocument();
    // UI-23: "Audio Overview" and "Regenerate" were two buttons for one action
    // once a player existed. Regenerate is the only one left.
    expect(screen.queryByRole("button", { name: /^Audio Overview$/i })).not.toBeInTheDocument();
    // AudioPlayer renders (play button, seek slider)
    expect(
      screen.getByRole("button", { name: /Play audio overview/i }),
    ).toBeInTheDocument();
  });

  // UI-23: Regenerate must put the offer back, or the card would be a dead end.
  it("brings the Audio Overview button back when Regenerate resets the section", async () => {
    const user = userEvent.setup();
    renderCard();

    await user.click(screen.getByRole("button", { name: /Audio Overview/i }));
    const modeDialog = await screen.findByRole("dialog", { name: /Generate audio overview/i });
    await user.click(within(modeDialog).getByRole("button", { name: /Podcast style/i }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Regenerate/i })).toBeInTheDocument(),
    );

    await user.click(screen.getByRole("button", { name: /Regenerate/i }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^Audio Overview$/i })).toBeInTheDocument(),
    );
  });

  // (f) Audio Overview → mode dialog → Single speaker → success
  it("Audio Overview mode dialog: Single speaker choice also succeeds", async () => {
    const user = userEvent.setup();
    // Override fetch to return a single-mode URL
    _fetchMock.mockResolvedValueOnce({
      ok: true,
      json: vi.fn().mockResolvedValue({
        mp3_url: "/conversations/c1/report/audio/audio_overview_single.mp3",
        transcript_url: "/conversations/c1/report/audio/audio_overview_single.md",
      }),
    });
    renderCard();

    await user.click(screen.getByRole("button", { name: /Audio Overview/i }));
    const modeDialog = await screen.findByRole("dialog", { name: /Generate audio overview/i });
    await user.click(within(modeDialog).getByRole("button", { name: /Single speaker/i }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Export MP3/i })).toBeInTheDocument(),
    );
  });

  // (g) Mode chooser dialog can be dismissed without generating
  it("closing the mode dialog returns to idle without generating", async () => {
    const user = userEvent.setup();
    renderCard();

    await user.click(screen.getByRole("button", { name: /Audio Overview/i }));
    const modeDialog = await screen.findByRole("dialog", { name: /Generate audio overview/i });

    // Close via the X button
    await user.click(
      within(modeDialog).getByRole("button", { name: /Close audio mode dialog/i }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    // Still in idle — Audio Overview button is still there
    expect(screen.getByRole("button", { name: /Audio Overview/i })).toBeInTheDocument();
  });

  it("audio failure surfaces an honest reason and is resettable to idle", async () => {
    const user = userEvent.setup();
    // Both the SSE stream attempt and the blocking fallback return the error
    // (the stream's non-OK response makes the FE fall back, which re-surfaces it).
    _fetchMock.mockResolvedValue({
      ok: false,
      body: null,
      json: vi.fn().mockResolvedValue({ detail: { reason: "tts_disabled" } }),
    });
    renderCard();

    await user.click(screen.getByRole("button", { name: /Audio Overview/i }));
    const modeDialog = await screen.findByRole("dialog", { name: /Generate audio overview/i });
    await user.click(within(modeDialog).getByRole("button", { name: /Podcast style/i }));

    await waitFor(() =>
      expect(screen.getAllByText(/disabled in Settings/i).length).toBeGreaterThan(0),
    );

    // Resettable back to idle via "Try again"
    await user.click(screen.getByRole("button", { name: /Reset audio overview state/i }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Audio Overview/i })).toBeInTheDocument(),
    );
    expect(screen.queryAllByText(/disabled in Settings/i)).toHaveLength(0);
  });

  // (h) W-08: the generating state shows an honest label and NO false download
  // notice when no download is happening (the blocking fallback has no progress).
  it("generating state shows a generic label without a false download notice", async () => {
    const user = userEvent.setup();
    // Hang the FIRST fetch (the SSE stream attempt) so we observe "generating"
    // with no progress events — the download notice must NOT appear.
    let resolveHang!: (value: unknown) => void;
    _fetchMock.mockReturnValueOnce(new Promise((res) => { resolveHang = res; }));
    renderCard();

    await user.click(screen.getByRole("button", { name: /Audio Overview/i }));
    const modeDialog = await screen.findByRole("dialog", { name: /Generate audio overview/i });
    await user.click(within(modeDialog).getByRole("button", { name: /Podcast style/i }));

    await waitFor(() =>
      expect(screen.getByText(/Generating audio…/i)).toBeInTheDocument(),
    );
    // W-08: no genuine download → the ~300 MB notice must NOT be shown.
    expect(screen.queryByText(/Downloading voice model/i)).not.toBeInTheDocument();

    // Unblock with a non-stream response → falls back to the blocking endpoint.
    resolveHang({ ok: false, body: null, json: vi.fn().mockResolvedValue({}) });
  });

  // (h2) W-08: the download notice appears ONLY when the stream reports a real
  // first-run voice-model download.
  it("shows the honest download notice only on the downloading_model stage", async () => {
    const user = userEvent.setup();
    _fetchMock.mockResolvedValueOnce(
      makeSseHangingResponse([{ stage: "preparing" }, { stage: "downloading_model" }]),
    );
    renderCard();

    await user.click(screen.getByRole("button", { name: /Audio Overview/i }));
    const modeDialog = await screen.findByRole("dialog", { name: /Generate audio overview/i });
    await user.click(within(modeDialog).getByRole("button", { name: /Podcast style/i }));

    await waitFor(() =>
      expect(
        screen.getByText(/Downloading voice model.*first run only/i),
      ).toBeInTheDocument(),
    );
  });

  // (h3) W-09: the stream's synthesizing stage renders staged progress, and the
  // download notice is NOT shown during synthesis.
  it("renders staged synthesizing progress from the SSE stream", async () => {
    const user = userEvent.setup();
    _fetchMock.mockResolvedValueOnce(
      makeSseHangingResponse([{ stage: "synthesizing", current: 3, total: 8 }]),
    );
    renderCard();

    await user.click(screen.getByRole("button", { name: /Audio Overview/i }));
    const modeDialog = await screen.findByRole("dialog", { name: /Generate audio overview/i });
    await user.click(within(modeDialog).getByRole("button", { name: /Podcast style/i }));

    await waitFor(() =>
      expect(screen.getByText(/Synthesizing turns 3\/8/i)).toBeInTheDocument(),
    );
    expect(document.querySelector('[data-tts-state="generating"]')).toHaveAttribute(
      "data-tts-stage",
      "synthesizing",
    );
    expect(screen.queryByText(/Downloading voice model/i)).not.toBeInTheDocument();
  });

  // (h4) W-09: a terminal "done" frame from the stream resolves to the player.
  it("finishes via the SSE stream's done event", async () => {
    const user = userEvent.setup();
    _fetchMock.mockResolvedValueOnce(
      makeSseResponse([
        { stage: "preparing" },
        { stage: "synthesizing", current: 1, total: 2 },
        { stage: "mixing" },
        {
          stage: "done",
          mp3_url: "/conversations/c1/report/audio/audio_overview_podcast.mp3",
        },
      ]),
    );
    renderCard();

    await user.click(screen.getByRole("button", { name: /Audio Overview/i }));
    const modeDialog = await screen.findByRole("dialog", { name: /Generate audio overview/i });
    await user.click(within(modeDialog).getByRole("button", { name: /Podcast style/i }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Export MP3/i })).toBeInTheDocument(),
    );
  });

  // (i) The PDF template selector threads `theme` + `mode` into the export POST body.
  describe("PDF template selector", () => {
    const findExportCall = () =>
      _fetchMock.mock.calls.find(([url]) =>
        String(url).includes("/report/export?fmt=pdf"),
      );

    function stubBlobAndUrl() {
      // PDF export goes through the non-FSA download path (jsdom has no FSA),
      // so stub object-URL plumbing and give fetch a blob() to download.
      vi.stubGlobal("URL", {
        ...URL,
        createObjectURL: vi.fn(() => "blob:mock"),
        revokeObjectURL: vi.fn(),
      });
      _fetchMock.mockResolvedValue({
        ok: true,
        blob: vi.fn().mockResolvedValue(new Blob(["%PDF-1.7"], { type: "application/pdf" })),
      });
    }

    it("defaults to Disco and POSTs theme:disco mode:light when exporting PDF", async () => {
      _caps.current = { md: true, pdf: true };
      stubBlobAndUrl();
      const user = userEvent.setup();
      renderCard();

      await user.click(screen.getByRole("button", { name: /Export as/i }));
      const dialog = await screen.findByRole("dialog");

      // The template picker is present and defaults to Disco (disco-light).
      const select = within(dialog).getByLabelText(/PDF template/i) as HTMLSelectElement;
      expect(select.value).toBe("disco-light");

      await user.click(within(dialog).getByRole("button", { name: /PDF/i }));

      await waitFor(() => expect(findExportCall()).toBeTruthy());
      const [, init] = findExportCall()!;
      const body = JSON.parse((init as RequestInit).body as string);
      expect(body.theme).toBe("disco");
      expect(body.mode).toBe("light");
    });

    it("selecting Midnight then exporting PDF POSTs theme:midnight mode:dark", async () => {
      _caps.current = { md: true, pdf: true };
      stubBlobAndUrl();
      const user = userEvent.setup();
      renderCard();

      await user.click(screen.getByRole("button", { name: /Export as/i }));
      const dialog = await screen.findByRole("dialog");

      // Pick the Midnight (dark) template.
      const select = within(dialog).getByLabelText(/PDF template/i);
      await user.selectOptions(select, "midnight-dark");

      await user.click(within(dialog).getByRole("button", { name: /PDF/i }));

      await waitFor(() => expect(findExportCall()).toBeTruthy());
      const [, init] = findExportCall()!;
      const body = JSON.parse((init as RequestInit).body as string);
      expect(body.theme).toBe("midnight");
      expect(body.mode).toBe("dark");
    });

    it("hides the template picker when PDF export is unavailable", async () => {
      // Default caps: pdf:false → no picker (no dead control).
      const user = userEvent.setup();
      renderCard();

      await user.click(screen.getByRole("button", { name: /Export as/i }));
      const dialog = await screen.findByRole("dialog");
      expect(
        within(dialog).queryByLabelText(/PDF template/i),
      ).not.toBeInTheDocument();
    });
  });
  // UI-25: the same actions are offered in the top bar; the card runs them.
  it("opens the follow-up input when the top bar asks for a follow-up", async () => {
    renderCard({ requestedAction: { name: "follow_up", nonce: 1 } });
    await waitFor(() =>
      expect(screen.getByRole("region", { name: /Follow-up input/i })).toBeInTheDocument(),
    );
  });

  it("opens the audio mode dialog when the top bar asks for audio", async () => {
    renderCard({ requestedAction: { name: "audio", nonce: 1 } });
    expect(
      await screen.findByRole("dialog", { name: /Generate audio overview/i }),
    ).toBeInTheDocument();
  });

  it("ignores a re-render that carries the same press", async () => {
    const { rerender, onFollowUp } = renderCard({
      requestedAction: { name: "follow_up", nonce: 1 },
    });
    await waitFor(() =>
      expect(screen.getByRole("region", { name: /Follow-up input/i })).toBeInTheDocument(),
    );
    const user = userEvent.setup();
    // Close it by hand, then re-render with the SAME press: it must stay closed.
    await user.click(screen.getByRole("button", { name: /Ask a Follow-Up/i }));
    expect(screen.queryByRole("region", { name: /Follow-up input/i })).not.toBeInTheDocument();
    rerender(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter>
          <ModeProvider>
            <ModeReadout />
            <NeedMoreCard
              report={STUB_REPORT}
              cid={STUB_CID}
              onFollowUp={onFollowUp}
              followUpBusy={false}
              requestedAction={{ name: "follow_up", nonce: 1 }}
            />
          </ModeProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.queryByRole("region", { name: /Follow-up input/i })).not.toBeInTheDocument();
  });
});
