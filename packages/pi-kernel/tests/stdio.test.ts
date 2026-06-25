/**
 * Unit tests for the hardened stdio transport (codex review fixes):
 *  - createLineReader: byte-capped inbound lines (giant/unterminated frames are
 *    rejected without parsing; the reader resyncs and keeps working).
 *  - createOutboundWriter: stdout backpressure is respected (writes await
 *    `drain`, the buffer stays bounded) and agent_event frames are never dropped
 *    while heartbeats coalesce under pressure.
 *  - createSerialQueue: strict FIFO; a task fully settles before the next runs.
 *
 * These drive the exported transport helpers against fake streams, so they are
 * deterministic and need no subprocess.
 */
import { EventEmitter } from "node:events";
import type { Writable } from "node:stream";

import { describe, expect, it } from "vitest";

import {
  MAX_LINE_BYTES,
  createLineReader,
  createOutboundWriter,
  createSerialQueue,
} from "../src/index.ts";
import { PiKernelRunner } from "../src/runner.ts";
import type { KernelOutbound } from "../src/protocol.ts";
import type { AgentSessionEvent } from "@earendil-works/pi-coding-agent";

describe("createLineReader (P1: inbound byte cap)", () => {
  it("emits an overflow for an over-cap line and keeps handling later valid lines", () => {
    const lines: string[] = [];
    const overflows: number[] = [];
    const cap = 64;
    const feed = createLineReader(
      { onLine: (l) => lines.push(l), onOverflow: (b) => overflows.push(b) },
      cap,
    );

    // A single oversized, NEWLINE-TERMINATED frame: rejected, never delivered.
    feed("X".repeat(cap + 10) + "\n");
    expect(overflows).toHaveLength(1);
    expect(overflows[0]).toBeGreaterThan(cap);
    expect(lines).toHaveLength(0);

    // The reader resynced: a subsequent valid line is delivered normally.
    feed('{"type":"init"}\n');
    expect(lines).toEqual(['{"type":"init"}']);
  });

  it("caps an UNTERMINATED giant frame fed in pieces (no unbounded buffering)", () => {
    const lines: string[] = [];
    const overflows: number[] = [];
    const cap = 128;
    const feed = createLineReader(
      { onLine: (l) => lines.push(l), onOverflow: (b) => overflows.push(b) },
      cap,
    );

    // Stream a huge frame with NO terminating newline across many chunks.
    for (let i = 0; i < 50; i++) feed("A".repeat(100));
    expect(overflows.length).toBeGreaterThanOrEqual(1);
    expect(lines).toHaveLength(0);

    // Once the over-cap line is finally terminated, the reader resyncs and the
    // next valid line still parses — the process is unharmed.
    feed("tail-of-giant-line\n");
    feed('{"type":"cancel"}\n');
    expect(lines).toEqual(['{"type":"cancel"}']);
  });

  it("delivers in-cap lines split across chunk boundaries", () => {
    const lines: string[] = [];
    const feed = createLineReader(
      { onLine: (l) => lines.push(l), onOverflow: () => {} },
      MAX_LINE_BYTES,
    );
    feed('{"type":');
    feed('"prompt","text":"hi"}');
    feed("\n");
    expect(lines).toEqual(['{"type":"prompt","text":"hi"}']);
  });
});

/** A Writable that reports "full" (write→false) until `release()` flushes it. */
class BlockingStream extends EventEmitter {
  chunks: string[] = [];
  private blocked = false;

  block(): void {
    this.blocked = true;
  }
  release(): void {
    this.blocked = false;
    this.emit("drain");
  }
  write(chunk: string): boolean {
    this.chunks.push(chunk);
    return !this.blocked; // false signals backpressure
  }
}

