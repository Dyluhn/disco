/**
 * Lifecycle integration tests (EPIC B / PR B1 acceptance): spawn the REAL sidecar
 * as a subprocess and drive it over stdio. Covers:
 *  - init → ready → heartbeat → followup → agent_event → EOF → clean exit
 *  - prompt with no model wired surfaces a non-fatal `error` (no crash)
 *  - cancel mid-run aborts the turn but keeps the sidecar ALIVE (resumable)
 *  - malformed / unrecognized stdin lines produce `error` frames, never a crash
 *
 * The sidecar runs via Node's native TypeScript execution (`node src/index.ts`)
 * so the test needs no prior build.
 */
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import type { KernelOutbound } from "../src/protocol.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const ENTRY = resolve(HERE, "../src/index.ts");

interface Sidecar {
  child: ChildProcessWithoutNullStreams;
  frames: KernelOutbound[];
  send: (line: object) => void;
  sendRaw: (line: string) => void;
  waitFor: (predicate: (f: KernelOutbound) => boolean, timeoutMs?: number) => Promise<KernelOutbound>;
  exitCode: () => Promise<number | null>;
}

function startSidecar(): Sidecar {
  const child = spawn(process.execPath, [ENTRY], {
    stdio: ["pipe", "pipe", "pipe"],
  }) as ChildProcessWithoutNullStreams;

  const frames: KernelOutbound[] = [];
  const waiters: Array<{ predicate: (f: KernelOutbound) => boolean; resolve: (f: KernelOutbound) => void }> = [];
  let buffer = "";

  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk: string) => {
    buffer += chunk;
    let idx: number;
    while ((idx = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 1);
      if (line === "") continue;
      const frame = JSON.parse(line) as KernelOutbound;
      frames.push(frame);
      for (let i = waiters.length - 1; i >= 0; i--) {
        const w = waiters[i]!;
        if (w.predicate(frame)) {
          waiters.splice(i, 1);
          w.resolve(frame);
        }
      }
    }
  });

  // Surface sidecar stderr for debugging without polluting the frame stream.
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk: string) => {
    process.stderr.write(`[sidecar stderr] ${chunk}`);
  });

  return {
    child,
    frames,
    send: (line) => child.stdin.write(JSON.stringify(line) + "\n"),
    sendRaw: (line) => child.stdin.write(line + "\n"),
    waitFor: (predicate, timeoutMs = 10_000) =>
      new Promise<KernelOutbound>((resolveP, reject) => {
        const existing = frames.find(predicate);
        if (existing) return resolveP(existing);
        const timer = setTimeout(() => {
          reject(new Error(`timed out waiting for frame; saw: ${JSON.stringify(frames)}`));
        }, timeoutMs);
        waiters.push({
          predicate,
          resolve: (f) => {
            clearTimeout(timer);
            resolveP(f);
          },
        });
      }),
    exitCode: () =>
      new Promise<number | null>((resolveP) => {
        if (child.exitCode !== null) return resolveP(child.exitCode);
        child.on("exit", (code) => resolveP(code));
      }),
  };
}

const delay = (ms: number) => new Promise((r) => setTimeout(r, ms));

