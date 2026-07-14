import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PreviewPane } from "@/components/build/canvas/PreviewPane";
import { ApiError } from "@/api/client";
import type { WorkspaceVersion } from "@/api/agent";
import type { AgentEvent, PreviewInfo } from "@/types/agent";

const {
  refetchVersionsMock,
  restoreWorkspaceVersionMock,
  showToastMock,
  pathPreviewBootstrapUrlMock,
  previewBootstrapUrlMock,
  useBuildPreviewMock,
  useWorkspaceVersionsMock,
} = vi.hoisted(() => ({
  refetchVersionsMock: vi.fn(),
  restoreWorkspaceVersionMock: vi.fn(),
  showToastMock: vi.fn(),
  pathPreviewBootstrapUrlMock: vi.fn((cid: string, targetPath: string) =>
    Promise.resolve(
      `http://localhost:8000/__disco/path-preview-auth/${cid}?target=${encodeURIComponent(targetPath)}`,
    ),
  ),
  // Version-history tests never exercise a live dev-server capability. Keep that
  // unrelated async effect pending so no state commit can outlive a sync assertion.
  previewBootstrapUrlMock: vi.fn(() => new Promise<string | null>(() => {})),
  useBuildPreviewMock: vi.fn<[{ data: PreviewInfo | null }]>(() => ({ data: null })),
  useWorkspaceVersionsMock: vi.fn(),
}));

vi.mock("@/hooks/useBuildPreview", () => ({
  useBuildPreview: (...args: unknown[]) => useBuildPreviewMock(...args),
}));

vi.mock("@/hooks/useWorkspaceVersions", () => ({
  useWorkspaceVersions: (...args: unknown[]) => useWorkspaceVersionsMock(...args),
}));

vi.mock("@/components/toastApi", () => ({
  useToast: () => ({ show: showToastMock }),
}));

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return {
    ...actual,
    agentHttpBase: () => "http://agent.test:8000",
    previewHostUrl: () => "http://preview.test",
    pathPreviewBootstrapUrl: pathPreviewBootstrapUrlMock,
    previewBootstrapUrl: previewBootstrapUrlMock,
  };
});

vi.mock("@/api/agent", () => ({
  restartPreview: vi.fn(),
  restoreWorkspaceVersion: (...args: unknown[]) => restoreWorkspaceVersionMock(...args),
}));

function withClient(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>;
}

function fileWrite(path: string, content: string): AgentEvent {
  return {
    id: `w-${path}`,
    kind: "action",
    thought: "",
    tool_call: { tool_name: "file_write", arguments: { path, content } },
  } as AgentEvent;
}

const HTML = fileWrite("index.html", "<html><body><h1>Preview</h1></body></html>");

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
      <PreviewPane status="FINISHED" cid="conv_versions" events={[HTML]} />,
    ),
  );
}

function versionText(seq: number, text: string) {
  return (_content: string, node: Element | null) =>
    Boolean(node?.textContent?.startsWith(`v${seq}`) && node.textContent.includes(text));
}

function getVersionLabel(seq: number, text: string): HTMLElement {
  const el = screen
    .getAllByText(versionText(seq, text))
    .find((node) => node.tagName.toLowerCase() === "span");
  if (!el) throw new Error(`missing version label v${seq}`);
  return el as HTMLElement;
}