describe("createOutboundWriter (P1: stdout backpressure)", () => {
  it("awaits drain under backpressure and never drops agent_event frames", async () => {
    const stream = new BlockingStream();
    const writer = createOutboundWriter(stream as unknown as Writable);

    stream.block();

    const agentEvent: KernelOutbound = { type: "agent_event", event: { kind: "k1" } };
    writer.write(agentEvent); // first write → stream reports full, pump parks on drain
    writer.write({ type: "agent_event", event: { kind: "k2" } });
    writer.write({ type: "agent_event", event: { kind: "k3" } });

    // Pressure on: only the first frame reached the stream; the rest are queued
    // (bounded), not flushed and not dropped.
    expect(stream.chunks).toHaveLength(1);

    // Reader catches up.
    stream.release();
    await writer.flushed();

    const kinds = stream.chunks.map((c) => (JSON.parse(c) as { event: { kind: string } }).event.kind);
    expect(kinds).toEqual(["k1", "k2", "k3"]); // all three, in order, none dropped
  });

  it("coalesces heartbeats under pressure but preserves agent_events", async () => {
    const stream = new BlockingStream();
    const writer = createOutboundWriter(stream as unknown as Writable);

    stream.block();
    writer.write({ type: "agent_event", event: { kind: "first" } }); // parks on drain
    // A flood of heartbeats while the reader is stalled must collapse to one.
    for (let ts = 0; ts < 100; ts++) writer.write({ type: "heartbeat", ts });
    writer.write({ type: "agent_event", event: { kind: "second" } });

    stream.release();
    await writer.flushed();

    const frames = stream.chunks.map((c) => JSON.parse(c) as KernelOutbound);
    const agentKinds = frames
      .filter((f): f is Extract<KernelOutbound, { type: "agent_event" }> => f.type === "agent_event")
      .map((f) => (f.event as { kind: string }).kind);
    const heartbeats = frames.filter((f) => f.type === "heartbeat");

    expect(agentKinds).toEqual(["first", "second"]); // both agent_events survive
    expect(heartbeats.length).toBe(1); // 100 pings coalesced to the freshest one
    expect(heartbeats[0]).toMatchObject({ type: "heartbeat", ts: 99 });
  });
});

describe("createOutboundWriter (P1 #2: BOUNDED outbound queue)", () => {
  it("bounds the queue under a stalled reader: producer is backpressured, nothing dropped", async () => {
    const stream = new BlockingStream();
    const cap = 8;
    let backpressure: boolean | undefined;
    const writer = createOutboundWriter(stream as unknown as Writable, {
      maxQueue: cap,
      onBackpressure: (active) => {
        backpressure = active;
      },
    });

    stream.block();

    // A cooperating producer writes until told to back off.
    const produced: string[] = [];
    let backedOff = false;
    for (let i = 0; i < 1000; i++) {
      const frame: KernelOutbound = { type: "agent_event", event: { kind: `k${i}` } };
      produced.push(`k${i}`);
      const ok = writer.write(frame);
      if (!ok) {
        backedOff = true;
        break; // respect backpressure rather than growing memory
      }
    }

    // The producer was stopped FAR short of 1000; the buffer stayed bounded.
    expect(backedOff).toBe(true);
    expect(backpressure).toBe(true);
    // At most cap frames are buffered + the one in-flight frame parked on drain.
    expect(produced.length).toBeLessThanOrEqual(cap + 1);

    // Reader catches up — everything accepted is delivered, in order, none dropped.
    stream.release();
    await writer.flushed();
    const kinds = stream.chunks.map(
      (c) => (JSON.parse(c) as { event: { kind: string } }).event.kind,
    );
    expect(kinds).toEqual(produced);
    expect(backpressure).toBe(false); // relieved once drained
  });

  it("whenWritable resolves once the queue drains below the cap", async () => {
    const stream = new BlockingStream();
    const writer = createOutboundWriter(stream as unknown as Writable, { maxQueue: 4 });
    stream.block();

    let ok = true;
    while (ok) ok = writer.write({ type: "agent_event", event: { kind: "x" } });

    let writableResolved = false;
    void writer.whenWritable().then(() => {
      writableResolved = true;
    });
    // Still congested: the producer stays parked.
    await new Promise((r) => setTimeout(r, 0));
    expect(writableResolved).toBe(false);

    stream.release();
    await writer.flushed();
    await new Promise((r) => setTimeout(r, 0));
    expect(writableResolved).toBe(true);
  });

  it("error/exit bypass the cap and still flush on teardown", async () => {
    const stream = new BlockingStream();
    const writer = createOutboundWriter(stream as unknown as Writable, { maxQueue: 4 });
    stream.block();

    // Saturate the cap with data frames (a misbehaving producer ignoring backpressure).
    for (let i = 0; i < 20; i++) {
      writer.write({ type: "agent_event", event: { kind: `k${i}` } });
    }
    // A terminal error + exit must still be accepted (reserved, cap-exempt path).
    writer.write({ type: "error", message: "boom", fatal: true });
    writer.write({ type: "exit", code: 0 });

    stream.release();
    await writer.flushed();

    const frames = stream.chunks.map((c) => JSON.parse(c) as KernelOutbound);
    expect(frames.some((f) => f.type === "error")).toBe(true);
    expect(frames.some((f) => f.type === "exit")).toBe(true);
    // exit is the last frame written.
    expect(frames[frames.length - 1]).toMatchObject({ type: "exit", code: 0 });
  });
});

