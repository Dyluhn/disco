import { act, fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { WSServerFrame, WSClientFrame } from "@/types/agent";
import { useBuildStream } from "@/hooks/useBuildStream";
import { AgentStatusBar } from "./AgentStatusBar";
const wire = vi.hoisted(() => ({ sink: null as null | ((f: WSServerFrame) => void), sent: [] as WSClientFrame[] }));
vi.mock("@/api/agent", () => ({
  subscribeConversation: (_cid: string, sink: (f: WSServerFrame) => void) => {
    wire.sink = sink;
    return { send: (f: WSClientFrame) => wire.sent.push(f), cancel: vi.fn() };
  }, resumeConversation: vi.fn(),
}));
function Harness() {
  const b = useBuildStream({ cid: "stopping-task", task: "work" });
  return <AgentStatusBar status={b.status} onStop={b.cancel} onKill={vi.fn()} onResume={b.canResume ? b.resume : undefined}
    isolation={{ tier: "local", label: "Local", adversarialSafe: false }} surface="agent" />;
}
it("keeps Stop pending until authoritative PAUSED arrives, then shows Resume without reopening", () => {
  render(<Harness />);
  act(() => wire.sink!({ type: "state", state: {
    conversation_id: "stopping-task", execution_status: "RUNNING", iteration: 1, max_iterations: 100, last_seq: 10,
    active_agent_view_id: "current-view", active_agent_view_seq: 9, pending_action_id: null, pending_plan_id: null, extras: {},
  } }));
  fireEvent.click(screen.getByRole("button", { name: /^stop the agent/i }));
  expect(wire.sent.at(-1)).toEqual({ type: "pause" });
  expect(screen.getByText("Stopping…")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /resume/i })).toBeNull();
  act(() => wire.sink!({ type: "event", event: { id: "paused", seq: 11, kind: "status", source: "system", status: "PAUSED", agent_view_id: "current-view" } }));
  expect(screen.queryByText("Stopping…")).toBeNull();
  expect(screen.getByRole("button", { name: /resume/i })).toBeInTheDocument();
});
