import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AlternativesGate } from "./AlternativesGate";
import type { AlternativesEvent } from "@/types/agent";

function gate(options: AlternativesEvent["options"]): AlternativesEvent {
  return {
    kind: "alternatives",
    id: "evt-1",
    seq: 1,
    source: "agent",
    failed_action_id: "act-1",
    summary: "Pick a path",
    options,
  } as unknown as AlternativesEvent;
}

describe("AlternativesGate — label fallback (BW-03)", () => {
  it("renders a label-only option (no tool_name) as a real choice, with NO 'Will call' row", () => {
    render(
      <AlternativesGate
        alternatives={gate([
          { id: "a", title: "Refactor the parser", description: "", tool_name: "", arguments: {} },
        ])}
        onPick={() => {}}
      />,
    );
    expect(screen.getByText("Refactor the parser")).toBeInTheDocument();
    // No tool → no "Will call" affordance, and the CTA reads "Choose this".
    expect(screen.queryByTitle(/Will call/)).not.toBeInTheDocument();
    expect(screen.getByText("Choose this")).toBeInTheDocument();
  });

  it("falls back to the description when title is empty (never a label-less button)", () => {
    render(
      <AlternativesGate
        alternatives={gate([
          { id: "b", title: "", description: "Roll back the migration", tool_name: "", arguments: {} },
        ])}
        onPick={() => {}}
      />,
    );
    const button = screen.getByRole("button", { name: /Roll back the migration/ });
    expect(button).toBeInTheDocument();
  });

  it("still shows the 'Will call' tool row + 'Run this path' for a runnable option", () => {
    render(
      <AlternativesGate
        alternatives={gate([
          { id: "c", title: "Retry with ci", description: "Use the lockfile.", tool_name: "shell", arguments: {} },
        ])}
        onPick={() => {}}
      />,
    );
    expect(screen.getByText("Retry with ci")).toBeInTheDocument();
    expect(screen.getByTitle("Will call: shell")).toBeInTheDocument();
    expect(screen.getByText("Run this path")).toBeInTheDocument();
  });
});
