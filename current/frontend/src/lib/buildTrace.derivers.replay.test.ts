/**
 * Gap #17 — REPLAY coverage for the Build INSPECTOR derivers that previously had
 * none: `deriveFiles`, `deriveTerminal`, `deriveDeliverable`.
 *
 * The existing `buildTrace.replay.test.ts` (W9) covers only the progress /
 * activity / live-signal derivers. The three derivers exercised here turn the
 * raw event stream into the workspace Files tab, the Terminal tab, and the
 * deliverable handoff — the user-facing OUTPUT views. They are pure functions, so
 * the test's INPUT is a hand-minimized event-stream fixture and the assertion is
 * on the real derived OUTPUT — no mocking of internals (the W9 discipline).
 *
 * REGRESSION net only (memory: feedback-live-model-proves-works): a green run
 * proves the wiring from events → views didn't break, NOT that a live build
 * actually produces these files.
 *
 * Run: npx vitest run src/lib/buildTrace.derivers.replay.test.ts
 */

import { describe, expect, it } from "vitest";
import type { AgentEvent } from "@/types/agent";
import { deriveFiles, deriveTerminal, deriveDeliverable } from "@/lib/buildTrace";

/** Cast a minimized fixture array to the real AgentEvent type. Fixtures may omit
 *  optional EventBase fields — the derivers must tolerate sparse inputs. */
function asEvents(events: unknown[]): AgentEvent[] {
  return events as unknown as AgentEvent[];
}

// ---------------------------------------------------------------------------
// deriveFiles — union of file_write / file_append / file_edit + sandbox-tool
// observations + serve deliverables, latest-content-wins.
// ---------------------------------------------------------------------------

describe("deriveFiles — workspace file reconstruction from the event stream", () => {
  it("file_write captures content + byte length; latest write wins per path", () => {
    const events = asEvents([
      {
        kind: "action",
        id: "a1",
        thought: "",
        tool_call: { tool_name: "file_write", arguments: { path: "index.html", content: "<h1>hi</h1>" } },
      },
      {
        kind: "action",
        id: "a2",
        thought: "",
        tool_call: { tool_name: "file_write", arguments: { path: "index.html", content: "<h1>bye</h1>" } },
      },
    ]);
    const files = deriveFiles(events);
    expect(files).toHaveLength(1);
    expect(files[0]?.path).toBe("index.html");
    expect(files[0]?.content).toBe("<h1>bye</h1>"); // latest write wins
    expect(files[0]?.bytes).toBe(new TextEncoder().encode("<h1>bye</h1>").length);
  });

  it("file_append accumulates onto prior content", () => {
    const events = asEvents([
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "file_write", arguments: { path: "log.txt", content: "line1\n" } } },
      { kind: "action", id: "a2", thought: "", tool_call: { tool_name: "file_append", arguments: { path: "log.txt", content: "line2\n" } } },
    ]);
    const files = deriveFiles(events);
    expect(files[0]?.content).toBe("line1\nline2\n");
  });

  it("file_edit mirrors an old→new replacement on a known file", () => {
    const events = asEvents([
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "file_write", arguments: { path: "app.py", content: "x = 1" } } },
      { kind: "action", id: "a2", thought: "", tool_call: { tool_name: "file_edit", arguments: { path: "app.py", old: "1", new: "2" } } },
    ]);
    const files = deriveFiles(events);
    expect(files[0]?.content).toBe("x = 2");
  });

  it("a successful slides_generate observation adds a server-side file (empty content)", () => {
    const events = asEvents([
      {
        kind: "observation",
        id: "o1",
        action_id: "a1",
        tool_result: { tool_name: "slides_generate", success: true, content: "", structured: { filename: "deck.pptx" } },
      },
    ]);
    const files = deriveFiles(events);
    expect(files.map((f) => f.path)).toContain("deck.pptx");
    expect(files.find((f) => f.path === "deck.pptx")?.content).toBe(""); // content is server-side only
  });

  it("a failed sandbox-tool observation does NOT add a file", () => {
    const events = asEvents([
      {
        kind: "observation",
        id: "o1",
        action_id: "a1",
        tool_result: { tool_name: "sheet_generate", success: false, content: "", structured: { filename: "nope.xlsx" } },
      },
    ]);
    expect(deriveFiles(events)).toHaveLength(0);
  });

  it("a serve deliverable adds its path when not already captured by a write", () => {
    const events = asEvents([
      { kind: "deliverable", id: "d1", title: "Report", path: "out/report.md", artifact_kind: "files" },
    ]);
    const files = deriveFiles(events);
    expect(files.map((f) => f.path)).toContain("out/report.md");
  });

  it("a file_write entry is NOT clobbered by a same-path deliverable (richer content kept)", () => {
    const events = asEvents([
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "file_write", arguments: { path: "report.md", content: "# real" } } },
      { kind: "deliverable", id: "d1", title: "Report", path: "report.md", artifact_kind: "files" },
    ]);
    const files = deriveFiles(events);
    expect(files).toHaveLength(1);
    expect(files[0]?.content).toBe("# real");
  });
});

