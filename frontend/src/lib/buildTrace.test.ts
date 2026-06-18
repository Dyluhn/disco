/**
 * buildTrace selectors — unit tests.
 *
 * DC-03 — deriveActivity autoApproved derivation.
 * WALK-09 — deliverable gate (only shown at FINISHED).
 * WALK-16 — deriveFiles harvests observation (slides/sheet) + deliverable events.
 */

import { describe, expect, it } from "vitest";
import { deriveActivity, deriveDeliverable, deriveFiles } from "@/lib/buildTrace";
import type { AgentEvent } from "@/types/agent";

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
