import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ActivityFeed } from "@/components/build/ActivityFeed";
import { deriveActivity } from "@/lib/buildTrace";
import type { AgentEvent } from "@/types/agent";

function workspaceRestored(seq: number): AgentEvent {
  return {
    id: `restore-${seq}`,
    kind: "workspace_restored",
    version_seq: seq,
    tree_digest: `digest-${seq}`,
    label: `v${seq}`,
  } as AgentEvent;
}

describe("ActivityFeed — workspace rollback marker", () => {
  it("renders workspace_restored as a compact marker chip, not a message row", () => {
    const items = deriveActivity([workspaceRestored(7)], null, "FINISHED");

    expect(items).toEqual([
      expect.objectContaining({
        kind: "rollback_marker",
        label: "↩ rolled back to v7",
      }),
    ]);

    render(<ActivityFeed items={items} />);
    expect(screen.getByText("↩ rolled back to v7")).toBeInTheDocument();
    expect(screen.queryByText("Note")).not.toBeInTheDocument();
    expect(screen.queryByText("Agent")).not.toBeInTheDocument();
  });
});
