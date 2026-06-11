/**
 * DC-03 — buildTrace.deriveActivity autoApproved derivation.
 *
 * autoApproved: true when e.meta?.auto_approved === "sandboxed";
 * false/absent otherwise. The badge is informational, never attention: true.
 */

import { describe, expect, it } from "vitest";
import { deriveActivity } from "@/lib/buildTrace";
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
