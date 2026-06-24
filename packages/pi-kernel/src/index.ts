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
 */
import { createInterface } from "node:readline";

import { PiKernelRunner } from "./runner.ts";
import { encodeOutbound, parseCommand, type KernelOutbound } from "./protocol.ts";

function writeFrame(event: KernelOutbound): void {
  process.stdout.write(encodeOutbound(event));
}

async function main(): Promise<void> {
  let exited = false;
  const runner = new PiKernelRunner({
    emit: writeFrame,
    onExit: (code) => {
      exited = true;
      // Allow the final `exit` frame to flush before tearing the process down.
      process.stdout.write("", () => process.exit(code));
    },
  });

  const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });

  rl.on("line", (line) => {
    const trimmed = line.trim();
    if (trimmed === "") return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(trimmed);
    } catch {
      writeFrame({ type: "error", message: "malformed JSON line" });
      return;
    }
    const command = parseCommand(parsed);
    if (!command) {
      writeFrame({ type: "error", message: "unrecognized command" });
      return;
    }
    // Commands are dispatched sequentially; a rejection here would be a runner
    // bug (it catches its own errors), so surface it rather than swallow.
    void runner.handleCommand(command).catch((err: unknown) => {
      writeFrame({
        type: "error",
        message: `internal error: ${err instanceof Error ? err.message : String(err)}`,
      });
    });
  });

  // EOF on stdin → graceful shutdown.
  rl.on("close", () => {
    if (!exited) void runner.shutdown(0);
  });

  for (const signal of ["SIGINT", "SIGTERM"] as const) {
    process.on(signal, () => {
      if (!exited) void runner.shutdown(0);
    });
  }
}

main().catch((err: unknown) => {
  process.stderr.write(`pi-kernel fatal: ${err instanceof Error ? err.stack ?? err.message : String(err)}\n`);
  process.exit(1);
});
