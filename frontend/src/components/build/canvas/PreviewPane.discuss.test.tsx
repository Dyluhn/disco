/**
 * PreviewPane — W-26 "Discuss with agent" wiring (build path).
 *
 * The build PreviewPane mounts SelectionOverlay over the srcdoc preview. When a
 * selection exists AND steering is available (onSteer defined), a "Discuss" button
 * forwards the named-element context to the agent via onSteer (the steer →
 * send_message path). No onSteer ⇒ no Discuss (no false affordance).
 *
 * useElementSelect is mocked to inject a canned selection so the overlay's label bar
 * (and thus the Discuss button) renders without driving the iframe postMessage flow.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PreviewPane } from "@/components/build/canvas/PreviewPane";
import { formatSelectionContext } from "@/lib/resolvers/appResolver";
import type { SelectionEnvelope } from "@/lib/selectionBridge";
import type { AgentEvent, PreviewInfo } from "@/types/agent";

const {
  previewBootstrapUrlMock,
  useBuildPreviewMock,
  resetSelectionSpy,
  disarmSpy,
  selectionRef,
} = vi.hoisted(() => ({
  previewBootstrapUrlMock: vi.fn(() => new Promise<never>(() => {})),
  useBuildPreviewMock: vi.fn<[{ data: PreviewInfo | null }]>(() => ({ data: null })),
  resetSelectionSpy: vi.fn(),
  disarmSpy: vi.fn(),
  selectionRef: { current: null as SelectionEnvelope | null },
}));

vi.mock("@/hooks/useBuildPreview", () => ({
  useBuildPreview: (...args: unknown[]) => useBuildPreviewMock(...args),
}));

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return {
    ...actual,
    agentHttpBase: () => "http://agent.test:8000",
    previewBootstrapUrl: previewBootstrapUrlMock,
  };
});

vi.mock("@/hooks/useElementSelect", () => ({
  useElementSelect: () => ({
    armed: true,
    arm: vi.fn(),
    disarm: disarmSpy,
    selection: selectionRef.current,
    walkUp: vi.fn(),
    resetSelection: resetSelectionSpy,
  }),
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

const HTML = fileWrite("index.html", "<html><body><h1>Done</h1></body></html>");

const SELECTION: SelectionEnvelope = {
  v: 1,
  channel: "disco-select",
  nonce: "n",
  type: "disco:selection",
  selection_ref: { kind: "source", oid: "index.html:1", file: "index.html", line: 1 },
  human_label: "h1 — “Done”",
  rect: { x: 5, y: 5, width: 40, height: 20 },
};

beforeEach(() => {
  useBuildPreviewMock.mockReturnValue({ data: null });
  selectionRef.current = SELECTION;
});
afterEach(() => {
  vi.clearAllMocks();
});

describe("PreviewPane — Discuss with agent (W-26)", () => {
  it("forwards the formatted selection context to onSteer when Discuss is clicked", () => {
    const onSteer = vi.fn();
    render(withClient(<PreviewPane status="FINISHED" cid="c1" events={[HTML]} onSteer={onSteer} />));
    fireEvent.click(screen.getByRole("button", { name: /discuss/i }));
    expect(onSteer).toHaveBeenCalledOnce();
    expect(onSteer).toHaveBeenCalledWith(formatSelectionContext(SELECTION));
    // the selection is cleared after dispatch (no stale highlight)
    expect(resetSelectionSpy).toHaveBeenCalled();
  });

  it("does NOT render Discuss when onSteer is absent (non-steerable → no false affordance)", () => {
    render(withClient(<PreviewPane status="FINISHED" cid="c1" events={[HTML]} />));
    expect(screen.queryByRole("button", { name: /discuss/i })).toBeNull();
  });
});
