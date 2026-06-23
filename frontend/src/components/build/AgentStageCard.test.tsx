/**
 * AgentStageCard (W-43) — the collapsed-chat stage view.
 *
 * Covers:
 *  1. deriveStage maps live signal + status onto the coarse stage labels.
 *  2. The card shows the stage label and is collapsed by default.
 *  3. Click-to-expand reveals the FULL ActivityFeed inline (codex P1: artifact /
 *     download affordances stay reachable, not hidden).
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AgentStageCard, deriveStage } from "@/components/build/AgentStageCard";
import type { ActivityItem } from "@/lib/buildTrace";
import type { AgentEvent } from "@/types/agent";

const action = (tool: string, args: Record<string, unknown> = {}): AgentEvent =>
  ({ id: `a-${tool}-${Math.random()}`, kind: "action", thought: "", tool_call: { tool_name: tool, arguments: args } }) as AgentEvent;

describe("deriveStage (W-43)", () => {
  it("FINISHED → done", () => {
    expect(deriveStage([], "FINISHED")).toBe("done");
  });
  it("STUCK / ERROR → stalled", () => {
    expect(deriveStage([], "STUCK")).toBe("stalled");
    expect(deriveStage([], "ERROR")).toBe("stalled");
  });
  it("a running file_write → building", () => {
    expect(deriveStage([action("file_write", { path: "a.html" })], "RUNNING")).toBe("building");
  });
  it("a running shell → building", () => {
    expect(deriveStage([action("shell", { command: "ls" })], "RUNNING")).toBe("building");
  });
  it("a running browser tool → reading", () => {
    expect(deriveStage([action("browser", { url: "x" })], "RUNNING")).toBe("reading");
  });
  it("a fresh RUNNING run with no events → planning", () => {
    expect(deriveStage([], "RUNNING")).toBe("planning");
  });
  it("IDLE → idle", () => {
    expect(deriveStage([], "IDLE")).toBe("idle");
  });
});

describe("AgentStageCard", () => {
  const activity: ActivityItem[] = [
    { kind: "action", id: "x1", label: "Wrote report.html", status: "done", attention: false },
  ] as unknown as ActivityItem[];

  it("shows the stage label and is collapsed by default (feed not shown)", () => {
    render(<AgentStageCard events={[]} status="RUNNING" activity={activity} />);
    expect(screen.getByTestId("agent-stage-card")).toHaveAttribute("data-stage", "planning");
    expect(screen.getByText(/Planning/)).toBeInTheDocument();
    expect(screen.queryByText("Wrote report.html")).toBeNull();
  });

  it("click-to-expand reveals the full activity feed (artifacts preserved)", () => {
    render(<AgentStageCard events={[]} status="RUNNING" activity={activity} conversationId="c1" />);
    fireEvent.click(screen.getByRole("button", { name: /expand agent history/i }));
    expect(screen.getByText("Wrote report.html")).toBeInTheDocument();
  });
});
