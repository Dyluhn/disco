/**
 * UI-11 — Kill is only offered while there is something to kill.
 *
 * A FINISHED / ERROR / IDLE run has no loop to interrupt and no sandbox left to
 * tear down, so the red button was a control that could not do anything — and
 * next to "Finished" it read as if the delivered build itself could be
 * destroyed. Live and gated states keep it (that's the always-available kill
 * switch, BoD §13.6), which the cluster6 suite pins.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AgentStatusBar } from "@/components/build/AgentStatusBar";
import type { ConversationStatus, IsolationInfo } from "@/types/agent";

const ISO: IsolationInfo = {
  tier: "local",
  label: "Local container",
  adversarialSafe: false,
};

function killButton() {
  return screen.queryByRole("button", { name: /kill the agent/i });
}

describe("AgentStatusBar — Kill visibility (UI-11)", () => {
  it.each<ConversationStatus>(["FINISHED", "ERROR", "IDLE"])(
    "hides Kill on %s — nothing left to kill",
    (status) => {
      render(
        <AgentStatusBar status={status} isolation={ISO} onKill={() => {}} onStop={() => {}} />,
      );
      expect(killButton()).not.toBeInTheDocument();
    },
  );

  it.each<ConversationStatus>([
    "RUNNING",
    "PAUSED",
    "STUCK",
    "WAITING_FOR_CONFIRMATION",
    "AWAITING_PLAN_APPROVAL",
    "AWAITING_USER_DECISION",
    "AWAITING_USER_QUESTION",
  ])("keeps an enabled Kill on %s — the run is still live", (status) => {
    render(<AgentStatusBar status={status} isolation={ISO} onKill={() => {}} onStop={() => {}} />);
    expect(killButton()).toBeEnabled();
  });

  it("still shows the Finished status label once Kill is gone", () => {
    render(<AgentStatusBar status="FINISHED" isolation={ISO} onKill={() => {}} onStop={() => {}} />);
    expect(screen.getByText("Finished")).toBeInTheDocument();
    expect(killButton()).not.toBeInTheDocument();
  });
});
