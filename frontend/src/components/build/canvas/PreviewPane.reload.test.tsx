/**
 * W-42 — PreviewPane auto-refresh on file rewrite.
 *
 * The live-server iframe's src carries the reloadKey as `?r=N`. When the agent
 * rewrites a file (the event stream changes the derived files signature), a
 * debounced effect bumps reloadKey so the iframe reloads with fresh content —
 * but only while RUNNING (a finished preview isn't fought), and not when the
 * files are unchanged.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PreviewPane } from "@/components/build/canvas/PreviewPane";
import type { AgentEvent, ConversationStatus, PreviewInfo } from "@/types/agent";

const { useBuildPreviewMock } = vi.hoisted(() => ({
  useBuildPreviewMock: vi.fn<[{ data: PreviewInfo | null }]>(() => ({ data: null })),
}));

vi.mock("@/hooks/useBuildPreview", () => ({
  useBuildPreview: (...args: unknown[]) => useBuildPreviewMock(...args),
}));

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return {
    ...actual,
    agentHttpBase: () => "http://agent.test:8000",
    previewHostUrl: () => "http://preview.test",
    previewBootstrapUrl: (_cid: string, _port: number, targetPath = "/") =>
      Promise.resolve(`http://preview.test${targetPath}`),
  };
});

const LIVE: PreviewInfo = {
  available: true,
  owner: { pid: 1, cmdline: "vite", session: null },
  ports: [{ port: 8000, owner: { pid: 1, cmdline: "vite", session: null } }],
};

function withClient(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>;
}

function fileWrite(path: string, content: string): AgentEvent {
  return {
    id: `w-${path}-${content.length}`,
    kind: "action",
    thought: "",
    tool_call: { tool_name: "file_write", arguments: { path, content } },
  } as AgentEvent;
}

function liveSrc(): string | null {
  return screen.getByTitle("Live preview").getAttribute("src");
}

beforeEach(() => {
  useBuildPreviewMock.mockReturnValue({ data: LIVE });
  vi.useFakeTimers();
});
afterEach(() => {
  vi.clearAllMocks();
  vi.useRealTimers();
});

function renderPane(status: ConversationStatus, events: AgentEvent[]) {
  return render(
    withClient(<PreviewPane status={status} cid="conv_reload1" events={events} />),
  );
}

describe("PreviewPane — W-42 auto-refresh", () => {
  it("bumps reloadKey (debounced) when a file is rewritten while RUNNING", async () => {
    const v1 = [fileWrite("index.html", "<h1>one</h1>")];
    const { rerender } = renderPane("RUNNING", v1);
    await act(async () => {
      await Promise.resolve();
    });
    expect(liveSrc()).toBe("http://preview.test/?r=0");

    // Agent rewrites the file → new content → signature changes.
    const v2 = [fileWrite("index.html", "<h1>two — much longer now</h1>")];
    rerender(withClient(<PreviewPane status="RUNNING" cid="conv_reload1" events={v2} />));

    // Debounced: nothing yet before the timer fires.
    await act(async () => {
      vi.advanceTimersByTime(200);
      await Promise.resolve();
    });
    expect(liveSrc()).toBe("http://preview.test/?r=0");

    // After the debounce window the iframe reloads.
    await act(async () => {
      vi.advanceTimersByTime(600);
      await Promise.resolve();
    });
    expect(liveSrc()).toBe("http://preview.test/?r=1");
  });

  it("does NOT bump when the files are unchanged", async () => {
    const v1 = [fileWrite("index.html", "<h1>one</h1>")];
    const { rerender } = renderPane("RUNNING", v1);
    await act(async () => {
      await Promise.resolve();
    });
    expect(liveSrc()).toBe("http://preview.test/?r=0");

    // Re-render with an equivalent file set (same path + content).
    rerender(
      withClient(
        <PreviewPane
          status="RUNNING"
          cid="conv_reload1"
          events={[fileWrite("index.html", "<h1>one</h1>")]}
        />,
      ),
    );
    await act(async () => {
      vi.advanceTimersByTime(1000);
      await Promise.resolve();
    });
    expect(liveSrc()).toBe("http://preview.test/?r=0");
  });

  it("does NOT auto-reload when not RUNNING (finished preview isn't fought)", async () => {
    const v1 = [fileWrite("index.html", "<h1>one</h1>")];
    const { rerender } = renderPane("FINISHED", v1);
    await act(async () => {
      await Promise.resolve();
    });
    expect(liveSrc()).toBe("http://preview.test/?r=0");

    const v2 = [fileWrite("index.html", "<h1>two — much longer now</h1>")];
    rerender(withClient(<PreviewPane status="FINISHED" cid="conv_reload1" events={v2} />));
    await act(async () => {
      vi.advanceTimersByTime(1000);
      await Promise.resolve();
    });
    expect(liveSrc()).toBe("http://preview.test/?r=0");
  });
});
