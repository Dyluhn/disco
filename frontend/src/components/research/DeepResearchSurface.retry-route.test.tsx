import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation, useParams } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import * as agent from "@/api/agent";
import * as research from "@/api/deepResearch";
import { ModeProvider } from "@/shell/ModeProvider";
import type { WSServerFrame, WSClientFrame } from "@/types/agent";
import { DeepResearchSurface } from "./DeepResearchSurface";

function Task() { return <DeepResearchSurface resumeCid={useParams().cid} />; }
function Path() { return <output data-testid="path">{useLocation().pathname}</output>; }
function mount(entry: string) {
  return render(<QueryClientProvider client={new QueryClient()}><MemoryRouter initialEntries={[entry]}>
    <ModeProvider><Path /><Routes><Route path="/deep/:cid" element={<Task />} /></Routes></ModeProvider>
  </MemoryRouter></QueryClientProvider>);
}
afterEach(() => vi.restoreAllMocks());
it("Retry changes an old /deep URL only after the new question lands, then reloads without restarting", async () => {
  const subscriptions: { cid: string; sink: (f: WSServerFrame) => void }[] = [];
  const sent: { cid: string; frame: WSClientFrame }[] = [];
  const create = vi.spyOn(research, "createDeepResearchConversation").mockResolvedValue("new-run");
  vi.spyOn(agent, "subscribeConversation").mockImplementation((cid, sink) => {
    subscriptions.push({ cid, sink });
    return { send: (frame) => sent.push({ cid, frame }), cancel: vi.fn() };
  });
  const view = mount("/deep/failed-run");
  await waitFor(() => expect(subscriptions).toHaveLength(1));
  act(() => {
    subscriptions[0].sink({ type: "event", event: { id: "upload", seq: 0, kind: "message", source: "environment", message: { role: "user", content: "Attached notes.txt" } } });
    subscriptions[0].sink({ type: "event", event: { id: "old-question", seq: 1, kind: "message", source: "user", message: { role: "user", content: "Research retry evidence" } } });
    subscriptions[0].sink({ type: "event", event: { id: "old-error", seq: 2, kind: "status", source: "system", status: "ERROR", detail: "Test failure" } });
  });
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(subscriptions.map(s => s.cid)).toEqual(["failed-run", "new-run"]));
  expect(screen.getByTestId("path")).toHaveTextContent("/deep/failed-run");
  expect(sent).toEqual([{ cid: "new-run", frame: { type: "send_message", content: "Research retry evidence" } }]);
  act(() => subscriptions[1].sink({ type: "event", event: { id: "new-upload", seq: 0, kind: "message", source: "environment", message: { role: "user", content: "Attached notes.txt" } } }));
  expect(screen.getByTestId("path")).toHaveTextContent("/deep/failed-run");
  act(() => subscriptions[1].sink({ type: "event", event: { id: "new-question", seq: 1, kind: "message", source: "user", message: { role: "user", content: "Research retry evidence" } } }));
  await waitFor(() => expect(screen.getByTestId("path")).toHaveTextContent("/deep/new-run"));
  expect(subscriptions.map(s => s.cid)).toEqual(["failed-run", "new-run"]);
  expect(create).toHaveBeenCalledTimes(1);
  expect(sent).toHaveLength(1);
  view.unmount();
  mount("/deep/new-run");
  await waitFor(() => expect(subscriptions).toHaveLength(3));
  expect(subscriptions[2].cid).toBe("new-run");
  expect(create).toHaveBeenCalledTimes(1);
  expect(sent).toHaveLength(1);
});
