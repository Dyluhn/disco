import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AgentCanvas } from "@/components/build/AgentCanvas";
import type { AgentEvent } from "@/types/agent";

const fileWrite = (path: string, content: string): AgentEvent =>
  ({ id: `w-${path}`, kind: "action", thought: "", tool_call: { tool_name: "file_write", arguments: { path, content } } }) as AgentEvent;

describe("AgentCanvas — the operator inspector", () => {
  it("renders Browser / Artifacts / Console tabs (not the build IDE's Files/Terminal/Preview)", () => {
    render(<AgentCanvas events={[]} status="RUNNING" cid="c1" />);
    expect(screen.getByRole("tab", { name: /browser/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /artifacts/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /console/i })).toBeInTheDocument();
    // no IDE-flavored tabs
    expect(screen.queryByRole("tab", { name: /^files$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /preview/i })).not.toBeInTheDocument();
  });

  it("defaults to Browser with an honest empty state when nothing has run", () => {
    render(<AgentCanvas events={[]} status="RUNNING" cid="c1" />);
    expect(screen.getByRole("tab", { name: /browser/i })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText(/When the agent opens a browser/i)).toBeInTheDocument();
  });

  it("defaults to Artifacts when files exist but no screenshot", () => {
    render(<AgentCanvas events={[fileWrite("notes.md", "# hi")]} status="FINISHED" cid="c1" />);
    expect(screen.getByRole("tab", { name: /artifacts/i })).toHaveAttribute("aria-selected", "true");
  });
});