describe("createOutboundWriter (P1 round-3: error frames cannot OOM the queue)", () => {
  it("coalesces a flood of identical NON-fatal diagnostic errors (queue stays bounded)", async () => {
    const stream = new BlockingStream();
    const writer = createOutboundWriter(stream as unknown as Writable, { maxQueue: 4 });
    stream.block();

    // A hostile peer spams the SAME malformed-input error frame thousands of
    // times (the realistic OOM vector — index.ts emits a constant "malformed
    // JSON line" message). Identical consecutive errors must coalesce instead of
    // accumulating, so memory does NOT grow with the flood.
    for (let i = 0; i < 10_000; i++) {
      writer.write({ type: "error", message: "malformed JSON line" });
    }

    stream.release();
    await writer.flushed();

    const errors = stream.chunks
      .map((c) => JSON.parse(c) as KernelOutbound)
      .filter((f) => f.type === "error");
    // 10k identical errors collapse to a tiny constant (the one parked in-flight
    // on the stream + at most one queued), NOT 10k frames.
    expect(errors.length).toBeLessThanOrEqual(2);
    expect(errors.length).toBeGreaterThanOrEqual(1);
  });

  it("non-fatal errors are cap-ELIGIBLE (engage backpressure); only exit + FATAL errors stay cap-exempt", async () => {
    const stream = new BlockingStream();
    let backpressure = false;
    const writer = createOutboundWriter(stream as unknown as Writable, {
      maxQueue: 4,
      onBackpressure: (active) => {
        backpressure = active;
      },
    });
    stream.block();

    // DISTINCT non-fatal errors fill the cap and trip backpressure — proof they
    // count toward the cap now (previously they were cap-exempt and unbounded).
    let backedOff = false;
    for (let i = 0; i < 100; i++) {
      const ok = writer.write({ type: "error", message: `bad line ${i}` });
      if (!ok) {
        backedOff = true;
        break;
      }
    }
    expect(backedOff).toBe(true);
    expect(backpressure).toBe(true);

    // A FATAL error and the terminal exit are STILL cap-exempt and flush on teardown.
    writer.write({ type: "error", message: "init failed", fatal: true });
    writer.write({ type: "exit", code: 1 });

    stream.release();
    await writer.flushed();
    const frames = stream.chunks.map((c) => JSON.parse(c) as KernelOutbound);
    expect(frames.some((f) => f.type === "error" && f.fatal === true)).toBe(true);
    expect(frames[frames.length - 1]).toMatchObject({ type: "exit", code: 1 });
  });
});

