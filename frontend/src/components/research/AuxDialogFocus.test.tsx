/**
 * AuxDialogFocus — regression tests for three real Firefox cancellation
 * failures (byte-identical candidate on main e8a9cc71d6831208c1cad14899fca2d727293896):
 *  F1 Export card -> IncludeFollowUpsModal -> Close leaves BODY (not the Export opener).
 *  F2 Audio card -> IncludeFollowUpsModal -> Close leaves BODY (not the Audio opener).
 *  F3 Audio selector Skip -> AudioModeDialog -> Close leaves BODY (not the Audio opener).
 * Expected: cancellation returns focus to the exact activated card opener
 * (connected/enabled/visible), with no audio generation or export.
 * Real rendered components + real userEvent + real Radix dialogs; no mocked
 * dialogs/focus/useRef/handlers, no scripted .focus() (clicks/keys focus
 * natively). jsdom is source-level only here, NOT real-browser overlay
 * acceptance. Escape checks are source-level and UNVERIFIED in a real browser.
 * Top-bar requestedAction has no DOM origin: opens/nonce preserved, but no
 * topbar-origin focus is ever asserted.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NeedMoreCard } from "./NeedMoreCard";
import { ModeProvider } from "@/shell/ModeProvider";
import type { MessageEvent, ReportEvent } from "@/types/agent";
import {
  fetchReportExportBlob,
  requestReportAudioBlocking,
  streamReportAudio,
} from "@/api/deepResearch";

// ── Mocks (API/network/capabilities/route boundaries only, as current tests) ──

const _caps = vi.hoisted(() => ({
  current: { md: true, pdf: false } as { md: boolean; pdf: boolean },
}));
vi.mock("@/hooks/useExportCapabilities", () => ({
  useExportCapabilities: () => _caps.current,
}));
vi.mock("@/hooks/useTemplates", () => ({
  useTemplates: () => [
    { id: "disco-light", name: "disco", mode: "light", label: "Disco", description: "d", accent: "#4077a3", bg: "#fcfcfa", default: true },
  ],
}));
vi.mock("@/api/deepResearch", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/deepResearch")>()),
  exportReportAsMarkdown: vi.fn(),
  exportReport: vi.fn().mockResolvedValue(true),
  serializeReportToMarkdown: vi.fn().mockReturnValue("# Report\n\nContent"),
  requestReportAudio: vi.fn(),
  fetchExistingReportAudio: vi.fn().mockResolvedValue([]),
  fetchReportExportBlob: vi.fn().mockResolvedValue(new Blob(["# Report"])),
  streamReportAudio: vi.fn().mockResolvedValue(false),
  requestReportAudioBlocking: vi.fn().mockResolvedValue({ mp3_url: "/c/c1/report/audio/a.mp3" }),
}));
const _fetchMock = vi.fn().mockResolvedValue({
  ok: true,
  json: vi.fn().mockResolvedValue({ mp3_url: "/c/c1/report/audio/a.mp3" }),
});
vi.stubGlobal("fetch", _fetchMock);
const _navMock = vi.hoisted(() => vi.fn());
vi.mock("react-router-dom", async (orig) => ({
  ...(await orig<typeof import("react-router-dom")>()),
  useNavigate: () => _navMock,
}));
vi.mock("@/api/agent", async (orig) => ({
  ...(await orig<typeof import("@/api/agent")>()),
  createBuildConversation: vi.fn().mockResolvedValue("conv_new_deck"),
}));

// ── Fixtures (minimal arbitrary report/cid/seqs; two genuine answered pairs) ──

const STUB_REPORT = {
  id: "evt_aux_focus",
  kind: "report",
  source: "agent",
  seq: 7,
  query: "Aux focus test report",
  summary: "Summary.",
  sections: [],
  passages: [],
  all_hits: [],
  unsupported_count: 0,
  bounded_by: null,
  depth_tier: "standard_deep",
} as unknown as ReportEvent;
const STUB_CID = "cid_aux_focus_1";

function msg(seq: number, role: "user" | "assistant", content: string): MessageEvent {
  return { kind: "message", seq, source: role === "user" ? "user" : "agent", message: { role, content } } as MessageEvent;
}
const FOLLOW_UPS: MessageEvent[] = [
  msg(10, "user", "What about the sparrow?"),
  msg(11, "assistant", "The sparrow flies at 4 m/s."),
  msg(12, "user", "Any penguins?"),
  msg(13, "assistant", "Penguins swim at 6 m/s."),
];

// ── Helpers ──

function renderCard(overrides?: Partial<React.ComponentProps<typeof NeedMoreCard>>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ModeProvider>
          <NeedMoreCard report={STUB_REPORT} cid={STUB_CID} onFollowUp={vi.fn()} followUpBusy={false} {...overrides} />
        </ModeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function renderTwoCards() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ModeProvider>
          <NeedMoreCard report={STUB_REPORT} cid="cid_aux_a" onFollowUp={vi.fn()} followUpBusy={false} followUps={FOLLOW_UPS} />
          <NeedMoreCard report={STUB_REPORT} cid="cid_aux_b" onFollowUp={vi.fn()} followUpBusy={false} followUps={FOLLOW_UPS} />
        </ModeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function expectNoPlayer() {
  expect(screen.queryByRole("button", { name: /Export MP3/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Play audio overview/i })).not.toBeInTheDocument();
}

function expectNoExportCall() {
  expect(fetchReportExportBlob).not.toHaveBeenCalled();
}

function expectNoAudioGeneration() {
  expect(streamReportAudio).not.toHaveBeenCalled();
  expect(requestReportAudioBlocking).not.toHaveBeenCalled();
}

// ── Tests ──

describe("AuxDialogFocus", () => {
  let anchorClick: ReturnType<typeof vi.spyOn>;
  const createObjectURL = vi.fn(() => "blob:aux-dialog-download");
  const revokeObjectURL = vi.fn();
  let objectUrlDescriptor: PropertyDescriptor | undefined;
  let revokeUrlDescriptor: PropertyDescriptor | undefined;
  beforeEach(() => {
    vi.clearAllMocks();
    anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    // jsdom omits browser blob URLs. Keep the real URL constructor and download
    // implementation; stub only this browser boundary and restore descriptors.
    objectUrlDescriptor = Object.getOwnPropertyDescriptor(URL, "createObjectURL");
    revokeUrlDescriptor = Object.getOwnPropertyDescriptor(URL, "revokeObjectURL");
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: revokeObjectURL });
    _caps.current = { md: true, pdf: false };
  });
  afterEach(() => {
    anchorClick.mockRestore();
    if (objectUrlDescriptor) Object.defineProperty(URL, "createObjectURL", objectUrlDescriptor);
    else Reflect.deleteProperty(URL, "createObjectURL");
    if (revokeUrlDescriptor) Object.defineProperty(URL, "revokeObjectURL", revokeUrlDescriptor);
    else Reflect.deleteProperty(URL, "revokeObjectURL");
  });

  // ── Baseline-failing real-browser cancellations (fail on main: BODY, not opener) ──

  it("F1: export selector Close returns focus to the Export opener", async () => {
    const user = userEvent.setup();
    renderCard({ followUps: FOLLOW_UPS });
    const exportBtn = screen.getByRole("button", { name: /Export as/i });
    await user.click(exportBtn);
    const selector = await screen.findByRole("dialog", { name: "Include follow-ups?" });
    await user.click(within(selector).getByRole("button", { name: "Close dialog" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expectNoPlayer();
    expectNoExportCall();
    expectNoAudioGeneration();
    expect(anchorClick).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(exportBtn);
  });

  it("F2: audio selector Close returns focus to the Audio Overview opener", async () => {
    const user = userEvent.setup();
    renderCard({ followUps: FOLLOW_UPS });
    const audioBtn = screen.getByRole("button", { name: /^Audio Overview$/i });
    await user.click(audioBtn);
    const selector = await screen.findByRole("dialog", { name: "Include follow-ups?" });
    await user.click(within(selector).getByRole("button", { name: "Close dialog" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expectNoPlayer();
    expectNoExportCall();
    expectNoAudioGeneration();
    expect(document.activeElement).toBe(audioBtn);
  });

  it("F3: audio selector Skip -> mode Close returns focus to the activated card opener (two-card isolation)", async () => {
    const user = userEvent.setup();
    renderTwoCards();
    const audioBtns = screen.getAllByRole("button", { name: /^Audio Overview$/i });
    expect(audioBtns).toHaveLength(2);
    const [firstBtn, secondBtn] = audioBtns;
    await user.click(secondBtn);
    const selector = await screen.findByRole("dialog", { name: "Include follow-ups?" });
    await user.click(within(selector).getByRole("button", { name: /^Skip$/i }));
    // Handoff: old selector must not steal focus; new mode dialog contains it after Radix cleanup.
    const modeDialog = await screen.findByRole("dialog", { name: "Generate audio overview" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Include follow-ups?" })).not.toBeInTheDocument());
    await waitFor(() => expect(modeDialog.contains(document.activeElement)).toBe(true));
    expectNoAudioGeneration();
    await user.click(within(modeDialog).getByRole("button", { name: "Close audio mode dialog" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expectNoPlayer();
    expectNoAudioGeneration();
    // Exact opener, never a fallback to the unactivated card's button.
    expect(document.activeElement).toBe(secondBtn);
    expect(document.activeElement).not.toBe(firstBtn);
  });

  // ── Compatibility: export confirm/skip handoff + prior ExportModal passes ──

  it("C1: export selector Skip opens the real Export dialog with autofocus, no export yet", async () => {
    const user = userEvent.setup();
    renderCard({ followUps: FOLLOW_UPS });
    await user.click(screen.getByRole("button", { name: /Export as/i }));
    const selector = await screen.findByRole("dialog", { name: "Include follow-ups?" });
    await user.click(within(selector).getByRole("button", { name: /^Skip$/i }));
    const exportDialog = await screen.findByRole("dialog", { name: "Export report" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Include follow-ups?" })).not.toBeInTheDocument());
    await waitFor(() => expect(exportDialog.contains(document.activeElement)).toBe(true));
    expectNoExportCall();
    expect(anchorClick).not.toHaveBeenCalled();
  });

  it("C2: export selector Confirm threads the real selection into the Markdown payload (deselect seq 10 -> [12])", async () => {
    const user = userEvent.setup();
    renderCard({ followUps: FOLLOW_UPS });
    await user.click(screen.getByRole("button", { name: /Export as/i }));
    const selector = await screen.findByRole("dialog", { name: "Include follow-ups?" });
    const sparrowBtn = within(selector)
      .getAllByRole("button")
      .find((b) => b.textContent?.includes("What about the sparrow"))!;
    await user.click(sparrowBtn); // deselect seq 10: meaningful selection action
    await user.click(within(selector).getByRole("button", { name: /Export with 1 follow-up/i }));
    const exportDialog = await screen.findByRole("dialog", { name: "Export report" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Include follow-ups?" })).not.toBeInTheDocument());
    // Intentional export: drive the real Markdown boundary and assert the payload.
    await user.click(within(exportDialog).getByRole("button", { name: /Markdown/i }));
    await waitFor(() => expect(fetchReportExportBlob).toHaveBeenCalledTimes(1));
    const bodyArg = vi.mocked(fetchReportExportBlob).mock.calls[0][2] as string;
    expect(JSON.parse(bodyArg).follow_up_seqs).toEqual([12]);
    await waitFor(() => expect(anchorClick).toHaveBeenCalledTimes(1));
    expect(createObjectURL).toHaveBeenCalledWith(expect.any(Blob));
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:aux-dialog-download");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expectNoAudioGeneration();
  });

  it("C3: export via selector Confirm then Export Close keeps prior focus-return pass", async () => {
    const user = userEvent.setup();
    renderCard({ followUps: FOLLOW_UPS });
    const exportBtn = screen.getByRole("button", { name: /Export as/i });
    await user.click(exportBtn);
    const selector = await screen.findByRole("dialog", { name: "Include follow-ups?" });
    await user.click(within(selector).getByRole("button", { name: /Export with 2 follow-ups/i }));
    const exportDialog = await screen.findByRole("dialog", { name: "Export report" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Include follow-ups?" })).not.toBeInTheDocument());
    await waitFor(() => expect(exportDialog.contains(document.activeElement)).toBe(true));
    await user.click(within(exportDialog).getByRole("button", { name: /Close export dialog/i }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(document.activeElement).toBe(exportBtn);
  });

  // ── Compatibility: audio confirm/skip -> mode containment, no generation ──

  it("C4: audio selector Skip opens the mode dialog with focus contained, no audio generated", async () => {
    const user = userEvent.setup();
    renderCard({ followUps: FOLLOW_UPS });
    await user.click(screen.getByRole("button", { name: /^Audio Overview$/i }));
    const selector = await screen.findByRole("dialog", { name: "Include follow-ups?" });
    await user.click(within(selector).getByRole("button", { name: /^Skip$/i }));
    const modeDialog = await screen.findByRole("dialog", { name: "Generate audio overview" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Include follow-ups?" })).not.toBeInTheDocument());
    await waitFor(() => expect(modeDialog.contains(document.activeElement)).toBe(true));
    expectNoPlayer();
    expectNoAudioGeneration();
    expectNoExportCall();
  });

  it("C5: audio selector Confirm opens the mode dialog with focus contained, no audio generated", async () => {
    const user = userEvent.setup();
    renderCard({ followUps: FOLLOW_UPS });
    await user.click(screen.getByRole("button", { name: /^Audio Overview$/i }));
    const selector = await screen.findByRole("dialog", { name: "Include follow-ups?" });
    await user.click(within(selector).getByRole("button", { name: /Generate audio with 2 follow-ups/i }));
    const modeDialog = await screen.findByRole("dialog", { name: "Generate audio overview" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Include follow-ups?" })).not.toBeInTheDocument());
    await waitFor(() => expect(modeDialog.contains(document.activeElement)).toBe(true));
    expectNoPlayer();
    expectNoAudioGeneration();
    expectNoExportCall();
  });

  it("C6: direct Audio mode (no follow-ups) opens and Close cancels without generating", async () => {
    const user = userEvent.setup();
    renderCard();
    await user.click(screen.getByRole("button", { name: /^Audio Overview$/i }));
    const modeDialog = await screen.findByRole("dialog", { name: "Generate audio overview" });
    await user.click(within(modeDialog).getByRole("button", { name: "Close audio mode dialog" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expectNoPlayer();
    expectNoAudioGeneration();
    expectNoExportCall();
    expect(screen.getByRole("button", { name: /^Audio Overview$/i })).toBeInTheDocument();
  });

  // ── Source-level Escape checks (UNVERIFIED in a real browser until later) ──

  it("C7 [source-level; unverified browser]: Escape closes the follow-up selector", async () => {
    const user = userEvent.setup();
    renderCard({ followUps: FOLLOW_UPS });
    await user.click(screen.getByRole("button", { name: /Export as/i }));
    await screen.findByRole("dialog", { name: "Include follow-ups?" });
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expectNoPlayer();
    expectNoExportCall();
    expectNoAudioGeneration();
  });

  it("C8 [source-level; unverified browser]: Escape closes the audio mode dialog", async () => {
    const user = userEvent.setup();
    renderCard();
    await user.click(screen.getByRole("button", { name: /^Audio Overview$/i }));
    await screen.findByRole("dialog", { name: "Generate audio overview" });
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expectNoPlayer();
    expectNoAudioGeneration();
    expectNoExportCall();
  });

  // ── Compatibility: programmatic top-bar opens/nonce (no origin guarantee) ──

  it("C9: top-bar requestedAction audio opens the selector; no topbar-origin focus asserted", async () => {
    renderCard({ followUps: FOLLOW_UPS, requestedAction: { name: "audio", nonce: 7 } });
    const selector = await screen.findByRole("dialog", { name: "Include follow-ups?" });
    // Audio branch: description names the audio action, not export.
    expect(within(selector).getByRole("button", { name: "Generate audio with 2 follow-ups" })).toBeInTheDocument();
    expectNoPlayer();
    expectNoAudioGeneration();
    expectNoExportCall();
  });

  // ── New controls (<=6): exact Audio Close/Escape, selector Escape, stale-origin, invalid target ──

  it("N1: direct Audio mode Close returns focus to the exact activated Audio button", async () => {
    const user = userEvent.setup();
    renderCard();
    const audioBtn = screen.getByRole("button", { name: /^Audio Overview$/i });
    await user.click(audioBtn);
    const modeDialog = await screen.findByRole("dialog", { name: "Generate audio overview" });
    await user.click(within(modeDialog).getByRole("button", { name: "Close audio mode dialog" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expectNoPlayer();
    expectNoAudioGeneration();
    expectNoExportCall();
    expect(document.activeElement).toBe(audioBtn);
  });

  it("N2 [source-level; unverified browser]: direct Audio mode Escape returns to the exact Audio button", async () => {
    const user = userEvent.setup();
    renderCard();
    const audioBtn = screen.getByRole("button", { name: /^Audio Overview$/i });
    await user.click(audioBtn);
    await screen.findByRole("dialog", { name: "Generate audio overview" });
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expectNoAudioGeneration();
    expect(document.activeElement).toBe(audioBtn);
  });

  it("N3 [source-level; unverified browser]: export selector Escape returns to the exact Export button", async () => {
    const user = userEvent.setup();
    renderCard({ followUps: FOLLOW_UPS });
    const exportBtn = screen.getByRole("button", { name: /Export as/i });
    await user.click(exportBtn);
    await screen.findByRole("dialog", { name: "Include follow-ups?" });
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expectNoExportCall();
    expectNoAudioGeneration();
    expect(document.activeElement).toBe(exportBtn);
  });

  it("N4: stale Audio origin is cleared — programmatic open/cancel never restores the former card opener", async () => {
    const user = userEvent.setup();
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const tree = (action: { name: "audio"; nonce: number } | null) => (
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ModeProvider>
            <NeedMoreCard report={STUB_REPORT} cid={STUB_CID} onFollowUp={vi.fn()} followUpBusy={false} followUps={FOLLOW_UPS} requestedAction={action} />
          </ModeProvider>
        </MemoryRouter>
      </QueryClientProvider>
    );
    const view = render(tree(null));
    const audioBtn = screen.getByRole("button", { name: /^Audio Overview$/i });
    await user.click(audioBtn);
    const first = await screen.findByRole("dialog", { name: "Include follow-ups?" });
    await user.click(within(first).getByRole("button", { name: "Close dialog" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(document.activeElement).toBe(audioBtn);
    vi.clearAllMocks();
    view.rerender(tree({ name: "audio", nonce: 8 }));
    const second = await screen.findByRole("dialog", { name: "Include follow-ups?" });
    expect(second).toBeInTheDocument(); // opens/nonce preserved for no-event call
    await user.click(within(second).getByRole("button", { name: "Close dialog" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(document.activeElement).not.toBe(audioBtn);
    expect(document.activeElement).toBe(document.body);
    expectNoAudioGeneration();
    expectNoExportCall();
  });

  it("N5: disconnected Audio opener keeps default behavior without falling back to another target", async () => {
    const user = userEvent.setup();
    renderTwoCards();
    const audioBtns = screen.getAllByRole("button", { name: /^Audio Overview$/i });
    const secondBtn = audioBtns[1];
    await user.click(secondBtn);
    const selector = await screen.findByRole("dialog", { name: "Include follow-ups?" });
    secondBtn.remove(); // invalid target: guard must keep Radix default, never another card
    await user.click(within(selector).getByRole("button", { name: "Close dialog" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(document.activeElement).toBe(document.body);
    expectNoAudioGeneration();
    expectNoExportCall();
  });
});
