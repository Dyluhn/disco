/**
 * The hold, end to end on the surface: the panel appears when the loop reports
 * one, "Continue waiting" is a LOCAL acknowledgement (nothing crosses the WS),
 * "Stop" sends the same `cancel` the top bar sends, and `hold_resumed` clears
 * the panel and leaves the wait on the permanent trace.
 *
 * The backend emitters land with Lane A1; these frames are the shape both sides
 * build to (PLAN.md "Event contract for Phase 3").
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ModeProvider } from "@/shell/ModeProvider";
import type { AgentEvent, WSClientFrame, WSServerFrame } from "@/types/agent";
import { DeepResearchSurface } from "./DeepResearchSurface";

const CID = "conv_hold_test";
const QUERY = "What is the current state of solid-state battery commercialization?";

const harness = vi.hoisted(() => ({
  sent: [] as unknown[],
  push: null as null | ((frame: unknown) => void),
}));

vi.mock("@/api/agent", () => ({
  subscribeConversation: (_cid: string, onFrame: (f: WSServerFrame) => void) => {
    harness.push = onFrame as (frame: unknown) => void;
    setTimeout(() => {
      onFrame({
        type: "state",
        state: {
          conversation_id: CID,
          execution_status: "RUNNING",
          iteration: 1,
          max_iterations: 30,
          last_seq: 3,
          pending_action_id: null,
          pending_plan_id: null,
          extras: {},
        },
      });
      for (const event of REPLAY) onFrame({ type: "event", event });
    }, 0);
    return {
      send: (frame: WSClientFrame) => harness.sent.push(frame),
      cancel: () => {},
    };
  },
  killConversation: () => Promise.resolve(),
  listDriverModels: () => Promise.resolve({ models: [], default: null }),
  getLastSelectedModel: () => Promise.resolve(null),
}));

/** Timestamps sit just before "now" so ages read as a live run, not a replay. */
function iso(offsetSeconds: number): string {
  return new Date(Date.now() + offsetSeconds * 1000).toISOString();
}

const REPLAY: AgentEvent[] = [
  {
    id: "e1",
    kind: "message",
    source: "user",
    seq: 1,
    timestamp: iso(-300),
    message: { role: "user", content: QUERY },
  },
  {
    id: "e2",
    kind: "action",
    source: "agent",
    seq: 2,
    timestamp: iso(-295),
    thought: "",
    tool_call: { tool_name: "brief", arguments: { text: "Chase the pilot lines." } },
  },
  {
    id: "e3",
    kind: "action",
    source: "agent",
    seq: 3,
    timestamp: iso(-5),
    thought: "",
    tool_call: {
      tool_name: "hold",
      arguments: {
        reason: "search_pool_cooling",
        engines: [
          { name: "brave", resume_at: iso(220) },
          { name: "mojeek", resume_at: iso(250) },
        ],
        resume_at: iso(220),
        sources_retained: 42,
        turn: { n: 7, of: 16 },
        queued_queries: ["pilot line capacity", "cycle life field data", "separator supply"],
      },
    },
  },
];

function renderSurface() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/deep/${CID}`]}>
        <ModeProvider>
          <Routes>
            <Route path="/deep/:cid" element={<DeepResearchSurface resumeCid={CID} />} />
          </Routes>
        </ModeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function renderWithHold() {
  renderSurface();
  return waitFor(
    () => screen.getByRole("region", { name: /Waiting for search engines/i }),
    { timeout: 3000 },
  );
}

describe("Deep Research surface — the loop holds, the user chooses", () => {
  beforeEach(() => {
    window.localStorage.clear();
    harness.sent.length = 0;
    harness.push = null;
  });

  it("surfaces the hold as a choice with the engines named", async () => {
    const panel = await renderWithHold();
    expect(panel).toHaveTextContent("brave and mojeek hit their rate limits and are cooling down.");
    expect(panel).toHaveTextContent("Turn 7 of 16 · 42 sources kept · 3 queries waiting to run");
    expect(panel).toHaveTextContent(/Research resumes on its own in 3:\d\d\./);
    // A hold is not an error state: the run's error surface stays absent.
    expect(screen.queryByText(/The research run failed/i)).not.toBeInTheDocument();
  });

  it("Continue waiting collapses to the heartbeat line and sends NOTHING", async () => {
    const user = userEvent.setup();
    await renderWithHold();
    expect(harness.sent).toEqual([]);

    await user.click(screen.getByRole("button", { name: /Continue waiting/i }));

    expect(
      screen.queryByRole("region", { name: /Waiting for search engines/i }),
    ).not.toBeInTheDocument();
    const line = document.querySelector('[data-dr-hold="line"]');
    expect(line).not.toBeNull();
    expect(line).toHaveTextContent(/Waiting for search engines — resumes in 3:\d\d/);
    // The whole point of Continue: the loop keeps its own schedule, untouched.
    expect(harness.sent).toEqual([]);
  });

  it("Stop sends the cancel frame that yields a resumable checkpoint", async () => {
    const user = userEvent.setup();
    await renderWithHold();
    await user.click(screen.getByRole("button", { name: /Stop and keep what it has/i }));
    expect(harness.sent).toEqual([{ type: "cancel" }]);
  });

  it("hold_resumed clears the panel and leaves the wait on the trace", async () => {
    await renderWithHold();
    act(() => {
      harness.push?.({
        type: "event",
        event: {
          id: "e4",
          kind: "action",
          source: "agent",
          seq: 4,
          timestamp: iso(0),
          thought: "",
          tool_call: {
            tool_name: "hold_resumed",
            arguments: { waited_s: 242, engines_live: ["brave", "mojeek"] },
          },
        },
      });
    });

    await waitFor(() =>
      expect(
        screen.queryByRole("region", { name: /Waiting for search engines/i }),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByText("Resumed after 4:02 — brave, mojeek live")).toBeInTheDocument();
    expect(screen.getByText("Waiting for search engines to cool down")).toBeInTheDocument();
  });
});
