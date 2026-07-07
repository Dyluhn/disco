import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { subscribeLive } from "./agent";

/** Minimal WebSocket stand-in: records instances + lets the test drive open/close. */
class MockWS {
  static instances: MockWS[] = [];
  static OPEN = 1;
  url: string;
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];
  constructor(url: string) {
    this.url = url;
    MockWS.instances.push(this);
  }
  send(d: string) {
    this.sent.push(d);
  }
  close() {
    this.readyState = 3;
    this.onclose?.();
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
}

describe("subscribeLive — reconnect with backoff", () => {
  beforeEach(() => {
    MockWS.instances = [];
    vi.stubGlobal("WebSocket", MockWS);
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("reconnects silently on a dropped socket, then surfaces an error only after giving up", async () => {
    const frames: { type: string }[] = [];
    const h = subscribeLive("conv_x", (f) => frames.push(f as { type: string }));
    await Promise.resolve();
    expect(MockWS.instances.length).toBe(1);

    // a network blip — the socket closes
    MockWS.instances[0].close();
    expect(frames.some((f) => f.type === "error")).toBe(false); // reconnecting, not fatal
    await vi.advanceTimersByTimeAsync(1000); // 1s backoff → a fresh socket
    expect(MockWS.instances.length).toBe(2);

    // keep dropping past the retry budget → it finally surfaces the error
    for (let i = 0; i < 8; i++) {
      MockWS.instances[MockWS.instances.length - 1].close();
      await vi.advanceTimersByTimeAsync(20000); // past the 15s cap
    }
    expect(frames.some((f) => f.type === "error")).toBe(true);
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
