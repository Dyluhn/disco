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
 * Hardening (codex review, rounds 1 & 2): the stdio layer is built to survive a
 * hostile or slow peer without crashing, deadlocking, or growing memory without
 * bound —
 *  - inbound lines are byte-capped (over-cap frames are rejected, not parsed);
 *  - command dispatch runs on TWO planes (round-2 P1 #1):
 *      • DATA plane  — init/prompt/followup/approve/reject are serialized so each
 *        fully resolves before the next begins (init really completes before a
 *        back-to-back prompt runs);
 *      • CONTROL plane — cancel / stdin-EOF / SIGINT / SIGTERM PREEMPT the data
 *        plane: they abort in-flight work immediately, bypassing the serial
 *        queue, so a hung `prompt()` can never wedge teardown. Signals also arm
 *        a hard force-exit deadline so a stuck teardown still terminates.
 *  - outbound writes respect backpressure (await `drain`), coalesce heartbeats
 *    under pressure, and are BOUNDED (round-2 P1 #2): when the queue reaches its
 *    cap the producer is told to back off (the inbound source is paused) instead
 *    of growing memory — but `agent_event` / `ready` are never dropped;
 *  - producer-side backpressure for the in-flight prompt (round-3 P1): a stalled
 *    stdout cannot pause an already-running `session.prompt()`, and Pi's
 *    `subscribe` listener is synchronous, so the runner forwards each
 *    `agent_event` only after AWAITING writer capacity (`whenWritable`) — frames
 *    park at the producer, before reaching the queue, so the queue stays at/under
 *    its cap regardless of how fast the turn emits;
 *  - only TERMINAL frames are cap-exempt (a reserved flush path so teardown can
 *    drain): the final `exit` and FATAL `error`s. NON-fatal diagnostic `error`s
 *    (malformed input, "prompt failed") are cap-ELIGIBLE and coalesce
 *    consecutive duplicates, so a hostile peer cannot OOM the queue via error
 *    frames either.
 *
 * The transport pieces below are exported so they can be unit-tested against
 * fake streams; `main()` only auto-runs when this module is the process entry.
 */
import { once } from "node:events";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { Writable } from "node:stream";

import { PiKernelRunner } from "./runner.ts";
import {
  encodeOutbound,
  parseCommand,
  type KernelCommand,
  type KernelOutbound,
} from "./protocol.ts";

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

/**
 * Default cap on the number of frames buffered for outbound delivery before the
 * producer is asked to back off. A stalled reader can park at most this many
 * data frames in memory; beyond it we apply UPSTREAM backpressure (pause the
 * inbound source AND, via `whenWritable`, the agent_event producer) rather than
 * growing the queue without bound. Only TERMINAL frames are cap-exempt (reserved
 * flush path so teardown always drains): the final `exit` and FATAL `error`s.
 */
export const DEFAULT_MAX_OUTBOUND_QUEUE = 1024;

export interface OutboundWriter {
  /**
   * Enqueue a frame for ordered, backpressure-respecting delivery. Returns
   * `false` once the bounded queue is at/over its cap — a cooperating producer
   * MUST then pause and await {@link OutboundWriter.whenWritable} before writing
   * more data frames. Only the terminal `exit` and FATAL `error`s are cap-exempt
   * (always accepted); NON-fatal diagnostic `error`s are cap-eligible.
   */
  write: (frame: KernelOutbound) => boolean;
  /** Resolves when the queue has drained back below the cap (capacity is free). */
  whenWritable: () => Promise<void>;
  /** Resolves once every queued frame has been flushed to the stream. */
  flushed: () => Promise<void>;
}

export interface OutboundWriterOptions {
  /** Max buffered data frames before upstream backpressure engages. */
  maxQueue?: number;
  /**
   * Called when the bounded queue crosses into (`true`) / out of (`false`) the
   * over-cap state. Wire this to pause/resume the upstream source (stdin) so no
   * fresh work is pulled while the consumer is stalled.
   */
  onBackpressure?: (active: boolean) => void;
}

/**
 * Wrap a writable stream so protocol frames are delivered with backpressure
 * respected AND the in-memory buffer is bounded:
 *  - when `stream.write()` returns false we await `'drain'` before the next
 *    write, so the OS-level writable buffer can never grow without bound;
 *  - the in-process queue is capped at `maxQueue` frames. When it fills, `write`
 *    returns `false` and `onBackpressure(true)` fires so the producer (and the
 *    inbound source) stops — memory stays bounded instead of accumulating
 *    `agent_event` frames behind a stalled reader. No `agent_event` / `ready`
 *    is ever DROPPED; the producer is BLOCKED instead;
 *  - `heartbeat` frames coalesce under pressure (only the freshest is kept), and
 *    consecutive duplicate NON-fatal `error` frames coalesce too (a hostile peer
 *    spamming malformed input cannot grow the queue);
 *  - only the terminal `exit` and FATAL `error`s are cap-EXEMPT (a reserved
 *    flush path) so the terminal `exit` can always be enqueued and drained on
 *    teardown even while the data queue is saturated. NON-fatal diagnostic
 *    `error`s are cap-ELIGIBLE — they engage backpressure like any data frame.
 */
export function createOutboundWriter(
  stream: Writable,
  options: OutboundWriterOptions = {},
): OutboundWriter {
  const maxQueue = Math.max(1, Math.floor(options.maxQueue ?? DEFAULT_MAX_OUTBOUND_QUEUE));
  const onBackpressure = options.onBackpressure;
  const queue: KernelOutbound[] = [];
  let pumping = false;
  let backpressured = false;
  let idleResolvers: Array<() => void> = [];
  let writableResolvers: Array<() => void> = [];

  // ONLY terminal teardown frames bypass the cap and are guaranteed to flush:
  //  - the final `exit`;
  //  - FATAL errors (e.g. init failure, which is immediately followed by
  //    shutdown + `exit`).
  // Non-fatal diagnostic errors (malformed input, "prompt failed", overflow)
  // are deliberately NOT reserved: they count toward the cap (engaging upstream
  // backpressure, which pauses stdin) and consecutive duplicates coalesce, so a
  // hostile peer cannot OOM the queue with a flood of `error` frames.
  function isReservedFrame(frame: KernelOutbound): boolean {
    return frame.type === "exit" || (frame.type === "error" && frame.fatal === true);
  }

  function notifyIdle(): void {
    if (idleResolvers.length === 0) return;
    const resolvers = idleResolvers;
    idleResolvers = [];
    for (const resolveIdle of resolvers) resolveIdle();
  }

  function notifyWritable(): void {
    if (writableResolvers.length === 0) return;
    const resolvers = writableResolvers;
    writableResolvers = [];
    for (const resolveWritable of resolvers) resolveWritable();
  }

  function setBackpressure(active: boolean): void {
    if (active === backpressured) return;
    backpressured = active;
    onBackpressure?.(active);
    if (!active) notifyWritable();
  }

  async function pump(): Promise<void> {
    if (pumping) return;
    pumping = true;
    try {
      while (queue.length > 0) {
        const frame = queue.shift()!;
        const ok = stream.write(encodeOutbound(frame));
        // Draining below the cap relieves upstream backpressure.
        if (queue.length < maxQueue) setBackpressure(false);
        if (!ok) {
          // Slow reader: wait for the OS buffer to drain before writing more.
          await once(stream, "drain");
        }
      }
    } finally {
      pumping = false;
    }
    if (queue.length === 0) {
      setBackpressure(false);
      notifyIdle();
    }
  }

  return {
    write(frame) {
      if (frame.type === "heartbeat") {
        // Coalesce: replace an already-queued (stale) heartbeat in place rather
        // than letting liveness pings pile up behind a slow reader. Position is
        // preserved, so no agent_event is reordered or dropped, and the queue
        // does not grow.
        const pending = queue.findIndex((f) => f.type === "heartbeat");
        if (pending >= 0) {
          queue[pending] = frame;
          void pump();
          return !backpressured;
        }
      }
      if (frame.type === "error" && !isReservedFrame(frame)) {
        // Coalesce a flood of identical NON-fatal diagnostic errors (e.g. a
        // hostile peer spamming malformed lines → repeated "malformed JSON
        // line"): collapse consecutive duplicates so they can't grow the queue.
        // Distinct errors still enqueue and count toward the cap.
        const tail = queue[queue.length - 1];
        if (
          tail !== undefined &&
          tail.type === "error" &&
          tail.message === frame.message &&
          (tail.fatal ?? false) === (frame.fatal ?? false)
        ) {
          void pump();
          return !backpressured;
        }
      }
      queue.push(frame);
      void pump();
      // Engage producer backpressure once the (cap-eligible) queue is full.
      // Reserved frames never trip the cap, so a final error/exit always lands.
      if (!isReservedFrame(frame) && queue.length >= maxQueue) {
        setBackpressure(true);
      }
      return !backpressured;
    },
    whenWritable() {
      if (!backpressured) return Promise.resolve();
      return new Promise<void>((resolveWritable) => writableResolvers.push(resolveWritable));
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
 * A single-lane async queue for the DATA plane. Each task starts only after the
 * previous one fully settles (resolve OR reject), so command handling is strictly
 * sequential and in arrival order — `init` completes before a back-to-back
 * `prompt` begins. NOTE: teardown/`cancel` deliberately do NOT ride this queue
 * (see {@link createControlPlane}); they must preempt a possibly-hung in-flight
 * data task rather than serialize behind it.
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
// Control plane: preemptive cancel / teardown (round-2 P1 #1)
// ---------------------------------------------------------------------------

/** Default grace before a stuck teardown is forced to exit (ms). */
export const DEFAULT_FORCE_EXIT_MS = 3000;

/** The runner surface the control plane drives. */
export interface ControlTarget {
  /** DATA-plane command handling (init/prompt/followup/approve/reject). */
  handleCommand: (command: KernelCommand) => Promise<void>;
  /** CONTROL-plane preempt: abort in-flight work WITHOUT tearing the process down. */
  cancel: () => Promise<void>;
  /** CONTROL-plane teardown: abort + dispose + emit the terminal `exit`. */
  shutdown: (code: number) => Promise<void>;
}

export interface ControlPlane {
  /** Route one inbound command onto the data plane, or preempt for `cancel`. */
  dispatch: (command: KernelCommand) => void;
  /** stdin EOF → preemptive graceful teardown. */
  eof: () => void;
  /** SIGINT / SIGTERM → preemptive teardown with a hard force-exit deadline. */
  signal: () => void;
}

export interface ControlPlaneOptions {
  /** The data-plane serial queue (ordering is preserved on this lane only). */
  queue: SerialQueue;
  target: ControlTarget;
  /** Surface a non-fatal control-plane failure (e.g. cancel threw). */
  onError: (message: string) => void;
  /** Grace before a stuck teardown is forced to exit. */
  forceExitMs?: number;
  /** Hard process-exit seam (injected in tests). Defaults to `process.exit`. */
  forceExit?: (code: number) => void;
}

/**
 * Split command handling into two planes so a hung in-flight `prompt()` can never
 * wedge cancellation or shutdown:
 *
 *  - DATA plane: `init` / `prompt` / `followup` / `approve` / `reject` run on the
 *    serial queue, preserving arrival order (init still completes before a
 *    back-to-back prompt).
 *  - CONTROL plane: `cancel`, stdin EOF and SIGINT/SIGTERM PREEMPT — they invoke
 *    `target.cancel()` / `target.shutdown()` DIRECTLY, bypassing the serial
 *    queue, even while a data task is in flight. Because `cancel`/`shutdown`
 *    abort the session, they are exactly what unblocks a stuck prompt. Signals
 *    additionally arm a hard {@link ControlPlaneOptions.forceExitMs} deadline: if
 *    graceful teardown (or the final flush) stalls, the process is force-exited.
 */
export function createControlPlane(options: ControlPlaneOptions): ControlPlane {
  const { queue, target, onError } = options;
  const forceExitMs = options.forceExitMs ?? DEFAULT_FORCE_EXIT_MS;
  const forceExit = options.forceExit ?? ((code: number) => process.exit(code));
  let tearingDown = false;

  function describe(err: unknown): string {
    return err instanceof Error ? err.message : String(err);
  }

  function teardown(code: number): void {
    if (tearingDown) return;
    tearingDown = true;
    // Hard deadline covering the WHOLE teardown (abort + dispose + final flush):
    // if any of it stalls — a hung abort, or a permanently stalled stdout reader
    // that blocks the `exit` flush — force the process to exit anyway. Unref'd so
    // it never by itself keeps the loop alive.
    const deadline = setTimeout(() => forceExit(code), Math.max(0, forceExitMs));
    deadline.unref?.();
    // Preempt: tear down DIRECTLY, never behind the serial queue, so a hung
    // in-flight prompt cannot block shutdown. The graceful path exits the process
    // via the runner's onExit (flush → process.exit); the deadline is the
    // backstop if that path stalls.
    void Promise.resolve()
      .then(() => target.shutdown(code))
      .catch((err) => {
        onError(`shutdown failed: ${describe(err)}`);
        forceExit(code);
      });
  }

  return {
    dispatch(command) {
      if (tearingDown) return;
      if (command.type === "cancel") {
        // CONTROL plane: abort in-flight work immediately, bypassing the data
        // queue, so a hung prompt is unblocked without waiting for it to settle.
        void Promise.resolve()
          .then(() => target.cancel())
          .catch((err) => onError(`cancel failed: ${describe(err)}`));
        return;
      }
      // DATA plane: ordered + serialized.
      void queue.run(async () => {
        try {
          await target.handleCommand(command);
        } catch (err) {
          onError(`internal error: ${describe(err)}`);
        }
      });
    },
    eof() {
      teardown(0);
    },
    signal() {
      teardown(0);
    },
  };
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

export async function main(): Promise<void> {
  let exited = false;
  const writer = createOutboundWriter(process.stdout, {
    // Upstream backpressure: when the outbound queue saturates behind a stalled
    // reader, pause the inbound source so no fresh command (and therefore no
    // fresh agent turn) is pulled; resume once the queue drains below the cap.
    onBackpressure: (active) => {
      if (active) process.stdin.pause();
      else process.stdin.resume();
    },
  });
  const queue = createSerialQueue();

  const runner = new PiKernelRunner({
    emit: (event) => writer.write(event),
    // Producer-side backpressure: an in-flight prompt's agent_events park here
    // until the outbound queue has capacity, so a stalled stdout pauses event
    // PRODUCTION instead of growing the queue without bound (round-3 P1).
    whenWritable: () => writer.whenWritable(),
    onExit: (code) => {
      exited = true;
      // Flush every queued frame (incl. the final `exit`) before tearing down.
      void writer.flushed().then(() => {
        process.stdout.write("", () => process.exit(code));
      });
    },
  });

  const control = createControlPlane({
    queue,
    target: {
      handleCommand: (command) => runner.handleCommand(command),
      // Preemptive cancel: abort the in-flight turn but keep the sidecar alive.
      cancel: () => runner.cancel(),
      // Idempotent w.r.t. an already-completed exit.
      shutdown: (code) => (exited ? Promise.resolve() : runner.shutdown(code)),
    },
    onError: (message) => writer.write({ type: "error", message }),
  });

  function onLine(line: string): void {
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
    // Route onto the data plane, or preempt for `cancel` — see createControlPlane.
    control.dispatch(command);
  }

  const feed = createLineReader(
    {
      onLine,
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

  // EOF on stdin → preemptive graceful teardown (bypasses the data queue).
  process.stdin.on("end", () => control.eof());

  // SIGINT/SIGTERM → preemptive teardown with a hard force-exit deadline, so a
  // stuck prompt/teardown can never make the signal ineffective.
  for (const signal of ["SIGINT", "SIGTERM"] as const) {
    process.on(signal, () => control.signal());
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
