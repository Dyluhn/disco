#!/usr/bin/env node
/**
 * Entry point for the Disco Pi Build Kernel sidecar (EPIC B / PR B1).
 *
 * Wires the transport-agnostic {@link PiKernelRunner} to real stdio:
 * - inbound: newline-delimited JSON commands on stdin → parsed → dispatched.
 * - outbound: every runner frame is written as one JSON line on stdout.
 *
 * stdout carries ONLY protocol frames; diagnostics go to stderr so the process
 * manager's parser never trips. EOF on stdin and SIGINT/SIGTERM both trigger a
 * clean shutdown.
 *
 * Hardening (codex review): the stdio layer is built to survive a hostile or
 * slow peer without crashing or growing memory without bound —
 *  - inbound lines are byte-capped (over-cap frames are rejected, not parsed);
 *  - command dispatch is serialized so each command fully resolves before the
 *    next begins (init really completes before a back-to-back prompt runs);
 *  - outbound writes respect backpressure (await `drain`) and coalesce
 *    heartbeats under pressure, but NEVER drop `agent_event` frames.
 *
 * The transport pieces below are exported so they can be unit-tested against
 * fake streams; `main()` only auto-runs when this module is the process entry.
 */
import { once } from "node:events";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { Writable } from "node:stream";

import { PiKernelRunner } from "./runner.ts";
import { encodeOutbound, parseCommand, type KernelOutbound } from "./protocol.ts";

/**
 * Maximum bytes for a single inbound JSON line. A peer cannot drive unbounded
 * memory growth with a giant or unterminated frame: once the in-flight line
 * exceeds this cap we reject it (protocol `error`) without parsing and resync on
 * the next newline. 4 MiB comfortably covers any legitimate command while
 * keeping the worst-case inbound buffer small. Overridable for tests via
 * `DISCO_PI_KERNEL_MAX_LINE_BYTES`.
 */
export const MAX_LINE_BYTES = 4 * 1024 * 1024;

function resolveMaxLineBytes(): number {
  const raw = process.env.DISCO_PI_KERNEL_MAX_LINE_BYTES;
  if (raw === undefined) return MAX_LINE_BYTES;
  const parsed = Number(raw);
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : MAX_LINE_BYTES;
}

// ---------------------------------------------------------------------------
// Inbound: byte-capped line reader
// ---------------------------------------------------------------------------

export interface LineReaderHandlers {
  /** A complete, in-cap line (newline stripped, NOT trimmed). */
  onLine: (line: string) => void;
  /** A line exceeded the byte cap; it was discarded without parsing. */
  onOverflow: (byteLength: number) => void;
}

/**
 * Build a chunk-fed line splitter that enforces a max byte cap per line. Feed it
 * UTF-8 string chunks (set the stream's encoding to "utf8" first). When the
 * current line exceeds `maxBytes` the reader invokes `onOverflow`, drops the
 * oversized bytes, and resyncs at the next newline — it never buffers or parses
 * the oversized payload, so a giant/unterminated frame cannot grow memory.
 */
export function createLineReader(
  handlers: LineReaderHandlers,
  maxBytes: number = MAX_LINE_BYTES,
): (chunk: string) => void {
  let buffer = "";
  let bufferBytes = 0;
  // True while we drop the tail of an over-cap line until its terminating "\n".
  let discarding = false;

  return function feed(chunk: string): void {
    let rest = chunk;
    while (rest.length > 0) {
      const nl = rest.indexOf("\n");

      if (nl < 0) {
        // No line terminator in this chunk.
        if (discarding) return; // still dropping an over-cap line
        const segBytes = Buffer.byteLength(rest, "utf8");
        if (bufferBytes + segBytes > maxBytes) {
          handlers.onOverflow(bufferBytes + segBytes);
          buffer = "";
          bufferBytes = 0;
          discarding = true; // drop everything until the next newline
          return;
        }
        buffer += rest;
        bufferBytes += segBytes;
        return;
      }

      const seg = rest.slice(0, nl);
      rest = rest.slice(nl + 1);

      if (discarding) {
        // This newline ends the discarded line; resume normal parsing after it.
        discarding = false;
        continue;
      }

      const segBytes = Buffer.byteLength(seg, "utf8");
      if (bufferBytes + segBytes > maxBytes) {
        // Over-cap but already fully delimited by this newline: reject + resync.
        handlers.onOverflow(bufferBytes + segBytes);
        buffer = "";
        bufferBytes = 0;
        continue;
      }

      const line = buffer + seg;
      buffer = "";
      bufferBytes = 0;
      handlers.onLine(line);
    }
  };
}

// ---------------------------------------------------------------------------
// Outbound: backpressure-aware writer
// ---------------------------------------------------------------------------

export interface OutboundWriter {
  /** Enqueue a frame for ordered, backpressure-respecting delivery. */
  write: (frame: KernelOutbound) => void;
  /** Resolves once every queued frame has been flushed to the stream. */
  flushed: () => Promise<void>;
}

