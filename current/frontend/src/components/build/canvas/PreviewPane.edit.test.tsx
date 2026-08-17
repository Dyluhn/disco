/** Scoped editing stays on the canonical running Preview frame. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PreviewPane } from "@/components/build/canvas/PreviewPane";
import type { SelectionEnvelope } from "@/lib/selectionBridge";
import type { AgentEvent, PreviewInfo } from "@/types/agent";

const {
  armSelectionMock,
  canonicalPreviewBootstrapUrlMock,
  disarmSelectionMock,
  resetSelectionMock,
  selectionRef,
  useBuildPreviewMock,
  useWorkspaceVersionsMock,
} = vi.hoisted(() => ({
  armSelectionMock: vi.fn(),
  canonicalPreviewBootstrapUrlMock: vi.fn(() =>
    Promise.resolve({
      url: "http://127.77.0.5:8000/__disco/preview-auth",
      intent: "canonical-edit",
    }),
  ),
  disarmSelectionMock: vi.fn(),
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
    arm: armSelectionMock,
    disarm: disarmSelectionMock,
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
  nonce: "selection-nonce",
  type: "disco:selection",
  selection_ref: {
    kind: "source",
    oid: "index.html:1",
    file: "index.html",
    line: 1,
  },
  human_label: "h1 — “Done”",
  rect: { x: 5, y: 5, width: 80, height: 30 },
};

beforeEach(() => {
  selectionRef.current = null;
  useBuildPreviewMock.mockReturnValue({
    data: {
      available: true,
      status: "running",
      generation: "edit-generation",
      reload_strategy: "reload",
    },
  });
  useWorkspaceVersionsMock.mockReturnValue({
    versions: [],
    loading: false,
    refetch: vi.fn(),
  });
  canonicalPreviewBootstrapUrlMock.mockResolvedValue({
    url: "http://127.77.0.5:8000/__disco/preview-auth",
    intent: "canonical-edit",
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("PreviewPane canonical edit mode", () => {
  it("offers Edit only after the canonical Preview exists", async () => {
    render(
      withClient(
        <PreviewPane
          status="RUNNING"
          cid="conv_edit"
          events={[HTML]}
          onSelectionEdit={vi.fn()}
        />,
      ),
    );

    expect(await screen.findByTitle("Preview")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /click-to-edit mode/i }),
    ).toBeInTheDocument();
    expect(screen.queryByTitle("Editable preview")).not.toBeInTheDocument();
  });

  it("arms editing without swapping away from the canonical frame", async () => {
    const user = userEvent.setup();
    render(
      withClient(
        <PreviewPane
          status="RUNNING"
          cid="conv_edit"
          events={[HTML]}
          onSelectionEdit={vi.fn()}
        />,
      ),
    );
    const frame = await screen.findByTitle("Preview");
    await user.click(
      screen.getByRole("button", { name: /click-to-edit mode/i }),
    );

    expect(armSelectionMock).toHaveBeenCalledOnce();
    expect(screen.getByTitle("Preview")).toBe(frame);
    expect(
      screen.getByText(/click the real running page/i),
    ).toBeInTheDocument();
    expect(screen.queryByTitle("Editable preview")).not.toBeInTheDocument();
    expect(canonicalPreviewBootstrapUrlMock).toHaveBeenCalledTimes(1);
  });

  it("submits the selected typed ref from the canonical frame", async () => {
    const user = userEvent.setup();
    const onSelectionEdit = vi.fn();
    selectionRef.current = SELECTION;
    render(
      withClient(
        <PreviewPane
          status="RUNNING"
          cid="conv_edit"
          events={[HTML]}
          onSelectionEdit={onSelectionEdit}
        />,
      ),
    );
    await screen.findByTitle("Preview");
    await user.click(
      screen.getByRole("button", { name: /click-to-edit mode/i }),
    );
    await user.type(
      screen.getByPlaceholderText(/describe the change/i),
      "Make it larger",
    );
    await user.click(screen.getByRole("button", { name: /apply this edit/i }));

    expect(onSelectionEdit).toHaveBeenCalledWith(
      SELECTION.selection_ref,
      "Make it larger",
      SELECTION.human_label,
    );
    expect(resetSelectionMock).toHaveBeenCalled();
  });

  it("hides Edit without a host wire and for an untrusted artifact viewer", async () => {
    const { rerender } = render(
      withClient(
        <PreviewPane status="RUNNING" cid="conv_edit" events={[HTML]} />,
      ),
    );
    await screen.findByTitle("Preview");
    expect(
      screen.queryByRole("button", { name: /click-to-edit mode/i }),
    ).not.toBeInTheDocument();

    rerender(
      withClient(
        <PreviewPane
          status="FINISHED"
          cid={null}
          events={[HTML]}
          onSelectionEdit={vi.fn()}
          untrusted
        />,
      ),
    );
    expect(screen.getByTitle("Static artifact viewer")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /click-to-edit mode/i }),
    ).not.toBeInTheDocument();
  });
});
