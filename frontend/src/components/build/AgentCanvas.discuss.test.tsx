/**
 * AgentCanvas — W-26 "Discuss with agent" wiring (agent path) + W-43 Agent History tab.
 *
 * The agent surface's ArtifactsPane mounts SelectionOverlay over the artifact preview.
 * Previously it had NO steer wire, so a selection delivered nothing. Now onSteer is
 * threaded through; when present, a "Discuss" button forwards the named-element context
 * to the agent. No onSteer ⇒ no Discuss (no false affordance).
 *
 * useElementSelect is mocked to inject a canned selection so the overlay renders its
 * label bar (and the Discuss button) without driving the iframe postMessage flow.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AgentCanvas } from "@/components/build/AgentCanvas";
import { formatSelectionContext } from "@/lib/resolvers/appResolver";
import type { SelectionEnvelope } from "@/lib/selectionBridge";
import type { AgentEvent } from "@/types/agent";

vi.mock("@/hooks/useModels", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/hooks/useModels")>();
  return {
    ...actual,
    useLiveBrowserConfig: vi.fn(() => ({ data: { enabled: false }, isLoading: false })),
  };
});

const { resetSelectionSpy, disarmSpy, selectionRef } = vi.hoisted(() => ({
  resetSelectionSpy: vi.fn(),
  disarmSpy: vi.fn(),
  selectionRef: { current: null as SelectionEnvelope | null },
}));

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

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const fileWrite = (path: string, content: string): AgentEvent =>
  ({
    id: `w-${path}`,
    kind: "action",
    thought: "",
    tool_call: { tool_name: "file_write", arguments: { path, content } },
  }) as AgentEvent;

const HTML = fileWrite("index.html", "<html><body><h1>Out</h1></body></html>");

const SELECTION: SelectionEnvelope = {
  v: 1,
  channel: "disco-select",
  nonce: "n",
  type: "disco:selection",
  selection_ref: { kind: "source", oid: "index.html:1", file: "index.html", line: 1 },
  human_label: "h1 — “Out”",
  rect: { x: 1, y: 1, width: 10, height: 10 },
};

afterEach(() => {
  vi.clearAllMocks();
  selectionRef.current = null;
});

describe("AgentCanvas — Discuss with agent (W-26)", () => {
  it("forwards the formatted selection context to onSteer when Discuss is clicked", () => {
    selectionRef.current = SELECTION;
    const onSteer = vi.fn();
    wrap(<AgentCanvas events={[HTML]} status="FINISHED" cid="c1" onSteer={onSteer} />);
    fireEvent.click(screen.getByRole("button", { name: /discuss/i }));
    expect(onSteer).toHaveBeenCalledOnce();
    expect(onSteer).toHaveBeenCalledWith(formatSelectionContext(SELECTION));
    expect(resetSelectionSpy).toHaveBeenCalled();
  });

  it("does NOT render Discuss when onSteer is absent (non-steerable)", () => {
    selectionRef.current = SELECTION;
    wrap(<AgentCanvas events={[HTML]} status="FINISHED" cid="c1" />);
    expect(screen.queryByRole("button", { name: /discuss/i })).toBeNull();
  });
});

describe("AgentCanvas — Agent History tab (W-43)", () => {
  it("renders an Agent History tab exposing the full activity feed", async () => {
    selectionRef.current = null;
    const user = userEvent.setup();
    wrap(<AgentCanvas events={[HTML]} status="FINISHED" cid="c1" />);
    const historyTab = screen.getByRole("tab", { name: /agent history/i });
    expect(historyTab).toBeInTheDocument();
    await user.click(historyTab);
    // The feed surfaces the artifact the agent wrote (download affordance preserved).
    expect(historyTab).toHaveAttribute("aria-selected", "true");
    expect(screen.getAllByText(/index\.html/i).length).toBeGreaterThan(0);
  });
});
