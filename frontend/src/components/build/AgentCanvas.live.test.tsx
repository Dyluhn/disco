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

// Mock agentGet for the live-url call
vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return {
    ...actual,
    agentGet: vi.fn().mockResolvedValue({
      url: "http://aabbccdd-6080.localhost:8000",
      novnc_path: "/vnc.html?autoconnect=1&view_only=1",
      port: 6080,
    }),
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
    await userEvent.click(liveBtn);
    await waitFor(() => {
      const iframe = screen.getByTestId("novnc-iframe") as HTMLIFrameElement;
      expect(iframe).toBeInTheDocument();
      expect(iframe.src).toContain("view_only=1");
      // The iframe sandbox must NOT include allow-same-origin (security)
      expect(iframe.getAttribute("sandbox")).not.toContain("allow-same-origin");
    });
  });
});