beforeEach(() => {
  refetchVersionsMock.mockResolvedValue({});
  restoreWorkspaceVersionMock.mockReset();
  showToastMock.mockReset();
  useBuildPreviewMock.mockReturnValue({ data: { available: false, reason: "offline" } });
  useWorkspaceVersionsMock.mockReturnValue({
    versions: VERSIONS,
    loading: false,
    refetch: refetchVersionsMock,
  });
  pathPreviewBootstrapUrlMock.mockImplementation((cid: string, targetPath: string) =>
    Promise.resolve(
      `http://localhost:8000/__disco/path-preview-auth/${cid}?target=${encodeURIComponent(targetPath)}`,
    ),
  );
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("PreviewPane version history picker", () => {
  it("hides the picker when no versions exist", () => {
    useWorkspaceVersionsMock.mockReturnValue({
      versions: [],
      loading: false,
      refetch: refetchVersionsMock,
    });

    renderPane();

    expect(screen.queryByRole("button", { name: /version history/i })).not.toBeInTheDocument();
  });

  it("renders Live plus version entries when versions exist", async () => {
    const user = userEvent.setup();
    renderPane();

    await user.click(screen.getByRole("button", { name: /version history/i }));

    expect(screen.getAllByText("Live").length).toBeGreaterThan(0);
    expect(getVersionLabel(2, "finished app")).toBeInTheDocument();
    expect(getVersionLabel(1, "turn")).toBeInTheDocument();
    expect(screen.getByText("finish")).toHaveClass("text-supported");
    expect(screen.getByText("restore")).toHaveClass("text-warn");
  });

  it("selecting a historical version switches the iframe and shows the read-only banner", async () => {
    const user = userEvent.setup();
    renderPane();

    await user.click(screen.getByRole("button", { name: /version history/i }));
    await user.click(getVersionLabel(2, "finished app"));

    expect(screen.getByText("viewing v2 (read-only)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /back to live/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /roll back to this version/i })).toBeInTheDocument();
    const frame = await screen.findByTitle("Historical preview");
    expect(frame).toHaveAttribute(
      "src",
      "http://localhost:8000/__disco/path-preview-auth/conv_versions?target=%2F%3Fversion%3D2%26r%3D0",
    );
    expect(frame.getAttribute("src")).not.toContain("agent.test");
    expect(pathPreviewBootstrapUrlMock).toHaveBeenCalledWith(
      "conv_versions",
      "/?version=2&r=0",
    );
  });

  it("fails closed instead of loading a historical version on the authenticated origin", async () => {
    const user = userEvent.setup();
    pathPreviewBootstrapUrlMock.mockRejectedValue(new Error("capability unavailable"));
    renderPane();

    await user.click(screen.getByRole("button", { name: /version history/i }));
    await user.click(getVersionLabel(2, "finished app"));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Historical preview unavailable: isolated preview access could not be established.",
    );
    expect(screen.queryByTitle("Historical preview")).not.toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(
      "/conversations/conv_versions/preview-app/?version=2",
    );
  });

  it("confirms rollback, refetches versions, returns to Live, and shows success", async () => {
    const user = userEvent.setup();
    restoreWorkspaceVersionMock.mockResolvedValue({
      restored: 2,
      new_version: 4,
      tree_digest: "digest-4",
    });
    renderPane();

    await user.click(screen.getByRole("button", { name: /version history/i }));
    await user.click(getVersionLabel(2, "finished app"));
    await user.click(screen.getByRole("button", { name: /roll back to this version/i }));

    expect(await screen.findByRole("dialog", { name: "Roll back to v2?" })).toBeInTheDocument();
    expect(
      screen.getByText(
        "Restores the workspace to this version. Nothing is deleted — the rollback is saved as a new version on top of history.",
      ),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^Roll back$/i }));

    await waitFor(() =>
      expect(restoreWorkspaceVersionMock).toHaveBeenCalledWith("conv_versions", 2),
    );
    await waitFor(() => expect(refetchVersionsMock).toHaveBeenCalled());
    expect(screen.queryByText("viewing v2 (read-only)")).not.toBeInTheDocument();
    expect(screen.getByText("Rolled back to v2; saved as v4.")).toHaveAttribute(
      "role",
      "status",
    );
    expect(showToastMock).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Rolled back to v2", body: "Saved as v4." }),
    );
  });

  it("shows the 409 running-build rollback error inline", async () => {
    const user = userEvent.setup();
    restoreWorkspaceVersionMock.mockRejectedValue(
      new ApiError(JSON.stringify({ detail: { reason: "conversation_running" } }), 409),
    );
    renderPane();

    await user.click(screen.getByRole("button", { name: /version history/i }));
    await user.click(getVersionLabel(2, "finished app"));
    await user.click(screen.getByRole("button", { name: /roll back to this version/i }));
    await user.click(await screen.findByRole("button", { name: /^Roll back$/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "build is running — pause or wait",
    );
    expect(screen.getByText("viewing v2 (read-only)")).toBeInTheDocument();
  });
});
