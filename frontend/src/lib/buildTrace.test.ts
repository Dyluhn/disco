/**
 * buildTrace selectors — unit tests.
 *
 * DC-03 — deriveActivity autoApproved derivation.
 * WALK-09 — deliverable gate (only shown at FINISHED).
 * WALK-16 — deriveFiles harvests observation (slides/sheet) + deliverable events.
 */

import { describe, expect, it } from "vitest";
import {
  deriveActivity,
  deriveBuildProgress,
  deriveDeliverable,
  deriveFiles,
  deriveLiveSignal,
  derivePlan,
  firstUserTask,
  plainError,
} from "@/lib/buildTrace";
import { serializeElementMention, stripElementMention } from "@/lib/elementMention";
import type { ElementMentionPayload } from "@/lib/elementMention";
import type { AgentEvent } from "@/types/agent";

function messageEvent(
  id: string,
  source: "user" | "agent" | "environment",
  content: string,
  role?: string,
): AgentEvent {
  return {
    kind: "message",
    id,
    source,
    message: { role: role ?? source, content },
  } as AgentEvent;
}

function actionEvent(
  id: string,
  toolName: string,
  meta?: Record<string, unknown>,
): AgentEvent {
  return {
    kind: "action",
    id,
    thought: "doing something",
    tool_call: {
      tool_name: toolName,
      arguments: { command: "echo hi" },
      call_id: id,
    },
    meta,
  } as AgentEvent;
}

function actionEventWithArgs(
  id: string,
  toolName: string,
  args: Record<string, unknown>,
): AgentEvent {
  return {
    kind: "action",
    id,
    thought: "doing something",
    tool_call: {
      tool_name: toolName,
      arguments: args,
      call_id: id,
    },
  } as AgentEvent;
}

function planCardEvent(
  id: string,
  summary: string,
  steps: Array<{ title: string; detail?: string | null }>,
  revision = 1,
): AgentEvent {
  return { kind: "plan", id, source: "agent", summary, steps, revision, context: "" } as AgentEvent;
}

const MENTION_PAYLOAD: ElementMentionPayload = {
  domPath: ["h1#hero-title", "section.hero"],
  screenLabel: "Hero",
  text: "Harborline Studio",
  rect: { x: 24, y: 199.75, w: 470.399, h: 52 },
};

describe("derivePlan — legacy no-steps placeholder is dropped", () => {
  it("filters the '(the planner returned no concrete steps)' sentinel so the card renders summary-only", () => {
    const plan = derivePlan([
      planCardEvent("p1", "Move the spawn off the intersection.", [
        { title: "(the planner returned no concrete steps)", detail: null },
      ]),
    ]);
    expect(plan).not.toBeNull();
    expect(plan!.steps).toEqual([]); // no broken numbered "1." step
    expect(plan!.summary).toBe("Move the spawn off the intersection.");
  });

  it("keeps real steps intact", () => {
    const plan = derivePlan([
      planCardEvent("p1", "Build it", [
        { title: "Scaffold", detail: "make files" },
        { title: "Wire it up" },
      ]),
    ]);
    expect(plan!.steps.map((s) => s.title)).toEqual(["Scaffold", "Wire it up"]);
  });

  it("highest revision wins", () => {
    const plan = derivePlan([
      planCardEvent("p1", "v1", [{ title: "old" }], 1),
      planCardEvent("p2", "v2", [{ title: "new" }], 2),
    ]);
    expect(plan!.summary).toBe("v2");
  });
});

