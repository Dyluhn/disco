/**
 * LiveBrowserSection — the noVNC toggle is gated on the sandbox backend. The live stack
 * (Xvfb/x11vnc/websockify) runs only on the gVisor backend (LIVE_VIEW_BACKENDS), so:
 *  - local/podman backend → toggle locked off + explanatory note; clicking "On" FLAGS
 *    an error and never saves.
 *  - gVisor backend       → toggle enableable; clicking "On" saves enabled=true.
 *  - backend switch flips availability.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { LiveBrowserSection } from "./LiveBrowserSection";

const mutate = vi.fn();

vi.mock("@/hooks/useModels", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/hooks/useModels")>();
  return {
    ...actual,
    useLiveBrowserConfig: vi.fn(() => ({ data: { enabled: false }, isLoading: false })),
    useUpdateLiveBrowserConfig: vi.fn(() => ({ mutate, isPending: false, error: null })),
    useSandboxConfig: vi.fn(() => ({ data: { backend: "local" } })),
  };
});

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

async function setBackend(backend: string) {
  const { useSandboxConfig } = await import("@/hooks/useModels");
  (useSandboxConfig as ReturnType<typeof vi.fn>).mockReturnValue({ data: { backend } });
}

describe("LiveBrowserSection — backend-gated noVNC toggle", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("(a) local backend → toggle locked off with explanatory note, screenshots only", async () => {
    await setBackend("local");
    wrap(<LiveBrowserSection />);
    // explanatory note naming the backend.
    expect(screen.getByText(/requires a containerized sandbox/i)).toBeInTheDocument();
    const onBtn = screen.getByRole("button", { name: /^On/i });
    expect(onBtn).toHaveAttribute("data-locked", "true");
    expect(onBtn).toHaveAttribute("aria-disabled", "true");
  });

  it("(b) local backend → clicking On FLAGS the error and never saves", async () => {
    await setBackend("local");
    wrap(<LiveBrowserSection />);
    const onBtn = screen.getByRole("button", { name: /^On/i });
    await userEvent.click(onBtn);
    // a visible, flagged error — not a silent no-op.
    expect(await screen.findByRole("alert")).toHaveTextContent(/can't be turned on/i);
    // and the save mutation is NEVER called (no persisting enabled=true on local).
    expect(mutate).not.toHaveBeenCalled();
  });

  it("(c) gVisor backend → toggle enableable; clicking On saves enabled=true", async () => {
    await setBackend("gvisor");
    wrap(<LiveBrowserSection />);
    expect(screen.queryByText(/requires a containerized sandbox/i)).not.toBeInTheDocument();
    const onBtn = screen.getByRole("button", { name: /^On/i });
    expect(onBtn).not.toHaveAttribute("data-locked");
    await userEvent.click(onBtn);
    expect(mutate).toHaveBeenCalledWith({ enabled: true });
  });

  it("(d) switching backend gvisor→local flips availability (locks the toggle)", async () => {
    await setBackend("gvisor");
    const { rerender } = wrap(<LiveBrowserSection />);
    expect(screen.getByRole("button", { name: /^On/i })).not.toHaveAttribute("data-locked");

    await setBackend("local");
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    rerender(
      <QueryClientProvider client={qc}>
        <LiveBrowserSection />
      </QueryClientProvider>,
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^On/i })).toHaveAttribute("data-locked", "true"),
    );
  });
});