describe("pi-kernel sidecar lifecycle", () => {
  let sidecar: Sidecar | undefined;

  afterEach(() => {
    if (sidecar && sidecar.child.exitCode === null) {
      sidecar.child.kill("SIGKILL");
    }
    sidecar = undefined;
  });

  it("init → ready → heartbeat → followup → agent_event → EOF → clean exit", async () => {
    sidecar = startSidecar();

    sidecar.send({ type: "init", config: { heartbeatMs: 50 } });

    const ready = await sidecar.waitFor((f) => f.type === "ready");
    expect(ready.type).toBe("ready");
    if (ready.type === "ready") {
      expect(ready.protocolVersion).toBe(1);
      expect(typeof ready.piVersion).toBe("string");
      expect(ready.piVersion.length).toBeGreaterThan(0);
      // No real model is wired until EPIC C (the "unknown" sentinel → null).
      expect(ready.model).toBeNull();
      // No built-in and no custom tools.
      expect(ready.tools.activeToolNames).toEqual([]);
      expect(ready.tools.customToolCount).toBe(0);
      expect(ready.tools.noTools).toBe("all");
    }

    const heartbeat = await sidecar.waitFor((f) => f.type === "heartbeat");
    expect(heartbeat.type).toBe("heartbeat");
    if (heartbeat.type === "heartbeat") {
      expect(typeof heartbeat.ts).toBe("number");
    }

    // A real Pi session event flows out as an `agent_event`. `followup` queues a
    // message and the session emits a `queue_update` — deterministic without a
    // live model (which lands in EPIC C).
    sidecar.send({ type: "followup", text: "please continue" });
    const evt = await sidecar.waitFor((f) => f.type === "agent_event");
    expect(evt.type).toBe("agent_event");
    if (evt.type === "agent_event") {
      expect(typeof evt.event.kind).toBe("string");
      expect(evt.event.kind).toBe("queue_update");
    }

    // Termination is via stdin EOF (NOT cancel).
    sidecar.child.stdin.end();

    const exit = await sidecar.waitFor((f) => f.type === "exit");
    expect(exit.type).toBe("exit");
    if (exit.type === "exit") {
      expect(exit.code).toBe(0);
    }

    const code = await sidecar.exitCode();
    expect(code).toBe(0);
  });

  it("prompt with no model wired surfaces a non-fatal error and stays alive", async () => {
    sidecar = startSidecar();
    sidecar.send({ type: "init", config: { heartbeatMs: 50 } });
    await sidecar.waitFor((f) => f.type === "ready");

    sidecar.send({ type: "prompt", text: "build me an app" });

    const error = await sidecar.waitFor((f) => f.type === "error");
    expect(error.type).toBe("error");
    if (error.type === "error") {
      expect(error.message).toContain("prompt failed");
      expect(error.fatal ?? false).toBe(false);
    }

    // The sidecar is still alive after a failed prompt: heartbeats continue.
    await delay(150);
    const hbCount = sidecar.frames.filter((f) => f.type === "heartbeat").length;
    expect(hbCount).toBeGreaterThan(0);
    expect(sidecar.child.exitCode).toBeNull();
  });

  it("cancel mid-run aborts the turn but keeps the sidecar alive (resumable)", async () => {
    sidecar = startSidecar();
    sidecar.send({ type: "init", config: { heartbeatMs: 50 } });
    await sidecar.waitFor((f) => f.type === "ready");

    // Queue some work, then cancel it.
    sidecar.send({ type: "followup", text: "work to abort" });
    await sidecar.waitFor((f) => f.type === "agent_event");
    sidecar.send({ type: "cancel" });

    // cancel must NOT terminate the process: no exit, still heartbeating.
    await delay(300);
    expect(sidecar.frames.some((f) => f.type === "exit")).toBe(false);
    expect(sidecar.child.exitCode).toBeNull();
    await sidecar.waitFor((f) => f.type === "heartbeat", 5_000);

    // It remains drivable: a follow-up after cancel still produces an event.
    sidecar.send({ type: "followup", text: "resume after cancel" });
    const evt = await sidecar.waitFor(
      (f) => f.type === "agent_event" && f.event.kind === "queue_update",
    );
    expect(evt.type).toBe("agent_event");

    // Clean teardown via EOF.
    sidecar.child.stdin.end();
    const exit = await sidecar.waitFor((f) => f.type === "exit");
    expect(exit.type).toBe("exit");
    expect(await sidecar.exitCode()).toBe(0);
  });

  it("survives malformed / unrecognized stdin without crashing", async () => {
    sidecar = startSidecar();
    sidecar.send({ type: "init", config: { heartbeatMs: 50 } });
    await sidecar.waitFor((f) => f.type === "ready");

    // 1. Not JSON at all.
    sidecar.sendRaw("this is not json {");
    const e1 = await sidecar.waitFor((f) => f.type === "error");
    expect(e1.type === "error" && e1.message).toContain("malformed JSON");

    // 2. Valid JSON, but not a recognized command.
    sidecar.send({ type: "bogus", foo: 1 });
    const e2 = await sidecar.waitFor(
      (f) => f.type === "error" && f.message.includes("unrecognized"),
    );
    expect(e2.type).toBe("error");

    // 3. Recognized type but wrong shape (prompt without text).
    sidecar.send({ type: "prompt" });
    await sidecar.waitFor(
      (f) => f.type === "error" && f.message.includes("unrecognized"),
    );

    // 4. Blank line is ignored entirely (no frame).
    sidecar.sendRaw("");

    // The sidecar is unharmed: it keeps heartbeating and accepts a real command.
    await sidecar.waitFor((f) => f.type === "heartbeat", 5_000);
    sidecar.send({ type: "followup", text: "still working" });
    await sidecar.waitFor((f) => f.type === "agent_event");
    expect(sidecar.child.exitCode).toBeNull();

    sidecar.child.stdin.end();
    expect(await sidecar.exitCode()).toBe(0);
  });

  it("shuts down cleanly on stdin EOF", async () => {
    sidecar = startSidecar();
    sidecar.send({ type: "init", config: { heartbeatMs: 50 } });
    await sidecar.waitFor((f) => f.type === "ready");

    // Closing stdin (EOF) should trigger a graceful shutdown.
    sidecar.child.stdin.end();

    const exit = await sidecar.waitFor((f) => f.type === "exit");
    expect(exit.type).toBe("exit");
    const code = await sidecar.exitCode();
    expect(code).toBe(0);
  });
});
