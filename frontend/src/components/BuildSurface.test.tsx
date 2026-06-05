/**
 * Build surface — integration over the offline fixture trace + unit tests for the
 * trace and the confirmation gate. Verifies: the plan→act→observe trace renders as
 * structured rows (not a chat blob), a risky action surfaces the confirmation UI WITH
 * the risk rationale, Approve resumes to a finished run, Reject records a denial, and
 * the kill control is reachable during a run.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import App from "@/App";
import { AgentTrace } from "@/components/build/AgentTrace";
import { ConfirmationPanel } from "@/components/build/ConfirmationPanel";
import { traceBeforeGate } from "@/fixtures/agentTrace";
import type { ActionEvent } from "@/types/agent";

const GATED = traceBeforeGate.find((e) => e.id === "evt_a_rm") as ActionEvent;

async function enterBuildAndSubmit() {
  const user = userEvent.setup();
  render(<App />);
  await user.click(screen.getByRole("radio", { name: "build" }));
  const input = screen.getByPlaceholderText(/describe a task/i);
  await user.type(input, "write fizzbuzz and clean up");
  await user.keyboard("{Enter}");
  return user;
}

describe("Build surface (live trace + the confirmation gate, over fixtures)", () => {
  it("streams a structured trace and pauses at the confirmation gate with the risk rationale", async () => {
    await enterBuildAndSubmit();

    // the gate surfaces the confirmation UI
    await waitFor(
      () => expect(screen.getByRole("alertdialog", { name: /approval/i })).toBeInTheDocument(),
      { timeout: 5000 },
    );
    // the trace rendered actions + observations as distinct rows (not a chat blob)
    expect(screen.getByText("python3 fizzbuzz.py")).toBeInTheDocument();
    expect(screen.getByText(/FizzBuzz/)).toBeInTheDocument();
    // the human decides WITH the risk context in view
    expect(screen.getByText(/high risk/i)).toBeInTheDocument();
    expect(screen.getByText(/recursive force delete/i)).toBeInTheDocument();
    // the kill switch is reachable during the run
    expect(screen.getByRole("button", { name: /kill the agent/i })).toBeEnabled();
  });

  it("Approve resumes the loop to a finished answer", async () => {
    const user = await enterBuildAndSubmit();
    await waitFor(() => screen.getByRole("button", { name: /approve & run/i }), { timeout: 5000 });
    await user.click(screen.getByRole("button", { name: /approve & run/i }));
    await waitFor(
      () => expect(screen.getByText(/fizzbuzz ran correctly/i)).toBeInTheDocument(),
      { timeout: 5000 },
    );
  });

  it("Reject records a denial without executing the action", async () => {
    const user = await enterBuildAndSubmit();
    await waitFor(() => screen.getByRole("button", { name: /reject/i }), { timeout: 5000 });
    await user.click(screen.getByRole("button", { name: /reject/i }));
    await waitFor(
      () => expect(screen.getByText(/left fizzbuzz.py in place/i)).toBeInTheDocument(),
      { timeout: 5000 },
    );
  });
});

describe("AgentTrace (structured, not a chat blob)", () => {
  it("renders an action's tool call + thought and an observation's result", () => {
    render(<AgentTrace events={traceBeforeGate} pendingActionId="evt_a_rm" />);
    expect(screen.getByText("python3 fizzbuzz.py")).toBeInTheDocument();
    expect(screen.getByText(/Run it to check the output/i)).toBeInTheDocument();
    expect(screen.getByText(/FizzBuzz/)).toBeInTheDocument();
    // the pending (gated) action is marked
    expect(screen.getByText(/awaiting approval/i)).toBeInTheDocument();
  });
});

describe("ConfirmationPanel", () => {
  it("shows the risk rationale and wires Approve/Reject", async () => {
    const user = userEvent.setup();
    const onApprove = vi.fn();
    const onReject = vi.fn();
    render(<ConfirmationPanel action={GATED} onApprove={onApprove} onReject={onReject} />);
    expect(screen.getByText(/recursive force delete/i)).toBeInTheDocument();
    expect(screen.getByText("rm -rf fizzbuzz.py")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /approve & run/i }));
    await user.click(screen.getByRole("button", { name: /reject/i }));
    expect(onApprove).toHaveBeenCalledOnce();
    expect(onReject).toHaveBeenCalledOnce();
  });
});