describe("deriveActivity — autoApproved derivation (DC-03)", () => {
  it("sets autoApproved=true when meta.auto_approved === 'sandboxed'", () => {
    const events: AgentEvent[] = [
      actionEvent("act-1", "shell", { auto_approved: "sandboxed" }),
    ];
    const items = deriveActivity(events, null, "RUNNING");
    expect(items).toHaveLength(1);
    expect(items[0].autoApproved).toBe(true);
  });

  it("sets autoApproved falsy when meta.auto_approved is absent", () => {
    const events: AgentEvent[] = [actionEvent("act-2", "shell", {})];
    const items = deriveActivity(events, null, "RUNNING");
    expect(items[0].autoApproved).toBeFalsy();
  });

  it("sets autoApproved falsy when meta is absent entirely", () => {
    const events: AgentEvent[] = [actionEvent("act-3", "shell")];
    const items = deriveActivity(events, null, "RUNNING");
    expect(items[0].autoApproved).toBeFalsy();
  });

  it("sets autoApproved falsy when meta.auto_approved has a different value", () => {
    const events: AgentEvent[] = [
      actionEvent("act-4", "shell", { auto_approved: "other_value" }),
    ];
    const items = deriveActivity(events, null, "RUNNING");
    expect(items[0].autoApproved).toBeFalsy();
  });

  it("auto-approved items are never attention=true from autoApproved alone", () => {
    const events: AgentEvent[] = [
      actionEvent("act-5", "file_write", { auto_approved: "sandboxed" }),
    ];
    const items = deriveActivity(events, null, "FINISHED");
    const item = items[0];
    expect(item.autoApproved).toBe(true);
    // attention is driven by risk/pending/failed, not by autoApproved
    expect(item.attention).toBe(false);
  });
});

// ---- WALK-09: deliverable panel gate ----------------------------------------

function deliverableEvent(id: string, title: string, path: string, kind: "app" | "files"): AgentEvent {
  return {
    kind: "deliverable",
    id,
    title,
    path,
    artifact_kind: kind,
    source: "agent",
  } as AgentEvent;
}

describe("W-01: firstUserTask recovers the real task from the stream", () => {
  it("returns the first user message's text", () => {
    const events: AgentEvent[] = [
      messageEvent("m1", "user", "build me a snake game"),
      messageEvent("m2", "agent", "On it."),
      messageEvent("m3", "user", "make it blue"),
    ];
    expect(firstUserTask(events)).toBe("build me a snake game");
  });

  it("skips leading agent/environment messages to find the FIRST user turn", () => {
    const events: AgentEvent[] = [
      messageEvent("e0", "environment", "Session resumed"),
      messageEvent("a0", "agent", "thinking…"),
      messageEvent("u0", "user", "port this to TypeScript"),
    ];
    expect(firstUserTask(events)).toBe("port this to TypeScript");
  });

  it("matches a user turn tagged only by message.role (no source)", () => {
    const e = {
      kind: "message",
      id: "m1",
      message: { role: "user", content: "summarize the report" },
    } as AgentEvent;
    expect(firstUserTask([e])).toBe("summarize the report");
  });

  it("trims whitespace and ignores blank user messages", () => {
    const events: AgentEvent[] = [
      messageEvent("m0", "user", "   "),
      messageEvent("m1", "user", "  real task  "),
    ];
    expect(firstUserTask(events)).toBe("real task");
  });

  it("returns null when there is no user message (the caller falls back)", () => {
    const events: AgentEvent[] = [
      messageEvent("a0", "agent", "hello"),
      messageEvent("e0", "environment", "note"),
    ];
    expect(firstUserTask(events)).toBeNull();
    expect(firstUserTask([])).toBeNull();
  });
});

