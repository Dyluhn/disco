import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PreviewPane } from "@/components/build/canvas/PreviewPane";
import type { WorkspaceVersion } from "@/api/agent";
import type { AgentEvent, PreviewInfo } from "@/types/agent";

const {
  canonicalPreviewBootstrapUrlMock,
  refetchVersionsMock,
  restoreWorkspaceVersionMock,
  showToastMock,
  useBuildPreviewMock,
  useWorkspaceVersionsMock,
} = vi.hoisted(() => ({
  canonicalPreviewBootstrapUrlMock: vi.fn((cid: string, target: string) =>
    Promise.resolve({
      url: `http://127.77.0.4:8000/__disco/preview-auth/${cid}`,
      intent: `intent:${target}`,
    }),
  ),
  refetchVersionsMock: vi.fn(),
  restoreWorkspaceVersionMock: vi.fn(),
  showToastMock: vi.fn(),
  useBuildPreviewMock: vi.fn<
    (...args: unknown[]) => { data: PreviewInfo | null }
  >(() => ({ data: null })),
  useWorkspaceVersionsMock: vi.fn(),
}));

vi.mock("@/hooks/useBuildPreview", () => ({
  useBuildPreview: (...args: unknown[]) => useBuildPreviewMock(...args),
}));

vi.mock("@/hooks/useWorkspaceVersions", () => ({
  useWorkspaceVersions: (...args: unknown[]) =>
    useWorkspaceVersionsMock(...args),
}));

vi.mock("@/components/toastApi", () => ({
  useToast: () => ({ show: showToastMock }),
}));

vi.mock("@/api/preview", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/preview")>();
  return {
    ...actual,
    previewBootstrapUrl: canonicalPreviewBootstrapUrlMock,
  };
});

vi.mock("@/api/agent", () => ({
  restartPreview: vi.fn(),
  restoreWorkspaceVersion: (...args: unknown[]) =>
    restoreWorkspaceVersionMock(...args),
}));

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
    arguments: { path: "index.html", content: "<h1>Preview</h1>" },
  },
} as AgentEvent;

const APP: AgentEvent = {
  id: "app",
  kind: "deliverable",
  title: "App",
  path: "index.html",
  artifact_kind: "app",
} as AgentEvent;

const VERSIONS: WorkspaceVersion[] = [
  {
    seq: 3,
    ts: "2026-07-03T12:00:00Z",
    label: null,
    trigger: "restore",
    file_count: 4,
    total_bytes: 400,
    tree_digest: "digest-3",
    pinned: false,
  },
  {
    seq: 2,
    ts: "2026-07-03T11:00:00Z",
    label: "finished app",
    trigger: "finish",
    file_count: 3,
    total_bytes: 300,
    tree_digest: "digest-2",
    pinned: false,
  },
  {
    seq: 1,
    ts: "2026-07-03T10:00:00Z",
    label: null,
    trigger: "turn",
    file_count: 2,
    total_bytes: 200,
    tree_digest: "digest-1",
    pinned: false,
  },
];

function renderPane() {
  return render(
    withClient(
      <PreviewPane
        status="FINISHED"
        cid="conv_versions"
        events={[HTML, APP]}
      />,
    ),
  );
}

function versionLabel(seq: number, text: string): HTMLElement {
  const node = screen
    .getAllByText((_content, element) =>
      Boolean(
        element?.textContent?.startsWith(`v${seq}`) &&
          element.textContent.includes(text),
      ),
    )
    .find((element) => element.tagName.toLowerCase() === "span");
  if (!node) throw new Error(`missing version label v${seq}`);
  return node;
}

async function completePreviewLoad(): Promise<void> {
  const frame = screen.getByTitle("Preview") as HTMLIFrameElement;
  await act(async () => {
    window.dispatchEvent(
      new MessageEvent("message", {
        source: frame.contentWindow,
        data: "disco-preview-bootstrap-ready",
      }),
    );
    fireEvent.load(frame);
  });
}

