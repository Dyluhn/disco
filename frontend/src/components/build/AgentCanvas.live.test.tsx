/**
 * P4 tests: Live toggle renders only when enable_live_browser=true;
 * iframe with view_only=1 appears when enabled + clicked.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { AgentCanvas } from "./AgentCanvas";

// Mock the live-browser config hook
vi.mock("@/hooks/useModels", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/hooks/useModels")>();
  return {
    ...actual,
    useLiveBrowserConfig: vi.fn(() => ({ data: { enabled: false }, isLoading: false })),
    useUpdateLiveBrowserConfig: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  };
});

// Mock agentGet for the live-url call. The route now returns ONLY {ready, novnc_path,
// port} — NO raw url — and the client builds the {cid8}-6080.localhost proxy URL via
// the real previewHostUrl (kept from importOriginal) using agentHttpBase() as the base.
vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return {
    ...actual,
    agentGet: vi.fn().mockResolvedValue({
      ready: true,
      novnc_path: "/vnc.html?autoconnect=1&view_only=1",
      port: 6080,
    }),
    agentSend: vi.fn().mockResolvedValue({ ok: true }), // live-stop teardown call
    agentHttpBase: vi.fn(() => "http://localhost:8000"),
  };
});

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const baseProps = { events: [], status: "IDLE" as const, cid: "conv_aabbccdd11223344" };

describe("AgentCanvas — Live browser toggle", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("does NOT render the Live toggle when live_browser.enabled=false", async () => {
    const { useLiveBrowserConfig } = await import("@/hooks/useModels");
    (useLiveBrowserConfig as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { enabled: false },
      isLoading: false,
    });
    wrap(<AgentCanvas {...baseProps} />);
    // No Live button should appear
    expect(screen.queryByRole("button", { name: /live/i })).not.toBeInTheDocument();
  });

  it("renders the Live toggle when live_browser.enabled=true", async () => {
    const { useLiveBrowserConfig } = await import("@/hooks/useModels");
    (useLiveBrowserConfig as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { enabled: true },
      isLoading: false,
    });
    wrap(<AgentCanvas {...baseProps} />);
    // Switch to browser tab first (it may already be active since no screenshots)
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /live/i })).toBeInTheDocument()
    );
  });

  it("opens noVNC iframe with view_only=1 when Live is clicked", async () => {
    const { useLiveBrowserConfig } = await import("@/hooks/useModels");
    (useLiveBrowserConfig as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { enabled: true },
      isLoading: false,
    });
    wrap(<AgentCanvas {...baseProps} />);
    const liveBtn = await screen.findByRole("button", { name: /live/i });
    // W-47: the button is gated on the side-effect-free /browser/live-ready poll
    // (mocked agentGet → {ready:true}); wait until it's enabled before clicking.
    await waitFor(() => expect(liveBtn).toBeEnabled());
    await userEvent.click(liveBtn);
    await waitFor(() => {
      const iframe = screen.getByTestId("novnc-iframe") as HTMLIFrameElement;
      expect(iframe).toBeInTheDocument();
      expect(iframe.src).toContain("view_only=1");
      // The proxy URL is the {cid8}-6080.localhost single-origin path, NOT a raw host:port.
      expect(iframe.src).toContain("aabbccdd-6080.localhost");
      // The iframe sandbox must NOT include allow-same-origin (security)
      expect(iframe.getAttribute("sandbox")).not.toContain("allow-same-origin");
    });
  });

  it("tears down the OWNING conversation's stack when switching conversations while live", async () => {
    // Regression for the re-review BLOCK: BrowserPane is not keyed by cid, so a
    // conversation switch must stop the conversation that OPENED the view, never the
    // now-current cid (else the old VNC stack leaks).
    const { useLiveBrowserConfig } = await import("@/hooks/useModels");
    (useLiveBrowserConfig as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { enabled: true },
      isLoading: false,
    });
    const { agentSend } = await import("@/api/client");
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const ui = (cid: string) => (
      <QueryClientProvider client={qc}>
        <AgentCanvas events={[]} status="IDLE" cid={cid} />
      </QueryClientProvider>
    );
    const { rerender } = render(ui("conv_aabbccdd11223344"));

    const liveBtn = await screen.findByRole("button", { name: /live/i });
    await waitFor(() => expect(liveBtn).toBeEnabled());
    await userEvent.click(liveBtn);
    await screen.findByTestId("novnc-iframe");
    (agentSend as ReturnType<typeof vi.fn>).mockClear();

    // Switch to a DIFFERENT conversation while the live view is open.
    rerender(ui("conv_bbbbbbbb99887766"));

    await waitFor(() => {
      // Teardown must target the ORIGINAL (owning) cid, not the new one.
      expect(agentSend).toHaveBeenCalledWith(
        "POST",
        expect.stringContaining("conv_aabbccdd11223344/browser/live-stop"),
      );
    });
    // And never the wrong (newly-current) conversation.
    expect(agentSend).not.toHaveBeenCalledWith(
      "POST",
      expect.stringContaining("conv_bbbbbbbb99887766/browser/live-stop"),
    );
  });

  it("W-47: keeps the Live button DISABLED until live-ready reports ready, with the reason as tooltip", async () => {
    const { useLiveBrowserConfig } = await import("@/hooks/useModels");
    (useLiveBrowserConfig as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { enabled: true },
      isLoading: false,
    });
    // The readiness probe says the agent hasn't opened a browser yet → not streamable.
    const { agentGet } = await import("@/api/client");
    (agentGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ready: false,
      reason: "no_daemon",
    });
    wrap(<AgentCanvas {...baseProps} />);
    const liveBtn = await screen.findByRole("button", { name: /live/i });
    // Button stays disabled and its tooltip explains why (the no_daemon reason text).
    await waitFor(() => expect(liveBtn).toBeDisabled());
    expect(liveBtn.getAttribute("title")).toMatch(/browser/i);
    // Restore the ready default so later runs/files don't inherit ready:false.
    (agentGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ready: true,
      novnc_path: "/vnc.html?autoconnect=1&view_only=1",
      port: 6080,
    });
  });
});
