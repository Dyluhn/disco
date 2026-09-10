/**
 * Build surface — W-43 Verbose Agent Chat (OFF) collapse behaviour.
 *
 * Drives the offline fixture flow (the same one BuildSurface.test uses): submit →
 * approve plan → per-action gate. With the preference OFF, the running narrative
 * collapses to the AgentStageCard — BUT:
 *   • the confirm gate STILL renders (gates are never hidden), and
 *   • the full feed (with its download/artifact affordances) stays reachable by
 *     expanding the stage card (codex P1 — no affordance is stranded).
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import App from "@/App";

beforeEach(() => {
  window.localStorage.setItem("verboseAgentChat", "0");
});
afterEach(() => {
  window.localStorage.clear();
});

async function submitAndApprovePlan() {
  const user = userEvent.setup();
  render(<App />);
  await user.click(screen.getByRole("radio", { name: "build" }));
  await user.type(
    await screen.findByPlaceholderText(
      /describe what you want/i,
      {},
      { timeout: 5000 },
    ),
    "make fizzbuzz",
  );
  await user.keyboard("{Enter}");
  await waitFor(() => screen.getByRole("button", { name: /approve & build/i }), { timeout: 5000 });
  await user.click(screen.getByRole("button", { name: /approve & build/i }));
  return user;
}

describe("Build surface — verbose chat OFF (W-43)", () => {
  it("collapses the feed to the stage card, but the confirm gate still renders", async () => {
    await submitAndApprovePlan();
    // the per-action gate is preserved (gates are never collapsed away)
    await waitFor(
      () => expect(screen.getByRole("alertdialog", { name: /action needs your approval/i })).toBeInTheDocument(),
      { timeout: 5000 },
    );
    // the running narrative collapsed to the stage card …
    expect(screen.getByTestId("agent-stage-card")).toBeInTheDocument();
    // … and the verbose activity label is hidden until expanded
    expect(screen.queryByText("Wrote fizzbuzz.py")).toBeNull();
  });

  it("keeps the sticky plan region opaque above scrolling feed rows", async () => {
    await submitAndApprovePlan();
    const stickyPlan = await screen.findByTestId("build-sticky-plan-card");
    expect(stickyPlan).toHaveClass("sticky", "z-20", "bg-bg", "pb-section");
    expect(stickyPlan).not.toHaveClass("mb-section");
  });

  it("expanding the stage card reveals the full feed (artifact affordances reachable)", async () => {
    const user = await submitAndApprovePlan();
    const card = await waitFor(() => screen.getByTestId("agent-stage-card"), { timeout: 5000 });
    await user.click(within(card).getByRole("button", { name: /expand agent history/i }));
    // the full activity feed (and its inline affordances) is now in the DOM
    await waitFor(() => expect(screen.getByText("Wrote fizzbuzz.py")).toBeInTheDocument(), {
      timeout: 5000,
    });
  });
});
