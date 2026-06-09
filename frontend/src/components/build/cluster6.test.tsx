/**
 * Cluster 6 — UI liveness: state-agnostic live signal, aggregate progress,
 * and the graceful Stop control.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { deriveLiveSignal, planProgressSummary } from "@/lib/buildTrace";
import type { StepState } from "@/lib/buildTrace";
import { AgentStatusBar } from "@/components/build/AgentStatusBar";
import type { IsolationInfo } from "@/types/agent";

const ISO: IsolationInfo = {
  tier: "local",
  label: "Local container",
  adversarialSafe: false,
};

describe("deriveLiveSignal — narrates gate/await states (not just RUNNING)", () => {
  it("returns waiting_for_you for confirmation/plan/decision states", () => {
    expect(deriveLiveSignal([], "WAITING_FOR_CONFIRMATION")).toEqual({
      kind: "waiting_for_you",
      label: expect.stringMatching(/approve/i),
    });
    expect(deriveLiveSignal([], "AWAITING_PLAN_APPROVAL").kind).toBe("waiting_for_you");
    expect(deriveLiveSignal([], "AWAITING_USER_DECISION").kind).toBe("waiting_for_you");
  });

  it("is idle only for genuinely terminal/inactive states", () => {
    expect(deriveLiveSignal([], "FINISHED")).toEqual({ kind: "idle" });
    expect(deriveLiveSignal([], "IDLE")).toEqual({ kind: "idle" });
  });

  it("still narrates RUNNING work", () => {
    expect(deriveLiveSignal([], "RUNNING").kind).toBe("starting");
  });
});

describe("planProgressSummary — aggregate done/total", () => {
  it("counts done steps and computes the fraction", () => {
    const progress = new Map<number, StepState>([
      [1, "done"],
      [2, "done"],
      [3, "active"],
    ]);
    const s = planProgressSummary(4, progress);
    expect(s.done).toBe(2);
    expect(s.total).toBe(4);
    expect(s.fraction).toBe(0.5);
  });

  it("handles an empty plan without dividing by zero", () => {
    expect(planProgressSummary(0, new Map())).toEqual({ done: 0, total: 0, fraction: 0 });
  });
});

describe("AgentStatusBar — graceful Stop control", () => {
  it("shows a Stop button wired to onStop while running", async () => {
    const onStop = vi.fn();
    const onKill = vi.fn();
    const user = userEvent.setup();
    render(
      <AgentStatusBar status="RUNNING" isolation={ISO} onKill={onKill} onStop={onStop} />,
    );
    const stop = screen.getByRole("button", { name: /stop the agent gracefully/i });
    await user.click(stop);
    expect(onStop).toHaveBeenCalledTimes(1);
    expect(onKill).not.toHaveBeenCalled();
  });

  it("hides Stop in terminal states (nothing to stop)", () => {
    render(<AgentStatusBar status="FINISHED" isolation={ISO} onKill={() => {}} onStop={() => {}} />);
    expect(
      screen.queryByRole("button", { name: /stop the agent gracefully/i }),
    ).not.toBeInTheDocument();
  });

  it("Kill is always present and distinct from Stop", () => {
    render(<AgentStatusBar status="RUNNING" isolation={ISO} onKill={() => {}} onStop={() => {}} />);
    expect(screen.getByRole("button", { name: /kill the agent/i })).toBeInTheDocument();
  });

  it("shows a Resume button (not Stop) when PAUSED, wired to onResume", async () => {
    const onResume = vi.fn();
    const user = userEvent.setup();
    render(
      <AgentStatusBar
        status="PAUSED"
        isolation={ISO}
        onKill={() => {}}
        onStop={() => {}}
        onResume={onResume}
      />,
    );
    // A paused build offers Resume, not Stop (you don't stop something already paused).
    expect(
      screen.queryByRole("button", { name: /stop the agent gracefully/i }),
    ).not.toBeInTheDocument();
    const resume = screen.getByRole("button", { name: /resume the agent/i });
    await user.click(resume);
    expect(onResume).toHaveBeenCalledTimes(1);
  });
});
