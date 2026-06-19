/**
 * PreviewPane — A1.6 edit-mode toggle.
 *
 * The "Edit" toggle appears only when there's a real steer wire (onSteer), a
 * client-side entry HTML to stamp, a conversation, and a trusted run. Toggling it
 * swaps the client-side `srcDoc` iframe for the server-stamped preview-edit route
 * (`src=`), where click-to-edit works. No onSteer / untrusted / no-cid → no toggle
 * (no false affordance).
 *
 * Direct PreviewPane render (not via Radix Tabs, which lazy-mounts panels).
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PreviewPane } from "@/components/build/canvas/PreviewPane";
import type { AgentEvent, PreviewInfo } from "@/types/agent";

const { useBuildPreviewMock } = vi.hoisted(() => ({
  useBuildPreviewMock: vi.fn<[{ data: PreviewInfo | null }]>(() => ({ data: null })),
}));

vi.mock("@/hooks/useBuildPreview", () => ({
  useBuildPreview: (...args: unknown[]) => useBuildPreviewMock(...args),
}));

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return { ...actual, agentHttpBase: () => "http://agent.test:8000" };
});

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

const HTML = fileWrite("index.html", "<html><body><h1>Done</h1></body></html>");

beforeEach(() => {
  useBuildPreviewMock.mockReturnValue({ data: null });
});
afterEach(() => {
  vi.clearAllMocks();
});

describe("PreviewPane — A1.6 edit toggle", () => {
  it("shows the Edit toggle when onSteer + an entry HTML + cid are present", () => {
    render(
      withClient(
        <PreviewPane status="FINISHED" cid="conv_edit1" events={[HTML]} onSteer={vi.fn()} />,
      ),
    );
    expect(screen.getByRole("button", { name: /Edit/i })).toBeInTheDocument();
    // Default (view) mode renders the client-side srcDoc, not the edit route.
    expect(screen.getByTitle("Static preview")).toBeInTheDocument();
    expect(screen.queryByTitle("Editable preview")).not.toBeInTheDocument();
  });

  it("toggling Edit swaps the iframe to the server preview-edit route URL", () => {
    render(
      withClient(
        <PreviewPane status="FINISHED" cid="conv_edit2" events={[HTML]} onSteer={vi.fn()} />,
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: /Edit/i }));
    const iframe = screen.getByTitle("Editable preview");
    expect(iframe).toBeInTheDocument();
    expect(iframe.getAttribute("src")).toBe(
      "http://agent.test:8000/conversations/conv_edit2/preview-edit/index.html",
    );
    // The edit frame must NOT grant same-origin (origin stays "null").
    expect(iframe.getAttribute("sandbox")).toBe("allow-scripts");
  });

  it("hides the Edit toggle when onSteer is absent (nothing to steer)", () => {
    render(withClient(<PreviewPane status="FINISHED" cid="conv_edit3" events={[HTML]} />));
    expect(screen.queryByRole("button", { name: /Edit/i })).not.toBeInTheDocument();
  });

  it("hides the Edit toggle for an untrusted run", () => {
    render(
      withClient(
        <PreviewPane
          status="FINISHED"
          cid="conv_edit4"
          events={[HTML]}
          onSteer={vi.fn()}
          untrusted
        />,
      ),
    );
    expect(screen.queryByRole("button", { name: /Edit/i })).not.toBeInTheDocument();
  });
});
