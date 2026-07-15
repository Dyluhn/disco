/**
 * Live-browser (noVNC) REDESIGN tests. There is NO manual "Live" button anymore:
 *  - disabled in Settings        → no button, screenshots, no live anything.
 *  - enabled + NOT streamable     → no button, screenshots, NO error banner.
 *  - enabled + streamable         → AUTO-starts (live-url called with NO click), iframe
 *                                    shows, green-blink "Live" badge appears on iframe load.
 *  - session ends (live-ready→F)  → live-stop called, reverts to screenshots.
 *  - auto-start FAILS             → visible, bounded screenshot fallback.
 *  - owner-cid teardown on switch still fires.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { AgentCanvas } from "./AgentCanvas";
import { agentGet, agentSend } from "@/api/client";
import type { AgentEvent } from "@/types/agent";

// Mock the live-browser config hook (per-test overridden).
vi.mock("@/hooks/useModels", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/hooks/useModels")>();
  return {
    ...actual,
    useLiveBrowserConfig: vi.fn(() => ({ data: { enabled: false }, isLoading: false })),
    useUpdateLiveBrowserConfig: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  };
});

// agentGet is path-aware for /browser/live-ready; agentSend handles /browser/live-url
// auto-start and teardown. Per-test we override live-url via the `live` controller.
const OK_URL = { ready: true, novnc_path: "/vnc.html?autoconnect=1&view_only=1", port: 6080 };
const live = {
  ready: { ready: true, reason: "ready" } as Record<string, unknown>,
  urlCalls: 0,
  // Per-call live-url behaviour: given the 1-based call index, return the resolved body
  // or throw to reject. Default: always succeed. A rejection's message carries the JSON
  // {reason} body (mirrors ApiError) so the component can tell doomed from transient.
  urlFor: (n: number): Record<string, unknown> => {
    void n;
    return OK_URL;
  },
};

function rejectReason(reason: string): never {
  throw new Error(JSON.stringify({ reason, message: reason }));
}

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return {
    ...actual,
    agentGet: vi.fn((path: string) => {
      if (path.includes("/browser/live-ready")) return Promise.resolve(live.ready);
      return Promise.resolve({});
    }),
    agentSend: vi.fn((method: string, path: string) => {
      if (method === "POST" && path.includes("/browser/live-url")) {
        live.urlCalls += 1;
        try {
          return Promise.resolve(live.urlFor(live.urlCalls));
        } catch (e) {
          return Promise.reject(e);
        }
      }
      return Promise.resolve({ ok: true });
    }),
    agentHttpBase: vi.fn(() => "http://localhost:8000"),
    previewBootstrapUrl: vi.fn((cid: string, port: number) =>
      Promise.resolve({
        url: `http://${cid.replace(/^conv_/, "").slice(0, 8)}-${port}.localhost:8000/__disco/preview-auth`,
        intent: `host-intent-${cid}-${port}`,
      }),
    ),
  };
});

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

async function enable(enabled: boolean) {
  const { useLiveBrowserConfig } = await import("@/hooks/useModels");
  (useLiveBrowserConfig as ReturnType<typeof vi.fn>).mockReturnValue({
    data: { enabled },
    isLoading: false,
  });
}

function finishSignedNavigation(iframe: HTMLIFrameElement) {
  window.dispatchEvent(
    new MessageEvent("message", {
      data: "disco-preview-bootstrap-ready",
      source: iframe.contentWindow,
    }),
  );
  fireEvent.load(iframe);
}

const baseProps = { events: [] as AgentEvent[], status: "RUNNING" as const, cid: "conv_aabbccdd11223344" };

describe("AgentCanvas — live browser (auto-stream redesign)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    live.ready = { ready: true, reason: "ready" };
    live.urlCalls = 0;
    live.urlFor = () => OK_URL;
  });

  it("(a) disabled → NO Live control, screenshots empty-state, no live anything", async () => {
    await enable(false);
    wrap(<AgentCanvas {...baseProps} />);
    // never a Live button/badge, and the screenshot empty-state copy shows.
    expect(screen.queryByRole("button", { name: /live/i })).not.toBeInTheDocument();
    expect(screen.queryByTestId("live-badge")).not.toBeInTheDocument();
    expect(screen.getByText(/frame-by-frame reel, not a live video/i)).toBeInTheDocument();
    // never probes live-ready when disabled.
    expect(agentGet).not.toHaveBeenCalledWith(expect.stringContaining("/browser/live-ready"));
  });

  it("(b) enabled but NOT streamable → no button, screenshots, NO error banner", async () => {
    await enable(true);
    live.ready = { ready: false, reason: "unsupported_backend" };
    wrap(<AgentCanvas {...baseProps} />);
    // it polls live-ready…
    await waitFor(() =>
      expect(agentGet).toHaveBeenCalledWith(expect.stringContaining("/browser/live-ready")),
    );
    // …but NEVER auto-starts (no live-url), shows no iframe, no badge, NO scary banner.
    expect(agentGet).not.toHaveBeenCalledWith(expect.stringContaining("/browser/live-url"));
    expect(screen.queryByTestId("novnc-iframe")).not.toBeInTheDocument();
    expect(screen.queryByTestId("live-badge")).not.toBeInTheDocument();
    expect(screen.queryByText(/live view unavailable/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /live/i })).not.toBeInTheDocument();
  });

  it("(c) enabled + streamable → AUTO-starts (no click) and shows the green-blink Live badge on iframe load", async () => {
    await enable(true);
    wrap(<AgentCanvas {...baseProps} />);
    // auto-start: live-url is called WITHOUT any user click.
    await waitFor(() =>
      expect(agentSend).toHaveBeenCalledWith(
        "POST",
        expect.stringContaining("/browser/live-url"),
      ),
    );
    const iframe = (await screen.findByTestId("novnc-iframe")) as HTMLIFrameElement;
    expect(iframe).not.toHaveAttribute("src");
    expect(iframe.getAttribute("sandbox")).toContain("allow-same-origin");
    const { previewBootstrapUrl } = await import("@/api/client");
    expect(previewBootstrapUrl).toHaveBeenCalledWith(
      baseProps.cid,
      6080,
      "/vnc.html?autoconnect=1&view_only=1",
    );
    // badge only AFTER the iframe genuinely loads (honest "actually streaming").
    expect(screen.queryByTestId("live-badge")).not.toBeInTheDocument();
    finishSignedNavigation(iframe);
    const badge = await screen.findByTestId("live-badge");
    expect(badge).toHaveAttribute("data-streaming", "true");
    expect(badge).toHaveTextContent(/live/i);
  });

  it("(d) session ends (live-ready flips false) → live-stop called, reverts to screenshots", async () => {
    await enable(true);
    wrap(<AgentCanvas {...baseProps} />);
    const iframe = (await screen.findByTestId("novnc-iframe")) as HTMLIFrameElement;
    await act(async () => {}); // install the signed-navigation message gate
    finishSignedNavigation(iframe);
    await screen.findByTestId("live-badge");

    (agentSend as ReturnType<typeof vi.fn>).mockClear();
    // The browser session ends → the probe now reports not-ready. After 2 consecutive
    // misses (~8s) the view must tear down. Polling is every 4s; advance fake timers.
    live.ready = { ready: false, reason: "no_daemon" };
    await waitFor(
      () =>
        expect(agentSend).toHaveBeenCalledWith(
          "POST",
          expect.stringContaining("conv_aabbccdd11223344/browser/live-stop"),
        ),
      { timeout: 16000 },
    );
    await waitFor(() => expect(screen.queryByTestId("novnc-iframe")).not.toBeInTheDocument());
    expect(screen.queryByTestId("live-badge")).not.toBeInTheDocument();
  }, 20000);

  it("(e) auto-start FAILS → visible screenshot fallback while bounded retry continues", async () => {
    await enable(true);
    live.urlFor = () => rejectReason("no_upstream"); // live-ready streamable, but the start blows up
    wrap(<AgentCanvas {...baseProps} />);
    await waitFor(() =>
      expect(agentSend).toHaveBeenCalledWith(
        "POST",
        expect.stringContaining("/browser/live-url"),
      ),
    );
    // No iframe or badge, but the fallback is explicit instead of silently hiding
    // that the requested live stream failed to start.
    await waitFor(() =>
      expect(screen.getByText(/frame-by-frame reel, not a live video/i)).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("novnc-iframe")).not.toBeInTheDocument();
    expect(screen.queryByTestId("live-badge")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("live-fallback-notice")).toHaveTextContent(
        /temporarily unavailable; retrying/i,
      ),
    );
  });

  it("(g) TRANSIENT live-url failure (no_upstream) does NOT latch — retries and succeeds once the browser is up", async () => {
    await enable(true);
    // First start attempt fails with the normal transient no_upstream (live_start ok but
    // the port isn't exposed yet); every later attempt succeeds.
    live.urlFor = (n) => (n === 1 ? rejectReason("no_upstream") : OK_URL);
    wrap(<AgentCanvas {...baseProps} />);
    // The first attempt fails (no iframe yet)…
    await waitFor(() => expect(live.urlCalls).toBeGreaterThanOrEqual(1));
    // …but it is NOT permanently latched: the next readiness tick (~4s) retries and the
    // live view appears. This is the codex-P1 regression — a transient blip must not
    // disable auto-start forever.
    await screen.findByTestId("novnc-iframe", undefined, {
      timeout: 16000,
    });
    const { previewBootstrapUrl } = await import("@/api/client");
    expect(previewBootstrapUrl).toHaveBeenCalledWith(
      baseProps.cid,
      6080,
      "/vnc.html?autoconnect=1&view_only=1",
    );
    expect(live.urlCalls).toBeGreaterThanOrEqual(2); // it genuinely retried
  }, 20000);

  it("(h) DOOMED live-url failure (unsupported_backend) latches — no retry-storm (called once, no tight loop)", async () => {
    await enable(true);
    live.urlFor = () => rejectReason("unsupported_backend"); // a hard capability error
    wrap(<AgentCanvas {...baseProps} />);
    await waitFor(() => expect(live.urlCalls).toBe(1));
    // Wait across several poll ticks; a doomed result must NOT keep hammering live-url.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 9000));
    });
    expect(live.urlCalls).toBe(1); // still exactly one — latched, no storm
    expect(screen.queryByTestId("novnc-iframe")).not.toBeInTheDocument();
    expect(screen.queryByTestId("live-badge")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("live-fallback-notice")).toHaveTextContent(
        /unavailable after bounded startup attempts/i,
      ),
    );
  }, 15000);

  it("(i) persistent transient failure stops after three attempts and stays visible", async () => {
    await enable(true);
    live.urlFor = () => rejectReason("no_upstream");
    wrap(<AgentCanvas {...baseProps} />);

    await waitFor(
      () => {
        expect(live.urlCalls).toBe(3);
        expect(screen.getByTestId("live-fallback-notice")).toHaveTextContent(
          /unavailable after bounded startup attempts/i,
        );
      },
      { timeout: 12000 },
    );
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 4500));
    });
    expect(live.urlCalls).toBe(3);
  }, 18000);

  it("(f) tears down the OWNING conversation's stack when switching conversations while live", async () => {
    await enable(true);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const ui = (cid: string) => (
      <QueryClientProvider client={qc}>
        <AgentCanvas events={[]} status="RUNNING" cid={cid} />
      </QueryClientProvider>
    );
    const { rerender } = render(ui("conv_aabbccdd11223344"));
    await screen.findByTestId("novnc-iframe"); // auto-started
    (agentSend as ReturnType<typeof vi.fn>).mockClear();

    // Switch to a DIFFERENT conversation while the live view is open.
    rerender(ui("conv_bbbbbbbb99887766"));
    await waitFor(() =>
      expect(agentSend).toHaveBeenCalledWith(
        "POST",
        expect.stringContaining("conv_aabbccdd11223344/browser/live-stop"),
      ),
    );
    // never the wrong (newly-current) conversation.
    expect(agentSend).not.toHaveBeenCalledWith(
      "POST",
      expect.stringContaining("conv_bbbbbbbb99887766/browser/live-stop"),
    );
  });
});