describe("element mentions — strip machine payloads from display text", () => {
  it("round-trips a serialized mention and returns the clean user text", () => {
    const message = `${serializeElementMention(MENTION_PAYLOAD)}\nMake the headline brighter`;
    expect(stripElementMention(message)).toEqual({
      clean: "Make the headline brighter",
      mention: { tag: "h1", text: "Harborline Studio", screen: "Hero" },
    });
  });

  it("treats a malformed unclosed mention as a consumed block", () => {
    const parsed = stripElementMention("<mentioned-element>\ndom: button.cta\ntext: Buy now");
    expect(parsed.clean).toBe("");
    expect(parsed.mention).toEqual({ tag: "button", text: "Buy now", screen: null });
  });

  it("labels user activity with clean text and keeps the mention chip data", () => {
    const items = deriveActivity(
      [messageEvent("u1", "user", `${serializeElementMention(MENTION_PAYLOAD)}\nChange this copy`)],
      null,
      "RUNNING",
    );
    expect(items).toHaveLength(1);
    expect(items[0].label).toBe("Change this copy");
    expect(items[0].mention).toEqual({ tag: "h1", text: "Harborline Studio" });
  });

  it("falls back to a plain pointed-at label when no user words follow", () => {
    const items = deriveActivity(
      [messageEvent("u1", "user", serializeElementMention(MENTION_PAYLOAD))],
      null,
      "RUNNING",
    );
    expect(items[0].label).toBe("Pointed at <h1>");
    expect(items[0].mention).toEqual({ tag: "h1", text: "Harborline Studio" });
  });

  it("uses cleaned user text in the live reading preview", () => {
    const signal = deriveLiveSignal(
      [messageEvent("u1", "user", `${serializeElementMention(MENTION_PAYLOAD)}\nChange this copy`)],
      "RUNNING",
    );
    expect(signal).toEqual({ kind: "thinking_about_user_message", preview: "Change this copy" });
  });

  it("uses cleaned user text for the resumed task fallback", () => {
    expect(
      firstUserTask([
        messageEvent("u1", "user", `${serializeElementMention(MENTION_PAYLOAD)}\nChange this copy`),
      ]),
    ).toBe("Change this copy");
  });
});

describe("deriveActivity — plain tool labels", () => {
  it("covers common build tools without falling back to raw tool names", () => {
    const items = deriveActivity(
      [
        actionEventWithArgs("preview", "preview_start", {}),
        actionEventWithArgs("slides", "slides_generate", {}),
        actionEventWithArgs("image", "image_generate", { prompt: "short prompt" }),
        actionEventWithArgs("safe-new", "safe_write_file", { path: "new.tsx" }),
        actionEventWithArgs("safe-edit", "safe_write_file", {
          path: "app.tsx",
          expected_sha256: "abc",
        }),
      ],
      null,
      "RUNNING",
    );
    expect(items.map((i) => i.label)).toEqual([
      "Started the preview server",
      "Generated a slide deck",
      'Generated an image: "short prompt"',
      "Wrote new.tsx",
      "Edited app.tsx",
    ]);
  });

  it("ellipsizes image prompts only when the prompt is actually truncated", () => {
    const longPrompt = "abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz";
    const items = deriveActivity(
      [actionEventWithArgs("image", "image_generate", { prompt: longPrompt })],
      null,
      "RUNNING",
    );
    expect(items[0].label).toBe(`Generated an image: "${longPrompt.slice(0, 48)}…"`);
  });

  it("attaches a plain first line for known failed action codes", () => {
    const events: AgentEvent[] = [
      actionEventWithArgs("edit", "file_edit", { path: "index.html" }),
      {
        kind: "agent_error",
        id: "err",
        action_id: "edit",
        error: "old_text_not_found",
      } as AgentEvent,
    ];
    const items = deriveActivity(events, null, "RUNNING");
    expect(items[0].status).toBe("failed");
    expect(items[0].expandable?.plainError).toBe(
      "An edit missed — retrying with fresh file contents",
    );
    expect(items[0].expandable?.error).toBe("old_text_not_found");
  });
});

describe("plainError", () => {
  it("maps known codes and backend phrase forms to plain language", () => {
    expect(plainError("old_text_not_found")).toBe(
      "An edit missed — retrying with fresh file contents",
    );
    expect(plainError("unknown or out-of-scope tool 'foo'; available: []")).toBe(
      "That tool wasn't available here",
    );
    expect(plainError("SandboxFileNotFoundError: missing.txt")).toBe("That file wasn't there");
  });

  it("returns null for unknown errors", () => {
    expect(plainError("something surprising happened")).toBeNull();
  });
});