/**
 * Wrap a writable stream so protocol frames are delivered with backpressure
 * respected: when `stream.write()` returns false we await `'drain'` before the
 * next write, so a slow reader can never let the writable buffer grow without
 * bound. Under pressure, queued `heartbeat` frames coalesce (only the freshest
 * liveness ping is kept) — but `agent_event` / `ready` / `error` / `exit` frames
 * are ALWAYS preserved, in order.
 */
export function createOutboundWriter(stream: Writable): OutboundWriter {
  const queue: KernelOutbound[] = [];
  let pumping = false;
  let idleResolvers: Array<() => void> = [];

  function notifyIdle(): void {
    if (idleResolvers.length === 0) return;
    const resolvers = idleResolvers;
    idleResolvers = [];
    for (const resolveIdle of resolvers) resolveIdle();
  }

  async function pump(): Promise<void> {
    if (pumping) return;
    pumping = true;
    try {
      while (queue.length > 0) {
        const frame = queue.shift()!;
        const ok = stream.write(encodeOutbound(frame));
        if (!ok) {
          // Slow reader: wait for the OS buffer to drain before writing more.
          await once(stream, "drain");
        }
      }
    } finally {
      pumping = false;
    }
    if (queue.length === 0) notifyIdle();
  }

  return {
    write(frame) {
      if (frame.type === "heartbeat") {
        // Coalesce: replace an already-queued (stale) heartbeat in place rather
        // than letting liveness pings pile up behind a slow reader. Position is
        // preserved, so no agent_event is reordered or dropped.
        const pending = queue.findIndex((f) => f.type === "heartbeat");
        if (pending >= 0) {
          queue[pending] = frame;
          void pump();
          return;
        }
      }
      queue.push(frame);
      void pump();
    },
    flushed() {
      if (queue.length === 0 && !pumping) return Promise.resolve();
      return new Promise<void>((resolveIdle) => idleResolvers.push(resolveIdle));
    },
  };
}

// ---------------------------------------------------------------------------
// Dispatch: strict FIFO serialization
// ---------------------------------------------------------------------------

export interface SerialQueue {
  /** Run `task` after all previously enqueued tasks settle; preserves order. */
  run: (task: () => Promise<void>) => Promise<void>;
}

/**
 * A single-lane async queue. Each task starts only after the previous one fully
 * settles (resolve OR reject), so command handling is strictly sequential and in
 * arrival order — `init` completes before a back-to-back `prompt` begins, and
 * `cancel` / EOF teardown is ordered with respect to in-flight work.
 */
export function createSerialQueue(): SerialQueue {
  let tail: Promise<void> = Promise.resolve();
  return {
    run(task) {
      const next = tail.then(task, task);
      // Don't let one task's rejection poison the lane for the next task.
      tail = next.then(
        () => undefined,
        () => undefined,
      );
      return next;
    },
  };
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

export async function main(): Promise<void> {
  let exited = false;
  const writer = createOutboundWriter(process.stdout);
  const queue = createSerialQueue();

  const runner = new PiKernelRunner({
    emit: (event) => writer.write(event),
    onExit: (code) => {
      exited = true;
      // Flush every queued frame (incl. the final `exit`) before tearing down.
      void writer.flushed().then(() => {
        process.stdout.write("", () => process.exit(code));
      });
    },
  });

  async function processLine(line: string): Promise<void> {
    const trimmed = line.trim();
    if (trimmed === "") return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(trimmed);
    } catch {
      writer.write({ type: "error", message: "malformed JSON line" });
      return;
    }
    const command = parseCommand(parsed);
    if (!command) {
      writer.write({ type: "error", message: "unrecognized command" });
      return;
    }
    try {
      await runner.handleCommand(command);
    } catch (err: unknown) {
      writer.write({
        type: "error",
        message: `internal error: ${err instanceof Error ? err.message : String(err)}`,
      });
    }
  }

  const feed = createLineReader(
    {
      // Each line is serialized through the queue so dispatch is sequential and
      // ordered even under burst input.
      onLine: (line) => void queue.run(() => processLine(line)),
      onOverflow: (byteLength) =>
        writer.write({
          type: "error",
          message: `inbound line exceeds ${resolveMaxLineBytes()} byte cap (${byteLength} bytes); frame discarded`,
        }),
    },
    resolveMaxLineBytes(),
  );

  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk: string) => feed(chunk));

  // EOF on stdin → graceful shutdown, ordered AFTER any in-flight commands.
  process.stdin.on("end", () => {
    void queue.run(() => (exited ? Promise.resolve() : runner.shutdown(0)));
  });

  for (const signal of ["SIGINT", "SIGTERM"] as const) {
    process.on(signal, () => {
      void queue.run(() => (exited ? Promise.resolve() : runner.shutdown(0)));
    });
  }
}

/** True when this module is the process entry point (vs. imported by a test). */
function isProcessEntry(): boolean {
  const argv1 = process.argv[1];
  if (!argv1) return false;
  try {
    return resolve(argv1) === fileURLToPath(import.meta.url);
  } catch {
    return false;
  }
}

if (isProcessEntry()) {
  main().catch((err: unknown) => {
    process.stderr.write(
      `pi-kernel fatal: ${err instanceof Error ? (err.stack ?? err.message) : String(err)}\n`,
    );
    process.exit(1);
  });
}
