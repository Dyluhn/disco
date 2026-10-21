import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation, useParams } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useBuild } from "@/hooks/useBuild";
import type { WSServerFrame, WSClientFrame } from "@/types/agent";
import { BuildRouteSync } from "./BuildRouteSync";

const wire = vi.hoisted(() => ({
  create: vi.fn(async () => "conv_new"),
  patch: vi.fn(async () => undefined),
  sent: [] as WSClientFrame[],
  sinks: [] as ((frame: WSServerFrame) => void)[],
}));
vi.mock("@/api/client", () => ({ agentLive: () => true, agentFetch: vi.fn(async () => ({ json: async () => ({}) })) }));
vi.mock("@/api/agent", () => ({
  createBuildConversation: wire.create,
  patchConversationSettings: wire.patch,
  killConversation: vi.fn(), resumeConversation: vi.fn(),
  subscribeConversation: (_cid: string, sink: (frame: WSServerFrame) => void) => {
    wire.sinks.push(sink);
    return { send: (frame: WSClientFrame) => wire.sent.push(frame), cancel: vi.fn() };
  },
}));
function Task({ framing }: { framing: "build" | "agent" }) {
  const { cid } = useParams();
  const b = useBuild(cid, framing);
  return <>
    <BuildRouteSync cid={b.cid} resumeCid={cid} framing={framing} hasPersistedEvents={b.events.some(e => e.kind === "message" && e.source === "user" && typeof e.seq === "number" && e.message.content === b.task)} />
    <button onClick={() => void b.ensurePreCid?.()}>Attach</button>
    <button onClick={() => void b.submit("Keep my task")}>Submit</button>
    <output>{b.cid ?? "no task"}</output>
  </>;
}
function Path() { return <div data-testid="path">{useLocation().pathname}</div>; }
function mount(framing: "build" | "agent", entry = "/") {
  return render(<QueryClientProvider client={new QueryClient()}><MemoryRouter initialEntries={[entry]}>
    <Path /><Routes>
      <Route path="/" element={<Task key="composer" framing={framing} />} />
      <Route path={`/${framing}/:cid`} element={<Task key="resumed" framing={framing} />} />
    </Routes>
  </MemoryRouter></QueryClientProvider>);
}
beforeEach(() => { wire.sent.length = 0; wire.sinks.length = 0; wire.create.mockClear(); wire.patch.mockClear(); });
describe.each(["build", "agent"] as const)("%s canonical task URL", framing => {
  it.each([false, true])("waits for persisted kickoff, survives route remount/reload; upload=%s", async upload => {
    const view = mount(framing);
    if (upload) {
      fireEvent.click(screen.getByText("Attach"));
      await waitFor(() => expect(wire.create).toHaveBeenCalledTimes(1));
    }
    fireEvent.click(screen.getByText("Submit"));
    await waitFor(() => expect(wire.sent.filter(f => f.type === "send_message")).toHaveLength(1));
    expect(screen.getByTestId("path")).toHaveTextContent(/^\/$/);
    act(() => wire.sinks.at(-1)!({ type: "event", event: {
      id: "upload-ack", seq: 1, kind: "message", source: "environment", message: { role: "user", content: "User uploaded file" },
    } }));
    expect(screen.getByTestId("path")).toHaveTextContent(/^\/$/);
    act(() => wire.sinks.at(-1)!({ type: "event", event: {
      id: "accepted", seq: 2, kind: "message", source: "user", message: { role: "user", content: "Keep my task" },
    } }));
    await waitFor(() => expect(screen.getByTestId("path")).toHaveTextContent(`/${framing}/conv_new`));
    expect(wire.create).toHaveBeenCalledTimes(1);
    expect(wire.sent.filter(f => f.type === "send_message")).toHaveLength(1);
    if (upload) expect(wire.patch).toHaveBeenCalledWith("conv_new", expect.any(Object));
    view.unmount();
    mount(framing, `/${framing}/conv_new`);
    await screen.findByText("conv_new");
    expect(wire.create).toHaveBeenCalledTimes(1);
    expect(wire.sent.filter(f => f.type === "send_message")).toHaveLength(1);
  });
});