describe("WALK-09: deliverable gate — panel only shows at FINISHED", () => {
  it("deriveDeliverable returns non-null the moment a DeliverableEvent exists (ungated derivation)", () => {
    // The `serve` tool emits a DeliverableEvent mid-run (before the run ends).
    // deriveDeliverable finds it immediately — it has no status gate.
    const events: AgentEvent[] = [deliverableEvent("d-mid", "Draft App", "dist", "app")];
    expect(deriveDeliverable(events)).not.toBeNull();
  });

  it("the FINISHED gate (b.status === 'FINISHED' ? deliverable : null) suppresses mid-run panels", () => {
    // This mirrors the conditional in BuildSurface.tsx:
    //   deliverable={b.status === "FINISHED" ? deliverable : null}
    // Before WALK-09 the raw `deliverable` was passed directly → panel appeared mid-run.
    const d = deriveDeliverable([deliverableEvent("d-mid", "App", "dist", "app")]);
    expect(d).not.toBeNull(); // derivation is non-null (unchanged behaviour)

    // RUNNING → gate returns null → panel is hidden
    const runningResult = ("RUNNING" satisfies string) === "FINISHED" ? d : null;
    expect(runningResult).toBeNull();

    // FINISHED → gate passes through → panel is visible
    const finishedResult = ("FINISHED" satisfies string) === "FINISHED" ? d : null;
    expect(finishedResult).not.toBeNull();
  });
});

// ---- WALK-16: deriveFiles — observation + deliverable sources ---------------

function observationEvent(
  id: string,
  toolName: string,
  structured: Record<string, unknown>,
  success = true,
): AgentEvent {
  return {
    kind: "observation",
    id,
    action_id: `act-${id}`,
    tool_result: {
      tool_name: toolName,
      success,
      content: "",
      structured,
    },
  } as AgentEvent;
}

describe("WALK-16: deriveFiles — observation and deliverable artifact sources", () => {
  it("harvests slides_generate filename from observation structured output", () => {
    const events: AgentEvent[] = [
      observationEvent("obs-1", "slides_generate", { filename: "deck.html", format: "html", slide_count: 5 }),
    ];
    const files = deriveFiles(events);
    expect(files).toHaveLength(1);
    expect(files[0].path).toBe("deck.html");
  });

  it("harvests sheet_generate filename from observation structured output", () => {
    const events: AgentEvent[] = [
      observationEvent("obs-2", "sheet_generate", { filename: "report.xlsx", title: "Sales" }),
    ];
    const files = deriveFiles(events);
    expect(files).toHaveLength(1);
    expect(files[0].path).toBe("report.xlsx");
  });

  it("harvests path from deliverable events", () => {
    const events: AgentEvent[] = [
      deliverableEvent("d1", "Handoff", "handoff.zip", "files"),
    ];
    const files = deriveFiles(events);
    expect(files).toHaveLength(1);
    expect(files[0].path).toBe("handoff.zip");
  });

  it("does not double-count a path written by file_write AND seen in a tool observation", () => {
    // file_write action comes first; the observation must not overwrite it
    const events: AgentEvent[] = [
      {
        kind: "action",
        id: "act-w",
        thought: "writing",
        tool_call: {
          tool_name: "file_write",
          arguments: { path: "deck.html", content: "<!DOCTYPE html>" },
          call_id: "c-w",
        },
      } as AgentEvent,
      observationEvent("obs-w", "slides_generate", { filename: "deck.html", format: "html" }),
    ];
    const files = deriveFiles(events);
    expect(files).toHaveLength(1);
    // file_write content is preserved (observation does not overwrite)
    expect(files[0].content).toBe("<!DOCTYPE html>");
  });

  it("does not double-count a path that is both a deliverable and a written file", () => {
    const events: AgentEvent[] = [
      {
        kind: "action",
        id: "act-w",
        thought: "writing",
        tool_call: {
          tool_name: "file_write",
          arguments: { path: "output.zip", content: "" },
          call_id: "c-w",
        },
      } as AgentEvent,
      deliverableEvent("d1", "Output", "output.zip", "files"),
    ];
    expect(deriveFiles(events)).toHaveLength(1);
  });

  it("skips a failed slides_generate observation (success=false)", () => {
    const events: AgentEvent[] = [
      observationEvent("obs-f", "slides_generate", { filename: "deck.html" }, false),
    ];
    expect(deriveFiles(events)).toHaveLength(0);
  });

  it("preserves existing file_write behaviour (regression)", () => {
    const events: AgentEvent[] = [
      {
        kind: "action",
        id: "act-1",
        thought: "writing",
        tool_call: {
          tool_name: "file_write",
          arguments: { path: "fizzbuzz.py", content: "print('FizzBuzz')" },
          call_id: "c-1",
        },
      } as AgentEvent,
    ];
    const files = deriveFiles(events);
    expect(files).toHaveLength(1);
    expect(files[0].path).toBe("fizzbuzz.py");
    expect(files[0].content).toContain("FizzBuzz");
  });
});

