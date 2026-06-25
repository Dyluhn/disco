/**
 * Two-plane control-model tests (round-2 P1 #1): cancel / EOF / signal must
 * PREEMPT a possibly-hung in-flight prompt rather than serialize behind it.
 *
 *  - DATA plane preserves arrival order (init completes before a back-to-back
 *    prompt) — so the round-1 ordering fix stays intact.
 *  - CONTROL plane: `cancel` aborts a NEVER-RESOLVING in-flight prompt promptly,
 *    without waiting for that prompt to settle.
 *  - SIGINT/SIGTERM force-exit after the deadline when graceful teardown hangs.
 *
 * Driven against a fake {@link ControlTarget} so a "hung prompt" is deterministic
 * (no live model needed): the fake's `prompt` resolves only once `cancel` /
 * `shutdown` aborts it, mirroring how `session.abort()` unblocks a real turn.
 */
import { describe, expect, it, vi } from "vitest";

import {
  createControlPlane,
  createSerialQueue,
  type ControlTarget,
} from "../src/index.ts";
import type { KernelCommand } from "../src/protocol.ts";

const tick = () => new Promise((r) => setTimeout(r, 0));
const delay = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** A fake runner whose `prompt` hangs until an abort (cancel/shutdown) fires. */
class FakeTarget implements ControlTarget {
  events: string[] = [];
  cancelled = false;
  private releaseInFlight?: () => void;

  async handleCommand(command: KernelCommand): Promise<void> {
    this.events.push(`handle:${command.type}`);
    if (command.type === "prompt") {
      this.events.push("prompt-start");
      // Hang until aborted — exactly like a real turn awaiting session.prompt().
      await new Promise<void>((resolve) => {
        this.releaseInFlight = resolve;
      });
      this.events.push("prompt-end");
    }
  }

  async cancel(): Promise<void> {
    this.cancelled = true;
    this.events.push("cancel");
    // session.abort() unblocks the in-flight turn.
    this.releaseInFlight?.();
    this.releaseInFlight = undefined;
  }

  async shutdown(code: number): Promise<void> {
    this.events.push(`shutdown:${code}`);
    this.releaseInFlight?.();
    this.releaseInFlight = undefined;
  }
}

describe("createControlPlane data plane (P1 #1: ordering preserved)", () => {
  it("serializes init before a back-to-back prompt (init completes first)", async () => {
    const target = new FakeTarget();
    const order: string[] = [];
    const seqTarget: ControlTarget = {
      handleCommand: async (c) => {
        if (c.type === "init") {
          await delay(20);
          order.push("init-done");
        } else {
          order.push(`${c.type}-start`);
        }
      },
      cancel: async () => {},
      shutdown: async () => {},
    };
    const control = createControlPlane({
      queue: createSerialQueue(),
      target: seqTarget,
      onError: () => {},
    });

    control.dispatch({ type: "init" });
    control.dispatch({ type: "followup", text: "x" });
    await delay(60);

    expect(order).toEqual(["init-done", "followup-start"]);
    expect(target.events).toEqual([]); // unrelated fake untouched
  });
});

describe("createControlPlane control plane (P1 #1: cancel preempts a hung prompt)", () => {
  it("aborts a never-resolving in-flight prompt without waiting for it", async () => {
    const target = new FakeTarget();
    const control = createControlPlane({
      queue: createSerialQueue(),
      target,
      onError: () => {},
    });

    control.dispatch({ type: "init" });
    control.dispatch({ type: "prompt", text: "hang forever" });
    await tick();

    // The prompt is in flight and HUNG — it has not ended on its own.
    expect(target.events).toContain("prompt-start");
    expect(target.events).not.toContain("prompt-end");
    expect(target.cancelled).toBe(false);

    // Cancel must run RIGHT NOW (control plane), not behind the hung prompt.
    control.dispatch({ type: "cancel" });
    await tick();
    expect(target.cancelled).toBe(true);

    // And aborting unblocked the previously-stuck prompt.
    await tick();
    expect(target.events).toContain("prompt-end");
  });

  it("surfaces a cancel failure as a non-fatal error (does not throw)", async () => {
    const onError = vi.fn();
    const control = createControlPlane({
      queue: createSerialQueue(),
      target: {
        handleCommand: async () => {},
        cancel: async () => {
          throw new Error("abort boom");
        },
        shutdown: async () => {},
      },
      onError,
    });
    control.dispatch({ type: "cancel" });
    await tick();
    expect(onError).toHaveBeenCalledWith(expect.stringContaining("cancel failed"));
  });
});

describe("createControlPlane teardown (P1 #1: signal force-exits a stuck teardown)", () => {
  it("force-exits after the deadline when shutdown hangs", async () => {
    const forceExit = vi.fn();
    const control = createControlPlane({
      queue: createSerialQueue(),
      target: {
        handleCommand: async () => {},
        cancel: async () => {},
        // Graceful teardown that never completes (e.g. a permanently stalled
        // stdout reader blocking the final flush).
        shutdown: () => new Promise<void>(() => {}),
      },
      onError: () => {},
      forceExitMs: 20,
      forceExit,
    });

    control.signal();
    expect(forceExit).not.toHaveBeenCalled(); // not synchronous
    await delay(60);
    expect(forceExit).toHaveBeenCalledWith(0); // forced after the deadline
  });

  it("preempts the data queue: SIGTERM with a hung prompt still tears down", async () => {
    const target = new FakeTarget();
    const forceExit = vi.fn();
    const control = createControlPlane({
      queue: createSerialQueue(),
      target,
      onError: () => {},
      forceExitMs: 1000,
      forceExit,
    });

    control.dispatch({ type: "init" });
    control.dispatch({ type: "prompt", text: "hang forever" });
    await tick();
    expect(target.events).toContain("prompt-start");
    expect(target.events).not.toContain("shutdown:0");

    // Signal arrives WHILE the prompt is hung: teardown must not wait behind it.
    control.signal();
    await tick();
    expect(target.events).toContain("shutdown:0"); // ran immediately, preempting
    expect(forceExit).not.toHaveBeenCalled(); // graceful shutdown resolved in time
  });

  it("ignores commands once teardown has begun", async () => {
    const target = new FakeTarget();
    const control = createControlPlane({
      queue: createSerialQueue(),
      target,
      onError: () => {},
      forceExit: () => {},
    });
    control.eof();
    await tick();
    control.dispatch({ type: "prompt", text: "too late" });
    await tick();
    expect(target.events).not.toContain("handle:prompt");
  });
});
