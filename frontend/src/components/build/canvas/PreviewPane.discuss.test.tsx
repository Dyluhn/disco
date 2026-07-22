/** Discuss/inspect remains attached to the canonical Preview frame. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PreviewPane } from "@/components/build/canvas/PreviewPane";
import { formatSelectionContext } from "@/lib/resolvers/appResolver";
import type { SelectionEnvelope } from "@/lib/selectionBridge";
import type { AgentEvent, PreviewInfo } from "@/types/agent";

const {
  canonicalPreviewBootstrapUrlMock,
  disarmMock,
  resetSelectionMock,
  selectionRef,
  useBuildPreviewMock,
  useWorkspaceVersionsMock,
} = vi.hoisted(() => ({
  canonicalPreviewBootstrapUrlMock: vi.fn(() =>
    Promise.resolve({
      url: "http://127.77.0.6:8000/__disco/preview-auth",
      intent: "canonical-discuss",
    }),
  ),
  disarmMock: vi.fn(),
  resetSelectionMock: vi.fn(),
  selectionRef: { current: null as SelectionEnvelope | null },
  useBuildPreviewMock: vi.fn<
    (...args: unknown[]) => { data: PreviewInfo | null }
  >(() => ({ data: null })),
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
  useWorkspaceVersions: (...args: unknown[]) =>
    useWorkspaceVersionsMock(...args),
}));

vi.mock("@/hooks/useElementSelect", () => ({
  useElementSelect: () => ({
    armed: true,
    arm: vi.fn(),
    disarm: disarmMock,
    selection: selectionRef.current,
    walkUp: vi.fn(),
    resetSelection: resetSelectionMock,
  }),
}));

vi.mock("@/api/client", async () => {
  const actual =
    await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return {
    ...actual,
    canonicalPreviewBootstrapUrl: canonicalPreviewBootstrapUrlMock,
  };
});

function withClient(ui: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{ui}</QueryClientProvider>;
}

const HTML: AgentEvent = {
  id: "write-index",
  kind: "action",
  thought: "",
  tool_call: {
    tool_name: "file_write",
    arguments: { path: "index.html", content: "<h1>Done</h1>" },
  },
} as AgentEvent;

const SELECTION: SelectionEnvelope = {
  v: 1,
  channel: "disco-select",
  nonce: "nonce",
  type: "disco:selection",
  selection_ref: {
    kind: "source",
    oid: "index.html:1",
    file: "index.html",
    line: 1,
  },
  human_label: "h1 — “Done”",
  rect: { x: 5, y: 5, width: 40, height: 20 },
};

beforeEach(() => {
  selectionRef.current = SELECTION;
  useBuildPreviewMock.mockReturnValue({
    data: {
      available: true,
      status: "running",
      generation: "discuss-generation",
      reload_strategy: "reload",
    },
  });
  canonicalPreviewBootstrapUrlMock.mockResolvedValue({
    url: "http://127.77.0.6:8000/__disco/preview-auth",
    intent: "canonical-discuss",
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("PreviewPane canonical Discuss wiring", () => {
  it("forwards selection context without replacing the running frame", async () => {
    const user = userEvent.setup();
    const onSteer = vi.fn();
    render(
      withClient(
        <PreviewPane
          status="RUNNING"
          cid="conv_discuss"
          events={[HTML]}
          onSteer={onSteer}
        />,
      ),
    );
    const frame = await screen.findByTitle("Preview");
    await user.click(screen.getByRole("button", { name: /discuss/i }));

    expect(onSteer).toHaveBeenCalledWith(formatSelectionContext(SELECTION));
    expect(resetSelectionMock).toHaveBeenCalled();
    expect(disarmMock).toHaveBeenCalled();
    expect(screen.getByTitle("Preview")).toBe(frame);
  });

  it("does not render Discuss when steering is unavailable", async () => {
    render(
      withClient(
        <PreviewPane status="RUNNING" cid="conv_discuss" events={[HTML]} />,
      ),
    );
    await screen.findByTitle("Preview");
    expect(
      screen.queryByRole("button", { name: /discuss/i }),
    ).not.toBeInTheDocument();
  });
});