// ---- F2: deliverable event → activity item conversion -----------------------

describe("F2: deriveActivity — deliverable events become file download cards", () => {
  it("converts deliverable(kind=files) to activity item with file expandable", () => {
    const events: AgentEvent[] = [
      deliverableEvent("d1", "Sales Report", "report.pdf", "files"),
    ];
    const items = deriveActivity(events, null, "FINISHED");
    // Should create an activity item
    const deliverableItem = items.find((i) => i.id === "d1");
    expect(deliverableItem).toBeDefined();
    expect(deliverableItem?.kind).toBe("action");
    expect(deliverableItem?.label).toBe("Delivered: Sales Report");
    expect(deliverableItem?.detail).toBe("report.pdf");
    expect(deliverableItem?.expandable?.file).toEqual({ filename: "report.pdf", title: "Sales Report" });
    expect(deliverableItem?.status).toBe("done");
    expect(deliverableItem?.attention).toBe(false);
  });

  it("skips deliverable(kind=app) from activity feed (opens live URL instead)", () => {
    const events: AgentEvent[] = [
      deliverableEvent("d2", "My App", "dist", "app"),
    ];
    const items = deriveActivity(events, null, "FINISHED");
    // App deliverables don't create activity items (they open live URLs)
    const deliverableItem = items.find((i) => i.id === "d2");
    expect(deliverableItem).toBeUndefined();
  });

  it("multiple deliverable events all appear in activity feed", () => {
    const events: AgentEvent[] = [
      deliverableEvent("d1", "Report", "report.pdf", "files"),
      deliverableEvent("d2", "Data", "data.csv", "files"),
    ];
    const items = deriveActivity(events, null, "FINISHED");
    const deliverableItems = items.filter((i) => i.expandable?.file);
    expect(deliverableItems).toHaveLength(2);
  });
});

// runthru-v2 (#3): the declarative capable-model progress derivation.
function planEvent(revision: number, nSteps: number): AgentEvent {
  return {
    kind: "plan",
    id: `plan-${revision}`,
    revision,
    summary: "p",
    context: "",
    steps: Array.from({ length: nSteps }, (_, i) => ({ title: `step ${i + 1}` })),
  } as unknown as AgentEvent;
}
function progressEvent(id: string, steps: Array<{ index: number; state: string }>): AgentEvent {
  return {
    kind: "action",
    id,
    thought: "",
    tool_call: { tool_name: "update_plan_progress", arguments: { steps }, call_id: id },
  } as AgentEvent;
}

