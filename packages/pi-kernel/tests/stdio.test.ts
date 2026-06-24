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
import type { KernelOutbound } from "../src/protocol.ts";

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
