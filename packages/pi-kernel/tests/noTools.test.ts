/**
 * No-tools posture test (EPIC B / PR B1, §2.2 / §5.1):
 * drive the runner in-process and assert the Pi session was created with
 * `noTools: "all"`, zero custom tools, and that NO built-in tool (read / bash /
 * edit / write) is registered or active on the session.
 *
 * In-process (not subprocess) so we can introspect both the exact options passed
 * to `createAgentSession` and the live session's tool registry.
 */
import { afterEach, describe, expect, it } from "vitest";

import { PiKernelRunner } from "../src/runner.ts";
import type { KernelOutbound, ReadyEvent } from "../src/protocol.ts";

const BUILTIN_TOOLS = ["read", "bash", "edit", "write"];

describe("pi-kernel no-tools posture", () => {
  let runner: PiKernelRunner | undefined;

  afterEach(async () => {
    if (runner) await runner.shutdown(0);
    runner = undefined;
  });

  it("creates the session with noTools:'all' and zero tools", async () => {
    const frames: KernelOutbound[] = [];
    runner = new PiKernelRunner({ emit: (e) => frames.push(e), heartbeatMs: 50 });

    await runner.handleCommand({ type: "init" });

    // The ready frame reports the no-tools surface.
    const ready = frames.find((f): f is ReadyEvent => f.type === "ready");
    expect(ready, "expected a ready frame").toBeDefined();
    expect(ready!.tools.noTools).toBe("all");
    expect(ready!.tools.activeToolNames).toEqual([]);
    expect(ready!.tools.customToolCount).toBe(0);

    // The exact options handed to createAgentSession.
    const opts = runner.getInitOptions();
    expect(opts, "expected init options to be recorded").toBeDefined();
    expect(opts!.noTools).toBe("all");
    expect(opts!.customTools).toEqual([]);
    // Project-local resources are never discovered: we own the loader and
    // pinned project trust to false.
    expect(opts!.resourceLoader).toBeDefined();

    // The live session must expose no active tools and no built-ins at all.
    const session = runner.getSession();
    expect(session, "expected a live session").toBeDefined();
    expect(session!.getActiveToolNames()).toEqual([]);

    const allToolNames = session!.getAllTools().map((t) => t.name);
    for (const builtin of BUILTIN_TOOLS) {
      expect(allToolNames, `built-in '${builtin}' must not be registered`).not.toContain(builtin);
    }

    // No error frames during a clean init.
    expect(frames.filter((f) => f.type === "error")).toEqual([]);
  });

  it("rejects a duplicate init without starting a second session", async () => {
    const frames: KernelOutbound[] = [];
    runner = new PiKernelRunner({ emit: (e) => frames.push(e), heartbeatMs: 50 });

    await runner.handleCommand({ type: "init" });
    const firstSession = runner.getSession();
    await runner.handleCommand({ type: "init" });

    // Same session instance; the duplicate init produced an error frame.
    expect(runner.getSession()).toBe(firstSession);
    expect(frames.some((f) => f.type === "error")).toBe(true);
  });
});
