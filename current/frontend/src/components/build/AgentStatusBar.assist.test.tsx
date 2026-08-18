/**
 * AgentStatusBar — Assist/Standard execution-tier badge stays hidden.
 *
 * The badge used to read a server-derived `assist` prop (Order D, sourced from
 * state.extras.assist via the reducer) and render "Assist" or "Standard". The
 * prop is gone from this component's signature — the underlying state still
 * exists upstream (useBuildStream.ts's `assist` field, reducer.ts's
 * extras.assist handling, useBuild.ts's returned `assist`), this component
 * just no longer accepts or renders it. This guards against a follow-up prop
 * threading silently resurrecting the badge.
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

describe("AgentStatusBar — Assist/Standard tier badge stays hidden", () => {
  it("never renders an Assist or Standard pill when autonomous/running", () => {
    render(<AgentStatusBar status="RUNNING" isolation={ISO} autonomous onKill={() => {}} />);
    expect(screen.queryByText("Assist")).not.toBeInTheDocument();
    expect(screen.queryByText("Standard")).not.toBeInTheDocument();
    expect(screen.queryByTitle(/small-model compensations/i)).not.toBeInTheDocument();
    expect(screen.queryByTitle(/running with a capable model/i)).not.toBeInTheDocument();
  });

  it("never renders an Assist or Standard pill when paused/non-autonomous", () => {
    render(<AgentStatusBar status="PAUSED" isolation={ISO} onKill={() => {}} />);
    expect(screen.queryByText("Assist")).not.toBeInTheDocument();
    expect(screen.queryByText("Standard")).not.toBeInTheDocument();
  });

  it("keeps the other status badges intact — a targeted removal, not a wholesale gutting", () => {
    render(<AgentStatusBar status="RUNNING" isolation={ISO} autonomous onKill={() => {}} />);
    expect(screen.getByTitle("gVisor (hardware sandbox)")).toBeInTheDocument();
    expect(screen.getByText("autonomous")).toBeInTheDocument();
  });
});
