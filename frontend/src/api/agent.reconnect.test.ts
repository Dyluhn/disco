import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { subscribeLive } from "./agent";

/** Minimal WebSocket stand-in: records instances + lets the test drive open/close. */
class MockWS {
  static instances: MockWS[] = [];
  static OPEN = 1;
  static CLOSED = 3;
  url: string;
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closeCalls = 0;
  sent: string[] = [];
  constructor(url: string) {
    this.url = url;
    MockWS.instances.push(this);
  }
  send(d: string) {
    this.sent.push(d);
  }
  close() {
    this.closeCalls += 1;
    this.readyState = MockWS.CLOSED;
    this.onclose?.();
  }
  open() {
    this.readyState = MockWS.OPEN;
    this.onopen?.();
  }
  message(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) });
  }
}

describe("subscribeLive — reconnect with backoff", () => {
  const originalVisibility = Object.getOwnPropertyDescriptor(document, "visibilityState");

  beforeEach(() => {
    MockWS.instances = [];
    vi.stubGlobal("WebSocket", MockWS);
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    if (originalVisibility) {
      Object.defineProperty(document, "visibilityState", originalVisibility);
    } else {
      delete (document as { visibilityState?: string }).visibilityState;
    }
  });

  it("force-closes a silently stale open socket, then reconnects", async () => {
    const frames: { type: string }[] = [];
    const h = subscribeLive("conv_x", (f) => frames.push(f as { type: string }));
    await Promise.resolve();
    expect(MockWS.instances.length).toBe(1);

    MockWS.instances[0].open();
    await vi.advanceTimersByTimeAsync(44_999);
    expect(MockWS.instances[0].closeCalls).toBe(0);

    await vi.advanceTimersByTimeAsync(1);
    expect(MockWS.instances[0].closeCalls).toBe(1);
    expect(frames.some((f) => f.type === "error")).toBe(false);

    await vi.advanceTimersByTimeAsync(1000);
    expect(MockWS.instances.length).toBe(2);
    h.cancel();
  });

  it("reconnects from the highest event actually delivered, not the state watermark", async () => {
    const cursors: number[] = [];
    const h = subscribeLive(
      "conv_cursor",
      () => {},
      (cid, lastSeq) => {
        expect(cid).toBe("conv_cursor");
        cursors.push(lastSeq);
        return `ws://test/ws/conversations/${cid}?last_seq=${lastSeq}`;
      },
    );
    await Promise.resolve();
    const first = MockWS.instances[0];
    first.open();
    first.message({
      type: "state",
      state: { execution_status: "FINISHED", last_seq: 99 },
    });
    first.message({
      type: "event",
      event: { id: "e7", kind: "status", source: "system", seq: 7, status: "RUNNING" },
    });
    first.close();

    await vi.advanceTimersByTimeAsync(1000);

    expect(MockWS.instances).toHaveLength(2);
    expect(cursors).toEqual([0, 7]);
    expect(MockWS.instances[1].url).toContain("?last_seq=7");
    h.cancel();
  });

  it("retries forever, surfaces degraded state after the old retry budget, and drains queued sends on reopen", async () => {
    const frames: { type: string; state?: string }[] = [];
    const h = subscribeLive("conv_x", (f) => frames.push(f as { type: string; state?: string }));
    await Promise.resolve();
    h.send({ type: "ping" });

    // Keep dropping past the former six-attempt retry budget. The stream should
    // report degraded, never emit the old fatal lost-connection error, and keep
    // creating sockets.
    for (let i = 0; i < 7; i++) {
      MockWS.instances[MockWS.instances.length - 1].close();
      await vi.advanceTimersByTimeAsync(20_000);
    }

    expect(MockWS.instances.length).toBe(8);
    expect(frames).toContainEqual({ type: "connection", state: "degraded" });
    expect(frames.some((f) => f.type === "error")).toBe(false);

    const reconnected = MockWS.instances[MockWS.instances.length - 1];
    reconnected.open();
    expect(frames).toContainEqual({ type: "connection", state: "connected" });
    expect(reconnected.sent).toEqual([JSON.stringify({ type: "ping" })]);
    h.cancel();
  });

  it("does not fire the watchdog while frames keep arriving normally", async () => {
    const h = subscribeLive("conv_fresh", () => {});
    await Promise.resolve();
    const socket = MockWS.instances[0];
    socket.open();

    for (let i = 0; i < 3; i++) {
      await vi.advanceTimersByTimeAsync(44_000);
      socket.message({ type: "pong" });
    }

    await vi.advanceTimersByTimeAsync(44_999);
    expect(socket.closeCalls).toBe(0);
    h.cancel();
  });

  it("resets the backoff attempt counter when the document becomes visible", async () => {
    let visibility: DocumentVisibilityState = "hidden";
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => visibility,
    });

    const h = subscribeLive("conv_visible", () => {});
    await Promise.resolve();

    MockWS.instances[0].close();
    await vi.advanceTimersByTimeAsync(1000);
    expect(MockWS.instances.length).toBe(2);

    MockWS.instances[1].close();
    await vi.advanceTimersByTimeAsync(2000);
    expect(MockWS.instances.length).toBe(3);

    visibility = "visible";
    document.dispatchEvent(new Event("visibilitychange"));

    MockWS.instances[2].close();
    await vi.advanceTimersByTimeAsync(999);
    expect(MockWS.instances.length).toBe(3);
    await vi.advanceTimersByTimeAsync(1);
    expect(MockWS.instances.length).toBe(4);
    h.cancel();
  });

  it("cancel() stops reconnecting (no socket churn after teardown)", async () => {
    const h = subscribeLive("conv_y", () => {});
    await Promise.resolve();
    MockWS.instances[0].close();
    await vi.advanceTimersByTimeAsync(1000);
    MockWS.instances[1].open(); // reconnected OK
    h.cancel();
    const before = MockWS.instances.length;
    MockWS.instances[1].close(); // a close after cancel must NOT reconnect
    await vi.advanceTimersByTimeAsync(30000);
    expect(MockWS.instances.length).toBe(before);
  });
});