describe("createOutboundWriter (P1 round-4: cap enforced AT THE QUEUE, not per-producer)", () => {
  it("bounds the queue when a producer IGNORES the false return: 10k distinct oversized errors stay <= maxQueue", async () => {
    const stream = new BlockingStream();
    const maxQueue = 4;
    const writer = createOutboundWriter(stream as unknown as Writable, { maxQueue });
    stream.block(); // stdout permanently stalled

    // Model the INBOUND diagnostic-error producer (index.ts onLine/onOverflow):
    // it calls write() and IGNORES the false return (stdin deliberately stays
    // readable for cancel/EOF). Each frame embeds a UNIQUE byteLength, so they
    // are DISTINCT and bypass the identical-message coalescing — the exact codex
    // repro. The choke point must refuse to grow the queue regardless.
    let acceptedAfterFirstFalse = 0;
    let sawFalse = false;
    for (let i = 0; i < 10_000; i++) {
      const ok = writer.write({
        type: "error",
        message: `inbound line exceeds cap (${i} bytes); frame discarded`,
      });
      if (!ok) sawFalse = true;
      else if (sawFalse) acceptedAfterFirstFalse += 1;
    }

    // The producer was told to back off (false) and then kept writing anyway.
    expect(sawFalse).toBe(true);
    // OLD push-always behavior accepted ~10k frames after the first false (codex:
    // "10,000 distinct non-fatal errors were accepted after the first false").
    // The choke point accepts NONE past the cap.
    expect(acceptedAfterFirstFalse).toBe(0);

    // PROVE the queue stayed bounded: release the reader and count everything that
    // drains out. If the queue had grown to 10k, ~10k frames would flush. It
    // stays at most maxQueue (buffered) + 1 (parked in-flight) + a coalesced
    // drop-summary that reuses an existing slot — a small constant, NOT 10k.
    stream.release();
    await writer.flushed();
    expect(stream.chunks.length).toBeLessThanOrEqual(maxQueue + 1);

    // The loss is surfaced HONESTLY: a bounded running summary, not silence.
    const errors = stream.chunks
      .map((c) => JSON.parse(c) as KernelOutbound)
      .filter((f): f is Extract<KernelOutbound, { type: "error" }> => f.type === "error");
    expect(errors.some((e) => /frame\(s\) dropped under backpressure/.test(e.message))).toBe(true);
  });

  it("reserved exit + FATAL error still flush even while cap-eligible frames are being refused", async () => {
    const stream = new BlockingStream();
    const writer = createOutboundWriter(stream as unknown as Writable, { maxQueue: 4 });
    stream.block();

    // Flood the cap with distinct non-fatal errors (ignoring backpressure).
    for (let i = 0; i < 5_000; i++) {
      writer.write({ type: "error", message: `distinct ${i}` });
    }
    // Terminal frames are reserved (cap-exempt) and must STILL be accepted+flushed.
    writer.write({ type: "error", message: "init failed", fatal: true });
    writer.write({ type: "exit", code: 1 });

    stream.release();
    await writer.flushed();

    const frames = stream.chunks.map((c) => JSON.parse(c) as KernelOutbound);
    expect(frames.some((f) => f.type === "error" && f.fatal === true)).toBe(true);
    expect(frames[frames.length - 1]).toMatchObject({ type: "exit", code: 1 });
    // Still bounded despite the 5k flood.
    expect(frames.length).toBeLessThanOrEqual(4 + 1 + 2);
  });

  it("NEGATIVE CONTROL: the old push-always behavior grows unbounded and FAILS the new bound", () => {
    // A faithful model of the pre-fix writer: write() always pushes a cap-eligible
    // frame, returning false at/over the cap but NEVER refusing the push. A
    // producer that ignores the false (the inbound diagnostic-error path) grows
    // the queue without bound — this is the bug the choke point fixes.
    const maxQueue = 4;
    const legacyQueue: KernelOutbound[] = [];
    const legacyPushAlwaysWrite = (frame: KernelOutbound): boolean => {
      legacyQueue.push(frame); // always grows — the defect
      return legacyQueue.length < maxQueue;
    };

    let sawFalse = false;
    for (let i = 0; i < 10_000; i++) {
      const ok = legacyPushAlwaysWrite({
        type: "error",
        message: `inbound line exceeds cap (${i} bytes); frame discarded`,
      });
      if (!ok) sawFalse = true;
    }

    expect(sawFalse).toBe(true);
    // The old behavior accepts all 10k — it would FAIL `length <= maxQueue + 1`.
    expect(legacyQueue.length).toBe(10_000);
    expect(legacyQueue.length).toBeGreaterThan(maxQueue + 1);
  });
});

describe("PiKernelRunner.forwardAgentEvent (P1 round-3: producer-side backpressure)", () => {
  it("backpressures the in-flight prompt under a stalled stdout: queue bounded, nothing dropped, order kept", async () => {
    const stream = new BlockingStream();
    const cap = 8;
    const writer = createOutboundWriter(stream as unknown as Writable, { maxQueue: cap });
    const runner = new PiKernelRunner({
      emit: (frame) => writer.write(frame),
      whenWritable: () => writer.whenWritable(),
    });

    stream.block(); // permanently stalled reader

    // Model the in-flight prompt as a producer emitting MANY agent_events,
    // awaiting the runner's backpressure point before each (a cooperating
    // producer — exactly what the EPIC C inference gateway will be).
    const TOTAL = 500;
    const produced: number[] = [];
    let done = false;
    const inFlightPrompt = (async () => {
      for (let i = 0; i < TOTAL; i++) {
        await runner.forwardAgentEvent({ type: "tok", n: i } as unknown as AgentSessionEvent);
        produced.push(i);
      }
      done = true;
    })();

    // Let production run until it parks on backpressure.
    await new Promise((r) => setTimeout(r, 20));

    // The PRODUCER was paused far short of TOTAL — it awaited writer capacity
    // instead of flooding the queue. Total buffered stays bounded by the cap.
    expect(done).toBe(false);
    expect(produced.length).toBeLessThanOrEqual(cap + 2);
    // Only one frame reached the stalled stream; the rest never exceeded the cap.
    expect(stream.chunks.length).toBe(1);

    // Reader catches up: production resumes and everything emitted is delivered
    // in order, none dropped.
    stream.release();
    await inFlightPrompt;
    await writer.flushed();

    expect(done).toBe(true);
    const order = stream.chunks.map(
      (c) => (JSON.parse(c) as { event: { raw: { n: number } } }).event.raw.n,
    );
    const expected = Array.from({ length: TOTAL }, (_, i) => i);
    expect(order).toEqual(expected);
    expect(produced).toEqual(expected);
  });
});

