/**
 * P1 #1 regression: outbound (stdout) backpressure must NOT pause the stdin
 * control channel.
 *
 * The OLD wiring paused `process.stdin` whenever the outbound queue saturated.
 * Under a permanently stalled stdout that meant the SINGLE protocol channel
 * stopped being read — so a later `cancel` / EOF was never seen until stdout
 * drained, defeating the very mechanism (preemptive teardown) meant to recover
 * from a stalled reader.
 *
 * This drives the REAL `main()` wiring with an injected fake stdin (whose
 * pause/resume are spied) and a permanently-blocked stdout. It asserts:
 *  - stdin is NEVER paused even while outbound backpressure is engaged;
 *  - an injected `cancel` and a stdin EOF are still read and TERMINATE the
 *    sidecar (via the control-plane force-exit deadline) despite the dead stdout.
 */
import { EventEmitter } from "node:events";
import type { Writable } from "node:stream";

import { afterEach, describe, expect, it, vi } from "vitest";

import { main, type ReadableLike } from "../src/index.ts";

const delay = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** A Writable that reports "full" (write→false) forever once `block()` is set. */
class BlockingStream extends EventEmitter {
  chunks: string[] = [];
  private blocked = false;
  block(): void {
    this.blocked = true;
  }
  write(chunk: string): boolean {
    this.chunks.push(chunk);
    return !this.blocked;
  }
}

/** A fake stdin that records pause/resume and lets the test inject lines/EOF. */
class FakeStdin extends EventEmitter implements ReadableLike {
  pauseCount = 0;
  resumeCount = 0;
  setEncoding(): this {
    return this;
  }
  pause(): this {
    this.pauseCount++;
    return this;
  }
  resume(): this {
    this.resumeCount++;
    return this;
  }
  feed(obj: object): void {
    this.emit("data", JSON.stringify(obj) + "\n");
  }
  eof(): void {
    this.emit("end");
  }
}

describe("main() wiring (P1 #1: control channel stays readable under outbound backpressure)", () => {
  const realStderrWrite = process.stderr.write.bind(process.stderr);

  afterEach(() => {
    process.stderr.write = realStderrWrite;
  });

  it("never pauses stdin under a stalled stdout, and still reads cancel + EOF to terminate", async () => {
    // Silence the startup assertion log for clean test output.
    process.stderr.write = (() => true) as typeof process.stderr.write;

    const stdin = new FakeStdin();
    const stdout = new BlockingStream();
    stdout.block(); // stdout is dead from the start
    const exit = vi.fn();

    await main({
      stdin,
      stdout: stdout as unknown as Writable,
      exit,
      // Tiny cap so a ready frame + a single heartbeat engage backpressure fast.
      maxOutboundQueue: 1,
      forceExitMs: 30,
      attachSignals: false,
    });

    // Start a session; ready + heartbeats pile up behind the dead stdout and
    // engage outbound backpressure.
    stdin.feed({ type: "init", config: { heartbeatMs: 5 } });
    await delay(80);

    // THE INVARIANT: backpressure engaged, but the control channel was never
    // paused — so cancel / EOF remain readable.
    expect(stdin.pauseCount).toBe(0);

    // Inject a cancel (control-plane preempt) and then EOF on the live channel.
    stdin.feed({ type: "cancel" });
    stdin.eof();

    // EOF teardown can't flush the final `exit` (stdout is dead), so the
    // control-plane force-exit deadline terminates the sidecar. That it fires at
    // all proves EOF was READ despite the stall + backpressure.
    await delay(120);
    expect(exit).toHaveBeenCalledWith(0);
  });
});
