import { afterEach, expect, it, vi } from "vitest";
import { rrfAnswer } from "@/fixtures/answers";
vi.mock("./client", async (orig) => ({
  ...(await orig<typeof import("./client")>()),
  researchWsUrl: () => "ws://localhost/ws/research",
  ensureAgentSession: async () => undefined,
}));
import { requestResearch, subscribeResearch } from "./research";
class Socket {
  static latest: Socket;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  send = vi.fn();
  close = vi.fn(() => this.onclose?.());
  constructor() { Socket.latest = this; }
}
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });
it("reports unexpected disconnect instead of leaving Search running forever", async () => {
  vi.stubGlobal("WebSocket", Socket);
  const onFrame = vi.fn();
  subscribeResearch({ query: "q" }, onFrame);
  await Promise.resolve();
  Socket.latest.onclose?.();
  expect(onFrame).toHaveBeenCalledWith(expect.objectContaining({ type: "error" }));
});
it("a durable final answer remains complete if the terminal socket frame is lost", async () => {
  vi.stubGlobal("WebSocket", Socket);
  const onFrame = vi.fn();
  const handle = subscribeResearch({ query: "q" }, onFrame);
  await Promise.resolve();
  Socket.latest.onmessage?.({ data: JSON.stringify({ type: "final", answer: rrfAnswer }) });
  Socket.latest.onclose?.();
  handle.cancel();
  expect(onFrame).toHaveBeenCalledTimes(1);
  expect(onFrame).toHaveBeenCalledWith({ type: "final", answer: rrfAnswer });
});
it("fresh explicit requests get distinct identities without HTTPS-only randomUUID", async () => {
  vi.spyOn(crypto, "randomUUID").mockImplementation(() => { throw new Error("Unavailable on LAN HTTP"); });
  const scope = { query: "q", request_id: "prior" };
  const first = await requestResearch(scope);
  const second = await requestResearch(scope);
  expect(first.request_id).toMatch(/^[a-f0-9]{32}$/);
  expect(second.request_id).not.toBe(first.request_id);
  expect(scope.request_id).toBe("prior");
});

it.each([undefined, null, "chosen-model"])(
  "preserves omitted, inherited-default and explicit model selection on the wire (%s)",
  async (selection) => {
    vi.stubGlobal("WebSocket", Socket);
    const handle = subscribeResearch({ query: "q", model_override: selection }, vi.fn());
    await Promise.resolve();
    Socket.latest.onopen?.();
    const request = JSON.parse(Socket.latest.send.mock.calls[0][0]);
    if (selection === undefined) expect(request).not.toHaveProperty("model_override");
    else expect(request).toHaveProperty("model_override", selection);
    handle.cancel();
  },
);
