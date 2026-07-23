/** Canonical Preview refresh policy: static runtimes remint on writes, HMR does not. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PreviewPane } from "@/components/build/canvas/PreviewPane";
import type {
  AgentEvent,
  ConversationStatus,
  PreviewInfo,
} from "@/types/agent";

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

function fileWrite(path: string, content: string): AgentEvent {
  return {
    id: `write-${path}-${content.length}`,
    kind: "action",
    thought: "",
    tool_call: { tool_name: "file_write", arguments: { path, content } },
  } as AgentEvent;
}

const APP: AgentEvent = {
  id: "app",
  kind: "deliverable",
  title: "App",
  path: "index.html",
  artifact_kind: "app",
} as AgentEvent;

const STATIC: PreviewInfo = {
  available: true,
  status: "running",
  generation: "static-generation",
  launch_kind: "static",
  reload_strategy: "reload",
  port: 43120,
};

const HMR: PreviewInfo = {
  ...STATIC,
  generation: "hmr-generation",
  launch_kind: "framework",
  reload_strategy: "hmr",
};

function renderPane(status: ConversationStatus, events: AgentEvent[]) {
  return render(
    withClient(
      <PreviewPane
        status={status}
        cid="conv_reload"
        events={[...events, APP]}
      />,
    ),
  );
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  useBuildPreviewMock.mockReturnValue({ data: STATIC });
  useWorkspaceVersionsMock.mockReturnValue({
    versions: [],
    loading: false,
    refetch: vi.fn(),
  });
  canonicalPreviewBootstrapUrlMock.mockImplementation(
    (cid: string, target: string) =>
      Promise.resolve({
        url: "http://127.77.0.3:8000/__disco/preview-auth",
        intent: `${cid}:${target}`,
      }),
  );
});

afterEach(() => {
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe("PreviewPane — canonical refresh policy", () => {
  it("keeps the last healthy frame visible with a host-owned refresh warning", async () => {
    useBuildPreviewMock.mockReturnValue({
      data: { ...HMR, update_error: "dependency refresh failed" },
    });

    renderPane("RUNNING", []);

    await waitFor(() =>
      expect(
        screen.getByText(/showing the last healthy frame.*dependency refresh failed/i),
      ).toBeInTheDocument(),
    );
  });

  it("debounces a plain/static file rewrite into one canonical remint", async () => {
    const initial = [fileWrite("index.html", "<h1>one</h1>")];
    const { rerender } = renderPane("RUNNING", initial);
    expect(await screen.findByTitle("Preview")).toBeInTheDocument();
    expect(canonicalPreviewBootstrapUrlMock).toHaveBeenCalledWith(
      "conv_reload",
      "/?_disco_refresh=0",
    );

    rerender(
      withClient(
        <PreviewPane
          status="RUNNING"
          cid="conv_reload"
          events={[fileWrite("index.html", "<h1>two, changed</h1>"), APP]}
        />,
      ),
    );
    await act(async () => {
      vi.advanceTimersByTime(599);
      await Promise.resolve();
    });
    expect(canonicalPreviewBootstrapUrlMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(1);
      await Promise.resolve();
    });
    await waitFor(() =>
      expect(canonicalPreviewBootstrapUrlMock).toHaveBeenCalledWith(
        "conv_reload",
        "/?_disco_refresh=1",
      ),
    );
    expect(canonicalPreviewBootstrapUrlMock).toHaveBeenCalledTimes(2);
  });

  it("leaves a framework frame mounted so HMR owns the update", async () => {
    useBuildPreviewMock.mockReturnValue({ data: HMR });
    const initial = [fileWrite("index.html", "<div>one</div>")];
    const { rerender } = renderPane("RUNNING", initial);
    const frame = (await screen.findByTitle("Preview")) as HTMLIFrameElement;

    rerender(
      withClient(
        <PreviewPane
          status="RUNNING"
          cid="conv_reload"
          events={[fileWrite("index.html", "<div>two</div>"), APP]}
        />,
      ),
    );
    await act(async () => {
      vi.advanceTimersByTime(1_000);
      await Promise.resolve();
    });

    expect(canonicalPreviewBootstrapUrlMock).toHaveBeenCalledTimes(1);
    expect(screen.getByTitle("Preview")).toBe(frame);
  });

  it("preserves the frame-reported route when a static runtime reloads", async () => {
    const initial = [fileWrite("index.html", "<h1>one</h1>")];
    const { rerender } = renderPane("RUNNING", initial);
    const frame = (await screen.findByTitle("Preview")) as HTMLIFrameElement;

    act(() => {
      window.dispatchEvent(
        new MessageEvent("message", {
          origin: "http://127.77.0.3:8000",
          source: frame.contentWindow,
          data: {
            channel: "disco-preview",
            type: "location",
            path: "/nested/page?mode=edit#inspector",
          },
        }),
      );
    });
    rerender(
      withClient(
        <PreviewPane
          status="RUNNING"
          cid="conv_reload"
          events={[fileWrite("index.html", "<h1>two</h1>"), APP]}
        />,
      ),
    );
    await act(async () => {
      vi.advanceTimersByTime(600);
      await Promise.resolve();
    });

    await waitFor(() =>
      expect(canonicalPreviewBootstrapUrlMock).toHaveBeenCalledWith(
        "conv_reload",
        "/nested/page?mode=edit&_disco_refresh=1#inspector",
      ),
    );
  });

  it("does not event-refresh a finished immutable Preview", async () => {
    const initial = [fileWrite("index.html", "<h1>one</h1>")];
    const { rerender } = renderPane("FINISHED", initial);
    await screen.findByTitle("Preview");

    rerender(
      withClient(
        <PreviewPane
          status="FINISHED"
          cid="conv_reload"
          events={[fileWrite("index.html", "<h1>two</h1>"), APP]}
        />,
      ),
    );
    await act(async () => {
      vi.advanceTimersByTime(1_000);
      await Promise.resolve();
    });
    expect(canonicalPreviewBootstrapUrlMock).toHaveBeenCalledTimes(1);
  });
});