// ---------------------------------------------------------------------------
// deriveTerminal — shell / code_exec commands paired with their observed output.
// ---------------------------------------------------------------------------

describe("deriveTerminal — shell + code_exec output reconstruction", () => {
  it("pairs a shell command with its observation output + success flag", () => {
    const events = asEvents([
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "shell", arguments: { command: "ls -la" } } },
      { kind: "observation", id: "o1", action_id: "a1", tool_result: { tool_name: "shell", success: true, content: "file1\nfile2" } },
    ]);
    const term = deriveTerminal(events);
    expect(term).toHaveLength(1);
    expect(term[0]?.command).toBe("ls -la");
    expect(term[0]?.output).toBe("file1\nfile2");
    expect(term[0]?.success).toBe(true);
    expect(term[0]?.running).toBe(false);
  });

  it("an action with no observation is marked running", () => {
    const events = asEvents([
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "shell", arguments: { command: "sleep 10" } } },
    ]);
    const term = deriveTerminal(events);
    expect(term[0]?.running).toBe(true);
    expect(term[0]?.output).toBe("");
  });

  it("an agent_error on the action surfaces as failed output", () => {
    const events = asEvents([
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "shell", arguments: { command: "boom" } } },
      { kind: "agent_error", id: "e1", action_id: "a1", error: "command not found: boom" },
    ]);
    const term = deriveTerminal(events);
    expect(term[0]?.success).toBe(false);
    expect(term[0]?.output).toBe("command not found: boom");
  });

  it("code_exec is rendered with a «code» command label", () => {
    const events = asEvents([
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "code_exec", arguments: { language: "python" } } },
      { kind: "observation", id: "o1", action_id: "a1", tool_result: { tool_name: "code_exec", success: true, content: "42" } },
    ]);
    const term = deriveTerminal(events);
    expect(term[0]?.command).toContain("python");
    expect(term[0]?.output).toBe("42");
  });

  it("non-terminal tools (file_write) never appear in the terminal view", () => {
    const events = asEvents([
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "file_write", arguments: { path: "x.txt", content: "y" } } },
    ]);
    expect(deriveTerminal(events)).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// deriveDeliverable — the latest finished-artifact handoff (serve), newest wins.
// ---------------------------------------------------------------------------

describe("deriveDeliverable — latest serve() handoff", () => {
  it("returns null before any deliverable (no false affordance)", () => {
    const events = asEvents([
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "file_write", arguments: { path: "a", content: "b" } } },
    ]);
    expect(deriveDeliverable(events)).toBeNull();
  });

  it("the NEWEST deliverable supersedes an earlier one", () => {
    const events = asEvents([
      { kind: "deliverable", id: "d1", title: "v1", path: "site-v1", artifact_kind: "app", deployment_url: "http://a" },
      { kind: "deliverable", id: "d2", title: "v2", path: "site-v2", artifact_kind: "app", deployment_url: "http://b" },
    ]);
    const d = deriveDeliverable(events);
    expect(d?.id).toBe("d2");
    expect(d?.title).toBe("v2");
    expect(d?.deploymentUrl).toBe("http://b");
  });

  it("carries artifact_kind + an empty deployment_url collapses to undefined", () => {
    const events = asEvents([
      { kind: "deliverable", id: "d1", title: "Files", path: "out.zip", artifact_kind: "files", deployment_url: "" },
    ]);
    const d = deriveDeliverable(events);
    expect(d?.kind).toBe("files");
    expect(d?.deploymentUrl).toBeUndefined();
  });
});
