/**
 * UI-44 — a preview whose origin answers the POST trampoline with an error page.
 *
 * The capability MINT succeeds, so nothing anywhere records a failure. But the
 * trampoline page never renders, so its `disco-preview-bootstrap-ready` message
 * never arrives, the frame's own `onLoad` stays gated behind it, and `frameReady`
 * never flips. The user was left staring at a raw nginx 403 / "preview origin
 * expired" body under a "Preparing Preview update…" strip that could never
 * resolve.
 *
 * The watchdog turns that silence into an honest, actionable state.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PreviewPane } from "@/components/build/canvas/PreviewPane";
import { PREVIEW_BOOTSTRAP_TIMEOUT_MS } from "@/components/build/canvas/previewPaneParts/usePreviewLaunch";
import type { AgentEvent, PreviewInfo } from "@/types/agent";

const {
  canonicalPreviewBootstrapUrlMock,
  useBuildPreviewMock,
  useWorkspaceVersionsMock,
} = vi.hoisted(() => ({
  canonicalPreviewBootstrapUrlMock: vi.fn((cid: string, target: string) =>
    Promise.resolve({
      url: "http://127.77.0.3:8000/__disco/preview-auth",
      intent: `${cid}:${target}`,
    }),
  ),
  useBuildPreviewMock: vi.fn<(...args: unknown[]) => { data: PreviewInfo | null }>(
    () => ({ data: null }),
  ),
  useWorkspaceVersionsMock: vi.fn<
    (...args: unknown[]) => {
      versions: never[];
      loading: boolean;
      refetch: ReturnType<typeof vi.fn>;
    }
  >(() => ({ versions: [], loading: false, refetch: vi.fn() })),
}));

vi.mock("@/hooks/useBuildPreview", () => ({
  useBuildPreview: (...args: unknown[]) => useBuildPreviewMock(...args),
}));

vi.mock("@/hooks/useWorkspaceVersions", () => ({
  useWorkspaceVersions: (...args: unknown[]) => useWorkspaceVersionsMock(...args),
}));

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return { ...actual, canonicalPreviewBootstrapUrl: canonicalPreviewBootstrapUrlMock };
});

function withClient(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{ui}</QueryClientProvider>;
}

const APP: AgentEvent = {
  id: "app",
  kind: "deliverable",
  title: "App",
  path: "index.html",
  artifact_kind: "app",
} as AgentEvent;

function previewInfo(generation: string, updateError?: string): PreviewInfo {
  return {
    available: true,
    status: "running",
    generation,
    launch_kind: "static",
    reload_strategy: "reload",
    port: 43120,
    ...(updateError ? { update_error: updateError } : {}),
  } as PreviewInfo;
}

/** Let the minted launch settle into the frame. */
async function settle() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