beforeEach(() => {
  refetchVersionsMock.mockResolvedValue({});
  restoreWorkspaceVersionMock.mockReset();
  showToastMock.mockReset();
  useBuildPreviewMock.mockReturnValue({
    data: {
      available: false,
      status: "stopped",
      generation: "sealed-generation",
      reason: "sealed snapshot",
    },
  });
  useWorkspaceVersionsMock.mockReturnValue({
    versions: VERSIONS,
    loading: false,
    refetch: refetchVersionsMock,
  });
  canonicalPreviewBootstrapUrlMock.mockImplementation(
    (cid: string, target: string) =>
      Promise.resolve({
        url: `http://127.77.0.4:8000/__disco/preview-auth/${cid}`,
        intent: `intent:${target}`,
      }),
  );
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("PreviewPane version history", () => {
  it("hides version history when no immutable versions exist", async () => {
    useWorkspaceVersionsMock.mockReturnValue({
      versions: [],
      loading: false,
      refetch: refetchVersionsMock,
    });
    renderPane();
    await screen.findByTitle("Preview");
    expect(
      screen.queryByRole("button", { name: /version history/i }),
    ).not.toBeInTheDocument();
  });

  it("lists Current and exact immutable versions", async () => {
    const user = userEvent.setup();
    renderPane();
    await screen.findByTitle("Preview");
    await user.click(screen.getByRole("button", { name: /version history/i }));

    expect(screen.getAllByText("Current").length).toBeGreaterThan(0);
    expect(versionLabel(2, "finished app")).toBeInTheDocument();
    expect(versionLabel(1, "turn")).toBeInTheDocument();
    expect(screen.getByText("finish")).toHaveClass("text-supported");
    expect(screen.getByText("restore")).toHaveClass("text-warn");
  });

  it("selects the exact version through the same canonical Preview", async () => {
    const user = userEvent.setup();
    renderPane();
    await screen.findByTitle("Preview");
    await user.click(screen.getByRole("button", { name: /version history/i }));
    await user.click(versionLabel(2, "finished app"));

    await waitFor(() =>
      expect(canonicalPreviewBootstrapUrlMock).toHaveBeenCalledWith(
        "conv_versions",
        "/?_disco_refresh=1",
        2,
      ),
    );
    await completePreviewLoad();
    expect(
      screen.getByText("Viewing immutable v2 (read-only)"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /back to current/i }),
    ).toBeInTheDocument();
    expect(screen.getByTitle("Preview")).toBeInTheDocument();
    expect(screen.queryByTitle("Historical preview")).not.toBeInTheDocument();
  });

  it("mints a fresh one-use launch when the same version is revisited", async () => {
    const user = userEvent.setup();
    renderPane();
    await screen.findByTitle("Preview");

    await user.click(screen.getByRole("button", { name: /version history/i }));
    await user.click(versionLabel(2, "finished app"));
    await waitFor(() =>
      expect(canonicalPreviewBootstrapUrlMock).toHaveBeenCalledTimes(2),
    );
    await completePreviewLoad();
    await user.click(screen.getByRole("button", { name: /back to current/i }));
    await waitFor(() =>
      expect(canonicalPreviewBootstrapUrlMock).toHaveBeenCalledTimes(3),
    );
    await user.click(screen.getByRole("button", { name: /version history/i }));
    await user.click(versionLabel(2, "finished app"));
    await waitFor(() =>
      expect(canonicalPreviewBootstrapUrlMock).toHaveBeenCalledTimes(4),
    );
  });

  it("retains the current frame and reports an unavailable version capability", async () => {
    const user = userEvent.setup();
    canonicalPreviewBootstrapUrlMock
      .mockResolvedValueOnce({
        url: "http://127.77.0.4:8000/__disco/preview-auth/current",
        intent: "current",
      })
      .mockRejectedValueOnce(new Error("capability unavailable"));
    renderPane();
    const currentFrame = await screen.findByTitle("Preview");

    await user.click(screen.getByRole("button", { name: /version history/i }));
    await user.click(versionLabel(2, "finished app"));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Preview update unavailable: capability unavailable",
    );
    expect(screen.getByTitle("Preview")).toBe(currentFrame);
    expect(screen.queryByText(/Viewing immutable v2/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^roll back$/i })).not.toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(
      "/conversations/conv_versions/preview-app/",
    );
  });

  it("confirms rollback, refetches, and returns to current Preview", async () => {
    const user = userEvent.setup();
    restoreWorkspaceVersionMock.mockResolvedValue({
      restored: 2,
      new_version: 4,
      tree_digest: "digest-4",
    });
    renderPane();
    await screen.findByTitle("Preview");
    await user.click(screen.getByRole("button", { name: /version history/i }));
    await user.click(versionLabel(2, "finished app"));
    await waitFor(() =>
      expect(canonicalPreviewBootstrapUrlMock).toHaveBeenCalledWith(
        "conv_versions",
        "/?_disco_refresh=1",
        2,
      ),
    );
    await completePreviewLoad();
    await user.click(screen.getByRole("button", { name: /^roll back$/i }));
    await user.click(
      await screen.findByRole("button", { name: /^roll back$/i }),
    );

    await waitFor(() =>
      expect(restoreWorkspaceVersionMock).toHaveBeenCalledWith(
        "conv_versions",
        2,
      ),
    );
    await waitFor(() =>
      expect(canonicalPreviewBootstrapUrlMock).toHaveBeenCalledTimes(3),
    );
    await completePreviewLoad();
    expect(refetchVersionsMock).toHaveBeenCalled();
    expect(
      screen.queryByText("Viewing immutable v2 (read-only)"),
    ).not.toBeInTheDocument();
    expect(screen.getByText("Rolled back to v2; saved as v4.")).toHaveAttribute(
      "role",
      "status",
    );
    expect(showToastMock).toHaveBeenCalledWith({
      title: "Rolled back to v2",
      body: "Saved as v4.",
    });
  });

  it("keeps the selected version visible when rollback is blocked by a run", async () => {
    const user = userEvent.setup();
    // ApiError is pulled in via importActual (not a static import) so this
    // spec file carries no `@/api/client` import of its own — PreviewPane no
    // longer talks to that module and components/ must not either.
    const { ApiError } = await vi.importActual<typeof import("@/api/client")>(
      "@/api/client",
    );
    restoreWorkspaceVersionMock.mockRejectedValue(
      new ApiError(
        JSON.stringify({ detail: { reason: "conversation_running" } }),
        409,
      ),
    );
    renderPane();
    await screen.findByTitle("Preview");
    await user.click(screen.getByRole("button", { name: /version history/i }));
    await user.click(versionLabel(2, "finished app"));
    await waitFor(() =>
      expect(canonicalPreviewBootstrapUrlMock).toHaveBeenCalledWith(
        "conv_versions",
        "/?_disco_refresh=1",
        2,
      ),
    );
    await completePreviewLoad();
    await user.click(screen.getByRole("button", { name: /^roll back$/i }));
    await user.click(
      await screen.findByRole("button", { name: /^roll back$/i }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "build is running — pause or wait",
    );
    expect(
      screen.getByText("Viewing immutable v2 (read-only)"),
    ).toBeInTheDocument();
  });
});
