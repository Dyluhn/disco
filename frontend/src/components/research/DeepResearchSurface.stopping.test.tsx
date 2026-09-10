/**
 * Stopping, end to end on the surface, replayed from the REAL frames.
 *
 * The events are the verbatim WebSocket capture of the live L29 run
 * (`lib/__fixtures__/l29-stop-frames.json`) — the writer phase, the server's
 * `stop_requested` marker, the draft stream's own heartbeats, and finally the
 * run's terminal PAUSED/"stopped". Nothing is hand-written.
 *
 * The measured defect this pins: Stop published a terminal IDLE 29 ms after the
 * click, so every run control vanished and the surface collapsed to "How it
 * researched" while the engine worked on for eight more minutes. What the
 * surface must do instead is hold the stopping wall up, replace Stop with a
 * disabled "Stopping…", and hand over to the checkpoint controls only when the
 * run itself says it is done.
 *
 * Run: npx vitest run src/components/research/DeepResearchSurface.stopping.test.tsx
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ModeProvider } from "@/shell/ModeProvider";
import capture from "@/lib/__fixtures__/l29-stop-frames.json";
import resumeCapture from "@/lib/__fixtures__/l29-resume-frames.json";
import type { AgentEvent, WSClientFrame, WSServerFrame } from "@/types/agent";
import { DeepResearchSurface } from "./DeepResearchSurface";

const CID = capture.conversation_id;
const CAPTURED = capture.events as unknown as AgentEvent[];
/** Every frame the run emitted before it wrote its own ending. */
const BEFORE_TERMINAL = CAPTURED.filter((event) => event.kind !== "status");
/** The run's own ending: PAUSED / "stopped". */
const TERMINAL = CAPTURED.find((event) => event.kind === "status")!;
/** A real `research_checkpoint` frame — this capture omits its own (401 KB). */
const CHECKPOINT = (resumeCapture.events as unknown as AgentEvent[]).find(
  (event) => event.kind === "research_checkpoint",
)!;

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
          last_seq: 82,
          pending_action_id: null,
          pending_plan_id: null,
          extras: {},
        },
      });
      for (const event of BEFORE_TERMINAL) onFrame({ type: "event", event });
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

function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  return render(
    <QueryClientProvider client={client}>
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

const control = (name: string) => document.querySelector(`[data-disco-control="${name}"]`);

describe("the surface between Stop and the run's own ending", () => {
  beforeEach(() => {
    harness.sent = [];
    harness.push = null;
  });

  it("keeps the run on screen, with a wall and no live Stop", async () => {
    mount();
    await waitFor(() => expect(control("dr.stopping")).not.toBeNull());

    // The wall stands, with the step in flight named from the run's own events.
    const panel = document.querySelector('[data-dr-stopping="panel"]') as HTMLElement;
    expect(panel).not.toBeNull();
    expect(panel.textContent).toMatch(/You pressed Stop at /);
    expect(panel.textContent).toContain("Writing the report");

    // Presence implies affordance: the live Stop is gone, not merely styled.
    expect(control("dr.stop")).toBeNull();
    expect(control("dr.stopping")).toBeDisabled();

    // Steering a run that is winding down would be a second false affordance.
    expect(control("dr-steer-input")).toBeNull();

    // The progress strip is still expanded — the run has NOT ended.
    expect(screen.queryByText("How it researched")).toBeNull();
  });

  it("hands over to the checkpoint controls when the run says it is done", async () => {
    mount();
    await waitFor(() => expect(control("dr.stopping")).not.toBeNull());

    act(() => harness.push?.({ type: "event", event: TERMINAL }));

    await waitFor(() => expect(control("dr.resume")).not.toBeNull());
    expect(document.querySelector('[data-dr-stopping="panel"]')).toBeNull();
    expect(control("dr.stopping")).toBeNull();
    expect(control("dr.stop")).toBeNull();
    // Nothing was sent to the server by the wall itself.
    expect(harness.sent).toEqual([]);
  });

  it("puts the checkpoint where the wall was, not below the whole trace", async () => {
    // The panel used to mount after the progress strip, so on a long run the
    // state the user had just created was off-screen — the run appeared to end
    // in the same silence the wall had been answering. It has to take the
    // wall's place. The checkpoint frame comes from the OTHER L29 capture
    // (`l29-resume-frames.json`, seq 27): this run's own checkpoint is the
    // 401 KB event left out of the stop capture for size.
    mount();
    await waitFor(() => expect(control("dr.stopping")).not.toBeNull());
    const wall = document.querySelector('[data-dr-stopping="panel"]')!;
    expect(
      wall.compareDocumentPosition(screen.getByText("Live trace")) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    act(() => harness.push?.({ type: "event", event: CHECKPOINT }));
    act(() => harness.push?.({ type: "event", event: TERMINAL }));

    await waitFor(() => expect(document.querySelector("[data-dr-checkpoint]")).not.toBeNull());
    const checkpoint = document.querySelector("[data-dr-checkpoint]")!;
    expect(checkpoint.textContent).toMatch(/Research paused before a report was written/);
    expect(
      checkpoint.compareDocumentPosition(screen.getByText("Live trace")) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});