/** The trampoline's handshake, as the real bootstrap page posts it. */
function announceBootstrapReady(frame: HTMLIFrameElement) {
  act(() => {
    window.dispatchEvent(
      new MessageEvent("message", {
        data: "disco-preview-bootstrap-ready",
        source: frame.contentWindow,
      }),
    );
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  useBuildPreviewMock.mockReturnValue({ data: previewInfo("bootstrap-generation") });
  useWorkspaceVersionsMock.mockReturnValue({
    versions: [],
    loading: false,
    refetch: vi.fn(),
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("PreviewPane — the bootstrap handshake watchdog (UI-44)", () => {
  it("replaces the frame with an actionable Preview unavailable state when the handshake never arrives", async () => {
    const { container } = render(
      withClient(
        <PreviewPane
          status="RUNNING"
          cid="conv_boot"
          events={[APP]}
          downloadTitle="Recipe box"
        />,
      ),
    );
    await settle();
    // Before the watchdog expires the pane behaves exactly as it always did:
    // the frame is mounted and the strip says it is preparing an update.
    expect(container.querySelector("iframe")).not.toBeNull();
    expect(screen.getByTestId("preview-status-overlay")).toHaveTextContent(
      "Preparing Preview update…",
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(PREVIEW_BOOTSTRAP_TIMEOUT_MS + 1_000);
    });

    // The raw proxy error page is gone…
    expect(container.querySelector("iframe")).toBeNull();
    // …replaced by the plain-language failure state plus its reason code.
    const failure = screen.getByRole("alert");
    expect(failure).toHaveTextContent("Preview unavailable");
    expect(failure).toHaveTextContent(/The preview server never answered/);
    expect(failure).toHaveTextContent("Reason code: preview_bootstrap_timeout");
    // …and the endless progress message is gone with it.
    expect(screen.queryByText("Preparing Preview update…")).toBeNull();
    expect(screen.queryByTestId("preview-status-overlay")).toBeNull();

    // Both recovery actions are real, wired controls.
    expect(
      screen.getByRole("button", { name: "Refresh Preview" }),
    ).toHaveAttribute("data-disco-control", "build.preview-failure-refresh");
    expect(
      screen.getByRole("button", { name: "Download source" }),
    ).toHaveAttribute("data-disco-control", "build.preview-failure-download");
  });

  it("never arms the watchdog for an intentless launch, which has no handshake", async () => {
    canonicalPreviewBootstrapUrlMock.mockResolvedValueOnce({
      url: "http://localhost:43120/",
      intent: "",
    });
    const { container } = render(
      withClient(<PreviewPane status="RUNNING" cid="conv_plain" events={[APP]} />),
    );
    await settle();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PREVIEW_BOOTSTRAP_TIMEOUT_MS + 1_000);
    });

    expect(container.querySelector("iframe")).not.toBeNull();
    expect(screen.queryByText("Preview unavailable")).toBeNull();
    expect(screen.queryByText(/Reason code: preview_bootstrap_timeout/)).toBeNull();
  });

  it("keeps the frame when the trampoline does announce bootstrap-ready", async () => {
    const { container } = render(
      withClient(<PreviewPane status="RUNNING" cid="conv_ok" events={[APP]} />),
    );
    await settle();
    const frame = container.querySelector("iframe") as HTMLIFrameElement;
    announceBootstrapReady(frame);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(PREVIEW_BOOTSTRAP_TIMEOUT_MS * 3);
    });

    expect(container.querySelector("iframe")).not.toBeNull();
    expect(screen.queryByText("Preview unavailable")).toBeNull();
    expect(screen.queryByText(/Reason code: preview_bootstrap_timeout/)).toBeNull();
  });

  it("does not hide a healthy frame when a LATER update fails — that frame is the last good one", async () => {
    const { container, rerender } = render(
      withClient(<PreviewPane status="RUNNING" cid="conv_last_good" events={[APP]} />),
    );
    await settle();
    const frame = container.querySelector("iframe") as HTMLIFrameElement;
    announceBootstrapReady(frame);
    // The gated load now reaches the pane: this frame is genuinely healthy.
    act(() => {
      fireEvent.load(frame);
    });

    // A later generation reminting into a refusal is the "update failed" case.
    canonicalPreviewBootstrapUrlMock.mockRejectedValueOnce(new Error("preview_unavailable"));
    useBuildPreviewMock.mockReturnValue({
      data: previewInfo("bootstrap-generation-2", "preview rebuild failed"),
    });
    rerender(
      withClient(<PreviewPane status="RUNNING" cid="conv_last_good" events={[APP]} />),
    );
    await settle();

    expect(screen.getByText(/Preview is showing the last healthy frame/)).toBeInTheDocument();
    // The refused remint IS a visible failure — the strip says so…
    expect(screen.getByTestId("preview-status-overlay")).toHaveTextContent(
      "Preview update unavailable",
    );
    // …and yet the healthy frame stays on screen behind it. That is deliberate,
    // and `frameReady` is the signal that separates it from UI-44's dead frame.
    expect(container.querySelector("iframe")).not.toBeNull();
    expect(screen.queryByText("Preview unavailable")).toBeNull();
  });
});
