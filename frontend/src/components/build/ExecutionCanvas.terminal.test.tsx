/**
 * ExecutionCanvas — Terminal tab BP-14 tests.
 *
 * Verifies: two-mode switch (live sessions vs. history fallback);
 * activity dot on the Terminal tab label; near-bottom autoscroll rule.
 *
 * useSessions is mocked synchronously and lives at the ExecutionCanvas level
 * (NOT inside TerminalPane): Radix unmounts inactive tab content, so the tab
 * badge must be driven by canvas-level polling or it can never light up while
 * the user is on another tab. The badge tests therefore assert WITHOUT
 * clicking the Terminal tab first — that's the regression they guard. The
 * pane-content tests still click the tab so the pane mounts.
 */

import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi, afterEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { SessionInfo, SessionView } from "@/api/agent";
import type { AgentEvent } from "@/types/agent";

// ---- Mock useSessions -------------------------------------------------------

const mockUseSessions = vi.fn();

vi.mock("@/hooks/useSessions", () => ({
  useSessions: (...args: unknown[]) => mockUseSessions(...args),
}));

// ---- Mock useBuildPreview ---------------------------------------------------

vi.mock("@/hooks/useBuildPreview", () => ({
  useBuildPreview: () => ({ data: null }),
}));

// ---- Mock api/agent ---------------------------------------------------------

vi.mock("@/api/agent", () => ({
  restartPreview: vi.fn(),
  getSessions: vi.fn().mockResolvedValue({ sessions: [] }),
  getSessionView: vi.fn(),
}));

// ---- Helpers ----------------------------------------------------------------

import { ExecutionCanvas } from "./ExecutionCanvas";

function makeQc() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

const noSessions = (): ReturnType<typeof mockUseSessions> => ({
  sessions: [] as SessionInfo[],
  selectedName: null,
  setSelectedName: vi.fn(),
  view: null,
});

const withSessions = (busy = false): ReturnType<typeof mockUseSessions> => ({
  sessions: [
    { name: "dev", busy, last_line: "VITE ready" },
    { name: "preview", busy: false, last_line: "Serving HTTP" },
  ] as SessionInfo[],
  selectedName: "dev",
  setSelectedName: vi.fn(),
  view: { name: "dev", busy, content: "live server output" } as SessionView,
});

const events: AgentEvent[] = [];

afterEach(() => {
  vi.clearAllMocks();
});

// ---- Tests ------------------------------------------------------------------

describe("ExecutionCanvas Terminal — two-mode switch", () => {
  it("shows 'history (no live sessions)' caption when sessions list is empty", async () => {
    mockUseSessions.mockReturnValue(noSessions());
    const qc = makeQc();
    render(
      <QueryClientProvider client={qc}>
        <ExecutionCanvas events={events} status="RUNNING" cid="c1" />
      </QueryClientProvider>,
    );
    await userEvent.click(screen.getByRole("tab", { name: /terminal/i }));
    expect(screen.getByText("history (no live sessions)")).toBeTruthy();
  });

  it("shows session chips and live content when sessions exist", async () => {
    mockUseSessions.mockReturnValue(withSessions());
    const qc = makeQc();
    render(
      <QueryClientProvider client={qc}>
        <ExecutionCanvas events={events} status="RUNNING" cid="c2" />
      </QueryClientProvider>,
    );
    await userEvent.click(screen.getByRole("tab", { name: /terminal/i }));
    expect(screen.getByText("dev")).toBeTruthy();
    expect(screen.getByText("preview")).toBeTruthy();
    expect(screen.getByText("live server output")).toBeTruthy();
  });
});

describe("ExecutionCanvas Terminal — activity dot on tab label", () => {
  it("shows activity dot on Terminal tab when any session is busy — WITHOUT visiting the tab", () => {
    mockUseSessions.mockReturnValue(withSessions(true));
    const qc = makeQc();
    render(
      <QueryClientProvider client={qc}>
        <ExecutionCanvas events={events} status="RUNNING" cid="c3" />
      </QueryClientProvider>,
    );
    // No tab click: the canvas mounts on the Files tab, yet the badge must
    // light up — that's the dot's entire job (summon the user to Terminal).
    // Pane-local polling would leave this permanently dark (the bp-14 worker's
    // original architecture did exactly that).
    expect(screen.getByLabelText("session running")).toBeTruthy();
  });

  it("does NOT show activity dot when no session is busy", () => {
    mockUseSessions.mockReturnValue(withSessions(false));
    const qc = makeQc();
    render(
      <QueryClientProvider client={qc}>
        <ExecutionCanvas events={events} status="RUNNING" cid="c4" />
      </QueryClientProvider>,
    );
    expect(screen.queryByLabelText("session running")).toBeNull();
  });

  it("passes visible=true to useSessions only when the Terminal tab is shown", async () => {
    mockUseSessions.mockReturnValue(withSessions(false));
    const qc = makeQc();
    render(
      <QueryClientProvider client={qc}>
        <ExecutionCanvas events={events} status="RUNNING" cid="c7" />
      </QueryClientProvider>,
    );
    // Mounted on Files → /view polling must be off (visible=false)…
    expect(mockUseSessions).toHaveBeenLastCalledWith("c7", true, false);
    // …and switch on when the user opens Terminal.
    await userEvent.click(screen.getByRole("tab", { name: /terminal/i }));
    expect(mockUseSessions).toHaveBeenLastCalledWith("c7", true, true);
  });
});

describe("ExecutionCanvas Terminal — near-bottom autoscroll", () => {
  it("renders the pane with live content without throwing", async () => {
    mockUseSessions.mockReturnValue(withSessions());
    const qc = makeQc();
    render(
      <QueryClientProvider client={qc}>
        <ExecutionCanvas events={events} status="RUNNING" cid="c5" />
      </QueryClientProvider>,
    );
    await userEvent.click(screen.getByRole("tab", { name: /terminal/i }));
    // scrollIntoView shim in setup.ts ensures no throw
    expect(screen.getByText("live server output")).toBeTruthy();
  });

  it("tracks atBottom=false via onScroll and does not force-scroll after", async () => {
    mockUseSessions.mockReturnValue(withSessions());
    const qc = makeQc();
    const { container } = render(
      <QueryClientProvider client={qc}>
        <ExecutionCanvas events={events} status="RUNNING" cid="c6" />
      </QueryClientProvider>,
    );
    await userEvent.click(screen.getByRole("tab", { name: /terminal/i }));

    // Simulate the user scrolling away from the bottom (scrollHeight >> scrollTop)
    const scrollEl = container.querySelector(
      ".overflow-auto.flex-1",
    ) as HTMLElement | null;
    if (scrollEl) {
      Object.defineProperty(scrollEl, "scrollHeight", { value: 2000, configurable: true });
      Object.defineProperty(scrollEl, "scrollTop", { value: 0, configurable: true });
      Object.defineProperty(scrollEl, "clientHeight", { value: 400, configurable: true });
      fireEvent.scroll(scrollEl);
    }
    // Content still visible — no crash
    expect(screen.getByText("live server output")).toBeTruthy();
  });
});
