import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { expect, it, vi } from "vitest";
import type { WSServerFrame, WSClientFrame } from "@/types/agent";
import { useDeepResearch } from "./useDeepResearch";

const wire = vi.hoisted(() => ({
  create: vi.fn<() => Promise<string>>(),
  subscriptions: [] as { cid: string; sink: (frame: WSServerFrame) => void; cancel: ReturnType<typeof vi.fn> }[],
  sent: [] as { cid: string; frame: WSClientFrame }[],
}));
vi.mock("@/api/client", () => ({ agentLive: () => true }));
vi.mock("@/api/deepResearch", () => ({ createDeepResearchConversation: wire.create, exportReport: vi.fn(), exportReportAsMarkdown: vi.fn() }));
vi.mock("@/api/agent", () => ({
  killConversation: vi.fn(), patchConversationSettings: vi.fn(),
  subscribeConversation: (cid: string, sink: (frame: WSServerFrame) => void) => {
    const cancel = vi.fn(); wire.subscriptions.push({ cid, sink, cancel });
    return { send: (frame: WSClientFrame) => wire.sent.push({ cid, frame }), cancel };
  },
}));
it("Retry keeps the new session while the old route remains and discards retired stream frames", async () => {
  let complete!: (cid: string) => void;
  wire.create.mockImplementationOnce(() => new Promise(resolve => { complete = resolve; }));
  const observed: { cid: string | null; ids: string[] }[] = [];
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const { result, rerender } = renderHook(({ route }) => {
    const r = useDeepResearch(route);
    observed.push({ cid: r.cid, ids: r.events.map(e => e.id) });
    return r;
  }, { initialProps: { route: "old" }, wrapper });
  await waitFor(() => expect(wire.subscriptions).toHaveLength(1));
  const old = wire.subscriptions[0];
  act(() => old.sink({ type: "event", event: { id: "old-question", seq: 1, kind: "message", source: "user", message: { role: "user", content: "Research the actual question" } } }));
  act(() => result.current.retry());
  await waitFor(() => expect(wire.create).toHaveBeenCalledTimes(1));
  expect(result.current.cid).toBeNull();
  await act(async () => complete("new"));
  await waitFor(() => expect(result.current.cid).toBe("new"));
  expect(wire.subscriptions.map(s => s.cid)).toEqual(["old", "new"]);
  expect(wire.sent).toEqual([{ cid: "new", frame: { type: "send_message", content: "Research the actual question" } }]);
  expect(old.cancel).toHaveBeenCalledOnce();
  expect(observed.filter(s => s.cid === "new").every(s => !s.ids.includes("old-question"))).toBe(true);
  act(() => old.sink({ type: "event", event: { id: "late-error", seq: 99, kind: "status", source: "system", status: "ERROR" } }));
  expect(result.current.events).toHaveLength(0);
  expect(result.current.status).toBe("IDLE");
  rerender({ route: "new" });
  expect(wire.subscriptions.map(s => s.cid)).toEqual(["old", "new"]);
  expect(wire.sent).toHaveLength(1);
  rerender({ route: "another" });
  await waitFor(() => expect(result.current.cid).toBe("another"));
  expect(wire.sent).toHaveLength(1);
});
