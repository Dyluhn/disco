/**
 * UI-15 — the "Preparing Preview update…" strip must cover the whole top of the
 * preview stage.
 *
 * The strip used to be inset from both sides (`inset-x-body`). The frame it sits
 * on top of starts at the stage's own left edge, so when that frame held a bare
 * proxy error page ("preview origin expired", "preview capability required" —
 * every one of those bodies begins with the word "preview"), a few pixels of its
 * first line showed past the strip's left edge and the user saw a stray "p"
 * glyph floating beside the message. A full-bleed strip covers the line it is
 * speaking for.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PreviewPane } from "@/components/build/canvas/PreviewPane";
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

const RUNNING_PREVIEW: PreviewInfo = {
  available: true,
  status: "running",
  generation: "strip-generation",
  launch_kind: "static",
  reload_strategy: "reload",
  port: 43120,
};

beforeEach(() => {
  useBuildPreviewMock.mockReturnValue({ data: RUNNING_PREVIEW });
  useWorkspaceVersionsMock.mockReturnValue({
    versions: [],
    loading: false,
    refetch: vi.fn(),
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("PreviewPane — the preparing/updating status strip", () => {
  it("spans the full stage width, so no sliver of the frame shows beside it", async () => {
    render(withClient(<PreviewPane status="RUNNING" cid="conv_strip" events={[APP]} />));

    const strip = await screen.findByTestId("preview-status-overlay");
    expect(strip).toHaveTextContent("Preparing Preview update…");
    // Full-bleed: no horizontal inset can leave a gutter for the frame's own
    // first line to peek through (that gutter is the stray "p" of UI-15).
    expect(strip.className).toContain("inset-x-0");
    expect(strip.className).not.toContain("inset-x-body");
  });

  it("still renders the frame underneath — the strip is an overlay, not a replacement", async () => {
    const { container } = render(
      withClient(<PreviewPane status="RUNNING" cid="conv_strip2" events={[APP]} />),
    );
    await waitFor(() => expect(container.querySelector("iframe")).not.toBeNull());
    expect(screen.getByTestId("preview-status-overlay")).toBeInTheDocument();
  });
});
