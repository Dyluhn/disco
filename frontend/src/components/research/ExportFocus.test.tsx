/** Real report Export dialog focus restoration and export compatibility. */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NeedMoreCard } from "./NeedMoreCard";
import { ModeProvider } from "@/shell/ModeProvider";
import type { MessageEvent, ReportEvent } from "@/types/agent";

// ── Mocks (mirror NeedMoreCard.test.tsx) ─────────────────────────────────────

const _caps = vi.hoisted(() => ({
  current: { md: true, pdf: false } as { md: boolean; pdf: boolean },
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
vi.mock("@/api/deepResearch", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/deepResearch")>()),
  exportReportAsMarkdown: vi.fn(),
  exportReport: vi.fn().mockResolvedValue(true),
  serializeReportToMarkdown: vi.fn().mockReturnValue("# Report\n\nContent"),
  requestReportAudio: vi.fn(),
  fetchExistingReportAudio: vi.fn().mockResolvedValue([]),
}));

const _fetchMock = vi.fn().mockResolvedValue({
  ok: true,
  json: vi.fn().mockResolvedValue({
    mp3_url: "/conversations/c1/report/audio/audio_overview_podcast.mp3",
    transcript_url: "/conversations/c1/report/audio/audio_overview_podcast.md",
  }),
});
vi.stubGlobal("fetch", _fetchMock);

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

// ── Fixtures (arbitrary IDs: nothing here may depend on a fixed cid/seq) ─────

const STUB_REPORT_A: ReportEvent = {
  id: "evt_report_focus_a",
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
const STUB_REPORT_B: ReportEvent = { ...STUB_REPORT_A, id: "evt_report_focus_b" };

// Two answered post-report follow-up pairs (WALK-20 handoff path).
const STUB_FOLLOW_UPS = [
  { seq: 101, message: { role: "user", content: "What about the cost curve?" } },
  { seq: 102, message: { role: "assistant", content: "Costs fall with scale." } },
  { seq: 103, message: { role: "user", content: "Which vendors lead?" } },
  { seq: 104, message: { role: "assistant", content: "Several pilot lines." } },
] as unknown as MessageEvent[];

// ── Helpers ──────────────────────────────────────────────────────────────────

function renderCard(
  overrides?: Partial<React.ComponentProps<typeof NeedMoreCard>>,
  extra?: React.ReactNode,
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onFollowUp = vi.fn();
  const result = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ModeProvider>
          <NeedMoreCard
            report={STUB_REPORT_A}
            cid="conv_focus_a"
            onFollowUp={onFollowUp}
            followUpBusy={false}
            {...overrides}
          />
          {extra}
        </ModeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...result, onFollowUp };
}

function getExportOpener(scope = screen) {
  return scope.getByRole("button", { name: /Export as/i }) as HTMLButtonElement;
}

/** Keyboard-activate the real Export opener (Enter on the focused button). */
async function keyboardOpenExport(user: ReturnType<typeof userEvent.setup>, opener: HTMLElement) {
  opener.focus();
  expect(document.activeElement).toBe(opener);
  await user.keyboard("{Enter}");
  return screen.findByRole("dialog", { name: "Export report" });
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe("ExportFocus", () => {
  let anchorClick: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.clearAllMocks();
    anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => {});
    _caps.current = { md: true, pdf: false };
  });

  afterEach(() => {
    anchorClick.mockRestore();
  });

  // BASELINE-FAIL: keyboard open -> Close button must return focus to the
  // connected Export opener (deployed 4a8ff leaves BODY instead).
  it("Close returns focus to the keyboard-activated Export opener", async () => {
    const user = userEvent.setup();
    renderCard();
    const opener = getExportOpener();

    const dialog = await keyboardOpenExport(user, opener);
    expect(dialog).toBeInTheDocument();
    // Focus moved into the real dialog (not left on the opener).
    expect(dialog.contains(document.activeElement)).toBe(true);

    await user.click(within(dialog).getByRole("button", { name: /Close export dialog/i }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    expect(document.activeElement).toBe(opener);
  });

  // BASELINE-FAIL: keyboard open -> Escape must return focus to the opener.
  it("Escape returns focus to the keyboard-activated Export opener", async () => {
    const user = userEvent.setup();
    renderCard();
    const opener = getExportOpener();

    await keyboardOpenExport(user, opener);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    expect(document.activeElement).toBe(opener);
  });

  // BASELINE-FAIL (return leg) / PASS-BASE (containment + reopen legs):
  // focus stays inside the open dialog across Tab, and repeated open/close
  // sessions keep working with focus returning to the opener each time.
  it("contains focus while open and returns focus on repeated sessions", async () => {
    const user = userEvent.setup();
    renderCard();
    const opener = getExportOpener();

    // Session 1.
    const dialog1 = await keyboardOpenExport(user, opener);
    for (let i = 0; i < 8; i += 1) {
      await user.tab();
      expect(dialog1.contains(document.activeElement)).toBe(true);
    }
    await user.click(within(dialog1).getByRole("button", { name: /Close export dialog/i }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(document.activeElement).toBe(opener);

    // Session 2 (reopen must not latch closed; keyboard path again).
    const dialog2 = await keyboardOpenExport(user, opener);
    expect(dialog2).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(document.activeElement).toBe(opener);
  });

  // BASELINE-FAIL: real include-followups handoff (Export -> include modal ->
  // confirm -> Export dialog), then Close must return to the connected Export
  // opener — not to the include confirm button, not to BODY.
  it("include-followups handoff lands in Export and Close returns to the Export opener", async () => {
    const user = userEvent.setup();
    renderCard({ followUps: STUB_FOLLOW_UPS });
    const opener = getExportOpener();

    // Keyboard-activate Export: the include selector opens first (WALK-20).
    opener.focus();
    await user.keyboard("{Enter}");
    const includeDialog = await screen.findByRole("dialog", { name: "Include follow-ups?" });
    expect(includeDialog).toBeInTheDocument();
    // Export itself must NOT be open yet.
    expect(screen.queryByRole("dialog", { name: "Export report" })).not.toBeInTheDocument();

    // Confirm the handoff with the real rendered confirm control.
    const confirm = within(includeDialog).getByRole("button", {
      name: /Export with 2 follow-ups/i,
    });
    await user.click(confirm);

    // Handoff control: the actual Export dialog opens with capabilities intact.
    const exportDialog = await screen.findByRole("dialog", { name: "Export report" });
    expect(within(exportDialog).getByRole("button", { name: /Markdown/i })).toBeEnabled();
    expect(screen.queryByRole("dialog", { name: "Include follow-ups?" })).not.toBeInTheDocument();

    await user.click(
      within(exportDialog).getByRole("button", { name: /Close export dialog/i }),
    );
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Export report" })).not.toBeInTheDocument(),
    );
    expect(document.activeElement).toBe(opener);
    expect(document.activeElement).not.toBe(confirm);
  });

  // EXPLORATORY: include-cancel (X) return target is NOT proven safe — this
  // test only pins what must hold regardless: no Export opens, and focus must
  // not jump to an unrelated element. The opener-return hypothesis for this
  // path is deliberately NOT asserted here.
  it("include-cancel opens no Export and steals focus to no unrelated element", async () => {
    const user = userEvent.setup();
    renderCard({ followUps: STUB_FOLLOW_UPS }, <button type="button">Unrelated</button>);
    const opener = getExportOpener();
    const unrelated = screen.getByRole("button", { name: "Unrelated" });

    opener.focus();
    await user.keyboard("{Enter}");
    const includeDialog = await screen.findByRole("dialog", { name: "Include follow-ups?" });
    await user.click(within(includeDialog).getByRole("button", { name: "Close dialog" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Include follow-ups?" })).not.toBeInTheDocument(),
    );

    expect(screen.queryByRole("dialog", { name: "Export report" })).not.toBeInTheDocument();
    expect(document.activeElement).not.toBe(unrelated);
  });

  // PASS-BASE (negative control): closing Export must not move focus to an
  // unrelated element. Holds even on the broken base (BODY), so it separates
  // "wrong target" regressions from the known "no return" failure.
  it("closing Export never focuses an unrelated element", async () => {
    const user = userEvent.setup();
    renderCard(undefined, <button type="button">Unrelated</button>);
    const opener = getExportOpener();
    const unrelated = screen.getByRole("button", { name: "Unrelated" });

    const dialog = await keyboardOpenExport(user, opener);
    await user.click(within(dialog).getByRole("button", { name: /Close export dialog/i }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    expect(document.activeElement).not.toBe(unrelated);
  });

  // BASELINE-FAIL: with multiple cards/openers, closing one Export must return
  // to ITS connected opener, not to another card's Export button.
  it("with two cards, Close returns to the connected opener only", async () => {
    const user = userEvent.setup();
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ModeProvider>
            <NeedMoreCard
              report={STUB_REPORT_A}
              cid="conv_focus_a"
              onFollowUp={vi.fn()}
              followUpBusy={false}
            />
            <NeedMoreCard
              report={STUB_REPORT_B}
              cid="conv_focus_b"
              onFollowUp={vi.fn()}
              followUpBusy={false}
            />
          </ModeProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const [firstOpener, secondOpener] = screen.getAllByRole("button", {
      name: /Export as/i,
    }) as HTMLButtonElement[];

    const dialog = await keyboardOpenExport(user, firstOpener);
    await user.click(within(dialog).getByRole("button", { name: /Close export dialog/i }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    expect(document.activeElement).toBe(firstOpener);
    expect(document.activeElement).not.toBe(secondOpener);
  });

  // PASS-BASE (functional compatibility): keyboard-opened Export keeps
  // capability/download/error semantics — MD enabled, PDF disabled without
  // WeasyPrint, no spurious error, template picker hidden.
  it("keyboard-opened Export preserves capabilities and error semantics", async () => {
    const user = userEvent.setup();
    renderCard();
    const opener = getExportOpener();

    const dialog = await keyboardOpenExport(user, opener);
    expect(within(dialog).getByRole("button", { name: /Markdown/i })).toBeEnabled();
    expect(within(dialog).getByRole("button", { name: /PDF/i })).toBeDisabled();
    expect(within(dialog).queryByLabelText(/PDF template/i)).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  // Overlay pointer dismissal is a real-browser acceptance item (pending):
  // jsdom cannot deliver a trustworthy outside-pointer action for a modal
  // Radix Dialog, so no jsdom assertion here counts as overlay acceptance.
  // Close/Escape above remain the deterministic close evidence.
  // PENDING-REAL-BROWSER: open Export, pointer-click the rendered overlay,
  // assert the dialog closes and focus returns to the connected opener.

  // Guard: a disconnected opener must not take focus, and focus must not
  // jump to another card's control. Real path: detach the exact opener
  // mid-open (no mock focus, no manual refocus), then Close.
  it("disconnected opener takes no focus and steals none from another card", async () => {
    const user = userEvent.setup();
    renderCard(undefined, <button type="button">Unrelated</button>);
    const opener = getExportOpener();
    const unrelated = screen.getByRole("button", { name: "Unrelated" });

    const dialog = await keyboardOpenExport(user, opener);
    opener.remove();
    await user.click(within(dialog).getByRole("button", { name: /Close export dialog/i }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    expect(document.activeElement).not.toBe(opener);
    expect(document.activeElement).not.toBe(unrelated);
  });

  // Guard: a hidden opener must not take focus.
  it("hidden opener takes no focus on Close", async () => {
    const user = userEvent.setup();
    renderCard(undefined, <button type="button">Unrelated</button>);
    const opener = getExportOpener();
    const unrelated = screen.getByRole("button", { name: "Unrelated" });

    const dialog = await keyboardOpenExport(user, opener);
    opener.hidden = true;
    await user.click(within(dialog).getByRole("button", { name: /Close export dialog/i }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    expect(document.activeElement).not.toBe(opener);
    expect(document.activeElement).not.toBe(unrelated);
  });

  // Guard: a disabled opener must not take focus.
  it("disabled opener takes no focus on Close", async () => {
    const user = userEvent.setup();
    renderCard(undefined, <button type="button">Unrelated</button>);
    const opener = getExportOpener();
    const unrelated = screen.getByRole("button", { name: "Unrelated" });

    const dialog = await keyboardOpenExport(user, opener);
    opener.disabled = true;
    await user.click(within(dialog).getByRole("button", { name: /Close export dialog/i }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    expect(document.activeElement).not.toBe(opener);
    expect(document.activeElement).not.toBe(unrelated);
  });

  // Compatibility: a failed download still surfaces the honest error and
  // Close still returns focus to the connected opener.
  it("export download error still shown and Close focus remains correct", async () => {
    const user = userEvent.setup();
    renderCard();
    const opener = getExportOpener();

    const dialog = await keyboardOpenExport(user, opener);
    _fetchMock.mockRejectedValueOnce(new Error("Export failed boom"));
    await user.click(within(dialog).getByRole("button", { name: /Markdown/i }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Export failed boom");

    await user.click(within(dialog).getByRole("button", { name: /Close export dialog/i }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(document.activeElement).toBe(opener);
  });

  // Compatibility: with PDF capability on, PDF enables, the template picker
  // shows, and Close still returns focus to the connected opener.
  it("PDF capability on keeps template picker and opener return", async () => {
    const user = userEvent.setup();
    _caps.current = { md: true, pdf: true };
    renderCard();
    const opener = getExportOpener();

    const dialog = await keyboardOpenExport(user, opener);
    expect(within(dialog).getByRole("button", { name: /PDF/i })).toBeEnabled();
    expect(within(dialog).getByLabelText(/PDF template/i)).toBeInTheDocument();
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: /Close export dialog/i }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(document.activeElement).toBe(opener);
  });

  it.each(["hidden", "inert"])("opener under a %s ancestor cannot take focus on Close", async (attribute) => {
    const user = userEvent.setup();
    renderCard(undefined, <button type="button">Unrelated</button>);
    const opener = getExportOpener();
    const unrelated = screen.getByRole("button", { name: "Unrelated" });
    const dialog = await keyboardOpenExport(user, opener);
    const card = opener.closest("section")!;
    card.setAttribute(attribute, "");
    await user.click(within(dialog).getByRole("button", { name: /Close export dialog/i }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(document.activeElement).not.toBe(opener);
    expect(document.activeElement).not.toBe(unrelated);
  });

});

// Audio dialog and pre-export cancellation focus remain separate, unproven cases.
