/**
 * Build surface (the split-pane Sidecar) — integration over the offline fixture + units
 * for the derived views. Verifies: the Activity Feed reads as plain language (not a tool
 * log); the execution canvas surfaces the agent's Files + Terminal output; a risky action
 * pauses with its risk rationale; Approve resumes, Reject denies; the kill switch is
 * reachable; and the event→view selectors are correct.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import App from "@/App";
import { ConfirmationPanel } from "@/components/build/ConfirmationPanel";
import {
  derivePlan,
  derivePlanProgress,
  deriveActivity,
  deriveFiles,
  deriveTerminal,
} from "@/lib/buildTrace";
import {
  gateState,
  planEvent,
  traceBeforeGate,
  traceAfterConfirm,
} from "@/fixtures/agentTrace";
import type { ActionEvent } from "@/types/agent";

const GATED = traceBeforeGate.find((e) => e.id === "evt_a_rm") as ActionEvent;
const ALL = [planEvent, ...traceBeforeGate, ...traceAfterConfirm];

async function enterBuildAndSubmit() {
  const user = userEvent.setup();
  render(<App />);
  await user.click(screen.getByRole("radio", { name: "build" }));
  await user.type(screen.getByPlaceholderText(/describe what you want/i), "make fizzbuzz");
  await user.keyboard("{Enter}");
  return user;
}

// The plan-first lifecycle: submit → review the plan → Approve & build → per-action gate.
async function enterBuildSubmitAndApprovePlan() {
  const user = await enterBuildAndSubmit();
  await waitFor(
    () => screen.getByRole("button", { name: /approve & build/i }),
    { timeout: 5000 },
  );
  await user.click(screen.getByRole("button", { name: /approve & build/i }));
  return user;
}

describe("Build surface (plan gate → build → action gate)", () => {
  it("pauses on a plan-approval gate FIRST, showing the proposed steps + rationale", async () => {
    await enterBuildAndSubmit();
    const planGate = await waitFor(
      () => screen.getByRole("alertdialog", { name: /plan.*approval/i }),
      { timeout: 5000 },
    );
    // the proposed steps are visible up front (capstones, not raw tool calls)
    expect(within(planGate).getByText(/Write fizzbuzz\.py/i)).toBeInTheDocument();
    expect(within(planGate).getByText(/Run it to verify/i)).toBeInTheDocument();
    expect(within(planGate).getByText(/Remove the script/i)).toBeInTheDocument();
    // the planner's context/rationale renders as markdown above the steps
    expect(within(planGate).getByText(/Context & rationale/i)).toBeInTheDocument();
    expect(within(planGate).getByText(/Findings/i)).toBeInTheDocument();
    expect(within(planGate).getByText(/workspace is empty/i)).toBeInTheDocument();
    // and Approve/Revise are wired
    expect(within(planGate).getByRole("button", { name: /approve & build/i })).toBeEnabled();
    expect(within(planGate).getByRole("button", { name: /revise/i })).toBeEnabled();
  });

  it("after plan approval, shows the activity feed and pauses at the per-action gate", async () => {
    await enterBuildSubmitAndApprovePlan();
    await waitFor(
      () =>
        expect(
          screen.getByRole("alertdialog", { name: /action needs your approval/i }),
        ).toBeInTheDocument(),
      { timeout: 5000 },
    );
    expect(screen.getByText("Wrote fizzbuzz.py")).toBeInTheDocument();
    expect(screen.getAllByText("Ran a command").length).toBeGreaterThan(0);
    const gate = screen.getByRole("alertdialog", { name: /action needs your approval/i });
    expect(within(gate).getByText(/high risk/i)).toBeInTheDocument();
    expect(within(gate).getByText(/recursive force delete/i)).toBeInTheDocument();
    // FizzBuzz appears in plan text + step labels + terminal output; the terminal
    // entry is the load-bearing assertion (the canvas shows the real run output).
    expect(screen.getAllByText(/FizzBuzz/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByRole("button", { name: /kill the agent/i })).toBeEnabled();
  });

  it("Approve resumes the loop to a finished answer rendered as markdown", async () => {
    const user = await enterBuildSubmitAndApprovePlan();
    await waitFor(() => screen.getByRole("button", { name: /approve & run/i }), { timeout: 5000 });
    await user.click(screen.getByRole("button", { name: /approve & run/i }));
    await waitFor(() => expect(screen.getByText(/fizzbuzz ran correctly/i)).toBeInTheDocument(), {
      timeout: 5000,
    });
    // markdown is RENDERED, not literal: **bold** → <strong>, `code` → <code>, no raw "**"
    expect(screen.getByText("fizzbuzz ran correctly").tagName).toBe("STRONG");
    expect(screen.getAllByText("fizzbuzz.py").some((el) => el.tagName === "CODE")).toBe(true);
    expect(screen.queryByText(/\*\*fizzbuzz/)).not.toBeInTheDocument();
  });

  it("shows the model picker (which model runs the agent) on the empty state", async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByRole("radio", { name: "build" }));
    // the picker trigger is present and shows the default model from the catalogue
    expect(screen.getByRole("button", { name: /choose the model/i })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("Qwen3.6-27B")).toBeInTheDocument(), {
      timeout: 5000,
    });
  });

  it("Reject records a denial without executing the action", async () => {
    const user = await enterBuildSubmitAndApprovePlan();
    await waitFor(() => screen.getByRole("button", { name: /reject/i }), { timeout: 5000 });
    await user.click(screen.getByRole("button", { name: /reject/i }));
    await waitFor(() => expect(screen.getByText(/left fizzbuzz.py in place/i)).toBeInTheDocument(), {
      timeout: 5000,
    });
  });

  it("after FINISHED, the bottom input becomes the 'Plan a change' box (re-enter plan mode)", async () => {
    const user = await enterBuildSubmitAndApprovePlan();
    await waitFor(() => screen.getByRole("button", { name: /approve & run/i }), { timeout: 5000 });
    await user.click(screen.getByRole("button", { name: /approve & run/i }));
    await waitFor(() => expect(screen.getByText(/fizzbuzz ran correctly/i)).toBeInTheDocument(), {
      timeout: 5000,
    });
    // the steer box is gone; the plan-a-change input takes its place
    const replan = screen.getByPlaceholderText(/plan a change to this build/i);
    expect(replan).toBeInTheDocument();
    // typing a change request kicks off a re-plan → another plan-approval gate
    await user.type(replan, "add a unit test");
    await user.keyboard("{Enter}");
    await waitFor(
      () => {
        const planGate = screen.getByRole("alertdialog", { name: /plan.*approval/i });
        expect(within(planGate).getByText(/revision 2/i)).toBeInTheDocument();
        expect(within(planGate).getByText(/add a unit test/i)).toBeInTheDocument();
      },
      { timeout: 5000 },
    );
  });
});

describe("event → view selectors (buildTrace)", () => {
  it("deriveActivity gives plain labels, skips plan-mode meta tools, flags the pending action", () => {
    const items = deriveActivity(traceBeforeGate, gateState.pending_action_id, "WAITING_FOR_CONFIRMATION");
    // plan_step actions are filtered out (control signals, not work)
    expect(items.map((i) => i.label)).toEqual(["Wrote fizzbuzz.py", "Ran a command", "Ran a command"]);
    const pending = items.at(-1)!;
    expect(pending.status).toBe("pending");
    expect(pending.attention).toBe(true); // HIGH-risk gated step floats up
  });

  it("derivePlan returns the latest plan and derivePlanProgress tracks plan_step reports", () => {
    const plan = derivePlan([planEvent, ...traceBeforeGate]);
    expect(plan).not.toBeNull();
    expect(plan!.steps).toHaveLength(3);
    expect(plan!.revision).toBe(1);
    const progress = derivePlanProgress(traceBeforeGate);
    expect(progress.get(1)).toBe("done"); // step 1 marked active then done
    expect(progress.get(2)).toBe("done");
    expect(progress.get(3)).toBeUndefined(); // step 3 not yet reported → pending
  });

  it("deriveFiles reads written files from the stream", () => {
    const files = deriveFiles(ALL);
    expect(files).toHaveLength(1);
    expect(files[0].path).toBe("fizzbuzz.py");
    expect(files[0].content).toContain("FizzBuzz");
    expect(files[0].bytes).toBeGreaterThan(0);
  });

  it("deriveTerminal pairs commands with their output", () => {
    const term = deriveTerminal(ALL);
    expect(term[0].command).toBe("python3 fizzbuzz.py");
    expect(term[0].output).toContain("FizzBuzz");
    expect(term[0].success).toBe(true);
  });
});

describe("ConfirmationPanel", () => {
  it("shows the risk rationale and wires Approve/Reject", async () => {
    const user = userEvent.setup();
    const onApprove = vi.fn();
    const onReject = vi.fn();
    render(<ConfirmationPanel action={GATED} onApprove={onApprove} onReject={onReject} />);
    expect(screen.getByText(/recursive force delete/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /approve & run/i }));
    await user.click(screen.getByRole("button", { name: /reject/i }));
    expect(onApprove).toHaveBeenCalledOnce();
    expect(onReject).toHaveBeenCalledOnce();
  });
});
