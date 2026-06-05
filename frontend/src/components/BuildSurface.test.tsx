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
import { deriveActivity, deriveFiles, deriveTerminal } from "@/lib/buildTrace";
import {
  gateState,
  traceBeforeGate,
  traceAfterConfirm,
} from "@/fixtures/agentTrace";
import type { ActionEvent } from "@/types/agent";

const GATED = traceBeforeGate.find((e) => e.id === "evt_a_rm") as ActionEvent;
const ALL = [...traceBeforeGate, ...traceAfterConfirm];

async function enterBuildAndSubmit() {
  const user = userEvent.setup();
  render(<App />);
  await user.click(screen.getByRole("radio", { name: "build" }));
  await user.type(screen.getByPlaceholderText(/describe what you want/i), "make fizzbuzz");
  await user.keyboard("{Enter}");
  return user;
}

describe("Build surface (Sidecar: activity feed + canvas + gate)", () => {
  it("shows a plain-language activity feed and pauses at the gate with the risk rationale", async () => {
    await enterBuildAndSubmit();
    await waitFor(
      () => expect(screen.getByRole("alertdialog", { name: /approval/i })).toBeInTheDocument(),
      { timeout: 5000 },
    );
    // the Activity Feed reads as a narrative, not raw tool calls
    expect(screen.getByText("Wrote fizzbuzz.py")).toBeInTheDocument();
    expect(screen.getAllByText("Ran a command").length).toBeGreaterThan(0);
    // the gate carries the risk context
    const gate = screen.getByRole("alertdialog", { name: /approval/i });
    expect(within(gate).getByText(/high risk/i)).toBeInTheDocument();
    expect(within(gate).getByText(/recursive force delete/i)).toBeInTheDocument();
    // the execution canvas surfaces the real terminal output (the agent's work, visible)
    expect(screen.getByText(/FizzBuzz/)).toBeInTheDocument();
    // the kill switch is reachable during the run
    expect(screen.getByRole("button", { name: /kill the agent/i })).toBeEnabled();
  });

  it("Approve resumes the loop to a finished answer rendered as markdown", async () => {
    const user = await enterBuildAndSubmit();
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
    const user = await enterBuildAndSubmit();
    await waitFor(() => screen.getByRole("button", { name: /reject/i }), { timeout: 5000 });
    await user.click(screen.getByRole("button", { name: /reject/i }));
    await waitFor(() => expect(screen.getByText(/left fizzbuzz.py in place/i)).toBeInTheDocument(), {
      timeout: 5000,
    });
  });
});

describe("event → view selectors (buildTrace)", () => {
  it("deriveActivity gives plain labels, flags the pending action, and marks attention", () => {
    const items = deriveActivity(traceBeforeGate, gateState.pending_action_id, "WAITING_FOR_CONFIRMATION");
    expect(items.map((i) => i.label)).toEqual(["Wrote fizzbuzz.py", "Ran a command", "Ran a command"]);
    const pending = items.at(-1)!;
    expect(pending.status).toBe("pending");
    expect(pending.attention).toBe(true); // HIGH-risk gated step floats up
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
