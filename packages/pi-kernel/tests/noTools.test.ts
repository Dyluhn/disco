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
import { DISCO_TOOL_NAMES } from "../src/tools.ts";
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

    // G1: only the reviewed internal skills mount — no discovered project/user
    // skill survives the override (and no built-in tools regardless).
    const skills = opts!.resourceLoader!.getSkills().skills.map((s) => s.name).sort();
    expect(skills).toEqual(["build-basic", "preview-repair"]);

    // No error frames during a clean init.
    expect(frames.filter((f) => f.type === "error")).toEqual([]);
  });

  it("with a bridge, mounts the Disco custom tools and NO built-ins (noTools:'builtin')", async () => {
    const frames: KernelOutbound[] = [];
    runner = new PiKernelRunner({ emit: (e) => frames.push(e), heartbeatMs: 50 });

    await runner.handleCommand({
      type: "init",
      config: {
        gateway: {
          baseUrl: "http://127.0.0.1:8731/v1",
          model: "disco-build",
          apiKey: "loopback-run-token",
        },
        bridge: { baseUrl: "http://127.0.0.1:8731", kernelId: "k-abc" },
      },
    });

    // The ready frame reports the D1 tool surface: builtin-disabled + 14 custom.
    const ready = frames.find((f): f is ReadyEvent => f.type === "ready");
    expect(ready, "expected a ready frame").toBeDefined();
    expect(ready!.tools.noTools).toBe("builtin");
    expect(ready!.tools.customToolCount).toBe(14);
    // Active tools are exactly the Disco custom set, never a built-in.
    for (const builtin of BUILTIN_TOOLS) {
      expect(ready!.tools.activeToolNames, `built-in '${builtin}' must not be active`).not.toContain(
        builtin,
      );
    }
    expect([...ready!.tools.activeToolNames].sort()).toEqual([...DISCO_TOOL_NAMES].sort());

    // The exact options handed to createAgentSession.
    const opts = runner.getInitOptions();
    expect(opts!.noTools).toBe("builtin");
    expect(opts!.customTools).toHaveLength(14);

    // The live session's ACTIVE tools are exactly the 14 Disco custom tools.
    // Under `noTools:"builtin"` the built-ins stay registered but INACTIVE
    // (disabled) — so they can never be called — while the custom set is live.
    const session = runner.getSession();
    const activeNames = session!.getActiveToolNames();
    for (const builtin of BUILTIN_TOOLS) {
      expect(activeNames, `built-in '${builtin}' must not be active`).not.toContain(builtin);
    }
    for (const name of DISCO_TOOL_NAMES) {
      expect(activeNames, `custom tool '${name}' must be active`).toContain(name);
    }
    expect([...activeNames].sort()).toEqual([...DISCO_TOOL_NAMES].sort());

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