describe("PiKernelRunner.forwardAgentEvent (P1 round-3 #2: producer backlog is HONESTLY bounded)", () => {
  it("a synchronous flood under permanently-stalled stdout stays bounded and terminates with a fatal error", async () => {
    const frames: KernelOutbound[] = [];
    const cap = 16;
    const runner = new PiKernelRunner({
      emit: (f) => frames.push(f),
      // stdout is dead: writer capacity NEVER frees, so the emit chain parks.
      whenWritable: () => new Promise<void>(() => {}),
      maxPendingAgentEvents: cap,
    });

    // Pi's `subscribe` listener is SYNCHRONOUS and fire-and-forget (its returned
    // promise is ignored): model a turn flooding events without awaiting.
    for (let i = 0; i < 100_000; i++) {
      void runner.forwardAgentEvent({ type: "tok", n: i } as unknown as AgentSessionEvent);
    }
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));

    // Memory stays bounded: buffered agent_events never exceed the cap (the old
    // code would have grown the promise chain to 100k closures).
    expect(runner.getPendingAgentEventCount()).toBeLessThanOrEqual(cap);

    // Nothing was silently dropped into the void: with stdout dead no agent_event
    // escaped, and the failure mode is EXPLICIT — a single fatal error + a single
    // terminal exit, never unbounded growth.
    expect(frames.filter((f) => f.type === "agent_event").length).toBe(0);
    expect(frames.some((f) => f.type === "error" && f.fatal === true)).toBe(true);
    expect(frames.filter((f) => f.type === "exit").length).toBe(1);
  });
});

describe("PiKernelRunner.shutdown (P1 round-3 #3: exit cannot race pending agent_events)", () => {
  it("flushes queued agent_events before exit; exit is the LAST frame; nothing emits after it", async () => {
    const frames: KernelOutbound[] = [];
    // Default whenWritable is always-writable, so the chain drains promptly.
    const runner = new PiKernelRunner({ emit: (f) => frames.push(f) });

    // Several agent_events enqueued via the fire-and-forget sync path, still
    // pending in the emit chain when shutdown is requested.
    for (let i = 0; i < 5; i++) {
      void runner.forwardAgentEvent({ type: "tok", n: i } as unknown as AgentSessionEvent);
    }

    await runner.shutdown(0);

    const types = frames.map((f) => f.type);
    // All five agent_events flushed, in order, BEFORE the terminal exit.
    expect(frames.filter((f) => f.type === "agent_event").length).toBe(5);
    expect(types[types.length - 1]).toBe("exit");
    expect(types.filter((t) => t === "exit").length).toBe(1);
    expect(types.lastIndexOf("agent_event")).toBeLessThan(types.indexOf("exit"));

    // Post-teardown emits are suppressed: no frame can appear after exit.
    const before = frames.length;
    await runner.forwardAgentEvent({ type: "late" } as unknown as AgentSessionEvent);
    expect(frames.length).toBe(before);
  });
});

describe("createSerialQueue (P1: sequential dispatch)", () => {
  it("runs tasks strictly in order, each settling before the next starts", async () => {
    const queue = createSerialQueue();
    const order: string[] = [];
    const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

    // First task is SLOW; second is fast. Without serialization the fast one
    // would finish first.
    void queue.run(async () => {
      await wait(30);
      order.push("a-end");
    });
    void queue.run(async () => {
      order.push("b-start");
      order.push("b-end");
    });
    await queue.run(async () => {
      order.push("c");
    });

    expect(order).toEqual(["a-end", "b-start", "b-end", "c"]);
  });

  it("a rejected task does not poison the lane; later tasks still run", async () => {
    const queue = createSerialQueue();
    const order: string[] = [];

    void queue.run(async () => {
      order.push("one");
      throw new Error("boom");
    }).catch(() => order.push("one-caught"));
    await queue.run(async () => {
      order.push("two");
    });

    expect(order).toEqual(["one", "one-caught", "two"]);
  });
});
