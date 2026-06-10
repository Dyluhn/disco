/**
 * BP-13 — AgentStatusBar suspended badge: shows when sandboxState === "suspended",
 * hidden otherwise.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AgentStatusBar } from "@/components/build/AgentStatusBar";
import type { IsolationInfo } from "@/types/agent";

const ISO: IsolationInfo = {
  tier: "gvisor",
  label: "gVisor (hardware sandbox)",
  adversarialSafe: true,
};

describe("AgentStatusBar — suspended sandbox badge (BP-13)", () => {
  it("shows 'suspended' pill when sandboxState is suspended", () => {
    render(
      <AgentStatusBar
        status="FINISHED"
        isolation={ISO}
        sandboxState="suspended"
        onKill={() => {}}
      />,
    );
    expect(screen.getByTitle(/sandbox suspended/i)).toBeInTheDocument();
    expect(screen.getByText("suspended")).toBeInTheDocument();
  });

  it("does NOT show the suspended pill when sandboxState is active", () => {
    render(
      <AgentStatusBar
        status="FINISHED"
        isolation={ISO}
        sandboxState="active"
        onKill={() => {}}
      />,
    );
    expect(screen.queryByText("suspended")).not.toBeInTheDocument();
  });

  it("does NOT show the suspended pill when sandboxState is undefined", () => {
    render(
      <AgentStatusBar
        status="FINISHED"
        isolation={ISO}
        onKill={() => {}}
      />,
    );
    expect(screen.queryByText("suspended")).not.toBeInTheDocument();
  });

  it("shows the isolation tier regardless of sandboxState", () => {
    render(
      <AgentStatusBar
        status="PAUSED"
        isolation={ISO}
        sandboxState="suspended"
        onKill={() => {}}
      />,
    );
    expect(screen.getByTitle("gVisor (hardware sandbox)")).toBeInTheDocument();
    expect(screen.getByText("suspended")).toBeInTheDocument();
  });
});
