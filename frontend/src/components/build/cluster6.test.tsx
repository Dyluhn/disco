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
    expect(deriveLiveSignal([], "AWAITING_USER_QUESTION")).toEqual({
      kind: "waiting_for_you",
      label: expect.stringMatching(/question/i),
    });
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

  it("shows a subtle reconnecting badge when the stream is degraded", () => {
    render(
      <AgentStatusBar
        status="RUNNING"
        isolation={ISO}
        connectionState="degraded"
        onKill={() => {}}
        onStop={() => {}}
      />,
    );
    const badge = screen.getByText(/reconnecting…/i);
    expect(badge).toBeInTheDocument();
    expect(badge.closest("[data-disco-control='build.connection']")).toHaveAttribute(
      "data-connection-state",
      "degraded",
    );
  });

  it("Stop shows a 'Stopping…' pending state after click (cancel isn't instant)", async () => {
    const user = userEvent.setup();
    render(<AgentStatusBar status="RUNNING" isolation={ISO} onKill={() => {}} onStop={() => {}} />);
    await user.click(screen.getByRole("button", { name: /stop the agent gracefully/i }));
    // the click isn't a dead void — the button reflects the in-flight cancel
    expect(screen.getByText(/stopping…/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /stop the agent gracefully/i })).toBeDisabled();
  });

  it("Kill requires confirmation — first click arms, second confirms", async () => {
    const onKill = vi.fn();
    const user = userEvent.setup();
    render(<AgentStatusBar status="RUNNING" isolation={ISO} onKill={onKill} onStop={() => {}} />);
    // first click does NOT kill — it arms the confirm
    await user.click(screen.getByRole("button", { name: /kill the agent/i }));
    expect(onKill).not.toHaveBeenCalled();
    expect(screen.getByText(/kill this run\?/i)).toBeInTheDocument();
    // confirm fires the destructive action
    await user.click(screen.getByRole("button", { name: /confirm kill/i }));
    expect(onKill).toHaveBeenCalledTimes(1);
  });

  it("Kill confirm can be cancelled without killing", async () => {
    const onKill = vi.fn();
    const user = userEvent.setup();
    render(<AgentStatusBar status="RUNNING" isolation={ISO} onKill={onKill} onStop={() => {}} />);
    await user.click(screen.getByRole("button", { name: /kill the agent/i }));
    await user.click(screen.getByRole("button", { name: /keep the run/i }));
    expect(onKill).not.toHaveBeenCalled();
    // back to the armed Kill button (not the confirm prompt)
    expect(screen.getByRole("button", { name: /kill the agent/i })).toBeInTheDocument();
    expect(screen.queryByText(/kill this run\?/i)).not.toBeInTheDocument();
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