describe("deriveBuildProgress — declarative full-state snapshot (#3)", () => {
  it("returns an empty map when there is no snapshot (→ UI shows the status chip)", () => {
    const events = [planEvent(1, 3), actionEvent("a1", "shell")];
    expect(deriveBuildProgress(events, "RUNNING").size).toBe(0);
  });

  it("takes the LATEST snapshot as the whole truth — self-correcting, not cumulative", () => {
    const events = [
      planEvent(1, 3),
      // an earlier, wrong snapshot...
      progressEvent("p1", [
        { index: 1, state: "active" },
        { index: 2, state: "pending" },
        { index: 3, state: "pending" },
      ]),
      // ...fully superseded by the latest one (a missed/garbled prior update self-heals).
      progressEvent("p2", [
        { index: 1, state: "done" },
        { index: 2, state: "done" },
        { index: 3, state: "active" },
      ]),
    ];
    const p = deriveBuildProgress(events, "RUNNING");
    expect(p.get(1)).toBe("done");
    expect(p.get(2)).toBe("done");
    expect(p.get(3)).toBe("active");
  });

  it("ignores snapshots from a superseded plan revision (re-plan starts fresh)", () => {
    const events = [
      planEvent(1, 2),
      progressEvent("p1", [
        { index: 1, state: "done" },
        { index: 2, state: "done" },
      ]),
      planEvent(2, 2), // re-plan — the prior snapshot must not satisfy the new checklist
    ];
    expect(deriveBuildProgress(events, "RUNNING").size).toBe(0);
  });

  it("on FINISHED, ALL plan steps read done — a stale partial snapshot can't show 2/3", () => {
    // model sent an early snapshot (only step 1 done) then finished without a final 100%.
    const events = [planEvent(1, 3), progressEvent("p1", [{ index: 1, state: "done" }])];
    const p = deriveBuildProgress(events, "FINISHED");
    expect(p.get(1)).toBe("done");
    expect(p.get(2)).toBe("done");
    expect(p.get(3)).toBe("done"); // not left "pending" → no "2/3 with no Done" lie
  });

  it("#3 small-model (Qwen): NO snapshot at all + FINISHED → every step reads done", () => {
    // The NL-done-at-finish tier (local Qwen) NEVER calls update_plan_progress. A
    // finished build is still complete, so all steps must check off. This was the live
    // bug: the no-snapshot early-return returned an empty map and skipped terminal
    // reconciliation, so a finished Qwen build showed every step unchecked.
    const p = deriveBuildProgress([planEvent(1, 4)], "FINISHED");
    expect(p.size).toBe(4);
    expect([1, 2, 3, 4].every((i) => p.get(i) === "done")).toBe(true);
  });

  it("#3 small-model: NO snapshot + still RUNNING → empty (no premature checks)", () => {
    // The fix must NOT leak into the running state — only a real FINISHED reconciles.
    expect(deriveBuildProgress([planEvent(1, 4)], "RUNNING").size).toBe(0);
  });

  it("drops out-of-range / malformed indices (no lying checklist from a bad snapshot)", () => {
    const events = [
      planEvent(1, 3),
      progressEvent("p1", [
        { index: 99, state: "done" }, // > nSteps
        { index: 0, state: "done" }, // < 1
      ]),
    ];
    // both dropped → empty map → BuildSurface falls back to the honest status chip,
    // NOT a "1/3" checklist with every real step pending.
    expect(deriveBuildProgress(events, "RUNNING").size).toBe(0);
  });

  it("on STUCK, a lingering active step reads as stalled (honest about halted work)", () => {
    const events = [
      planEvent(1, 1),
      progressEvent("p1", [{ index: 1, state: "active" }]),
    ];
    expect(deriveBuildProgress(events, "STUCK").get(1)).toBe("stalled");
  });
});

describe("deriveActivity — W-14/W-28: no 'Listed .' workspace-root stub", () => {
  function fileListEvent(id: string, path: unknown): AgentEvent {
    return {
      kind: "action",
      id,
      thought: "",
      tool_call: { tool_name: "file_list", arguments: { path }, call_id: id },
    } as AgentEvent;
  }

  it.each([".", "./", "", "/", undefined])(
    "suppresses a workspace-root file_list (path=%p) so nothing renders before real content",
    (path) => {
      const items = deriveActivity([fileListEvent("ls-1", path)], null, "RUNNING");
      expect(items).toHaveLength(0);
      // and never the bare "Listed ." stub
      expect(items.some((i) => /^Listed \.?$/.test(i.label))).toBe(false);
    },
  );

  it("the root list is suppressed but later real work still renders", () => {
    const events: AgentEvent[] = [
      fileListEvent("ls-1", "."),
      {
        kind: "action",
        id: "w-1",
        thought: "",
        tool_call: { tool_name: "file_write", arguments: { path: "index.html" }, call_id: "w-1" },
      } as AgentEvent,
    ];
    const items = deriveActivity(events, null, "RUNNING");
    expect(items).toHaveLength(1);
    expect(items[0].label).toBe("Wrote index.html");
  });

  it("a real SUBDIRECTORY file_list still renders (not noise)", () => {
    const items = deriveActivity([fileListEvent("ls-2", "src")], null, "RUNNING");
    expect(items).toHaveLength(1);
    expect(items[0].label).toBe("Listed src");
  });
});
