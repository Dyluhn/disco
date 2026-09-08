/**
 * The hold panel is a CHOICE, not a failure notice. These tests pin that it
 * says why the wait exists, what state the run is in, what happens next with
 * nobody touching it, and what stopping keeps — and that both controls do what
 * they claim: Continue waiting sends nothing, Stop stops the run.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { deriveActivity, type ActiveHold } from "@/lib/deepResearchTrace";
import holdAction from "@/lib/__fixtures__/hold-action.json";
import type { ActionEvent } from "@/types/agent";
import { DeepResearchHoldLine, DeepResearchHoldPanel } from "./DeepResearchHoldPanel";

const T0 = Date.parse("2026-09-02T10:00:00.000Z");

function iso(offsetSeconds: number): string {
  return new Date(T0 + offsetSeconds * 1000).toISOString();
}

const holdEvent: ActionEvent = {
  id: "hold_1",
  kind: "action",
  seq: 9,
  timestamp: iso(0),
  thought: "",
  tool_call: {
    tool_name: "hold",
    arguments: {
      reason: "search_pool_cooling",
      engines: [
        { name: "brave", resume_at: iso(220) },
        { name: "mojeek", resume_at: iso(250) },
      ],
      resume_at: iso(220),
      sources_retained: 42,
      turn: { n: 7, of: 16 },
      // The producer sends the QUERIES, not a count — see
      // `lib/__fixtures__/hold-action.json` for a verbatim `emit_hold` payload.
      queued_queries: ["pilot line capacity", "cycle life field data", "separator supply"],
    },
  },
};

function hold(): ActiveHold {
  const active = deriveActivity([holdEvent]).hold;
  if (!active) throw new Error("fixture did not produce a hold");
  return active;
}

describe("DeepResearchHoldPanel", () => {
  it("states why, the state now, what happens next, and what is still allowed", () => {
    render(
      <DeepResearchHoldPanel
        hold={hold()}
        nowMs={T0}
        onContinue={() => {}}
        onStop={() => {}}
      />,
    );
    const panel = screen.getByRole("region", { name: /Waiting for search engines/i });
    expect(panel).toHaveTextContent("brave and mojeek hit their rate limits and are cooling down.");
    expect(panel).toHaveTextContent("This is a cooldown, not an error.");
    expect(panel).toHaveTextContent("Turn 7 of 16 · 42 sources kept · 3 queries waiting to run");
    expect(panel).toHaveTextContent("Research resumes on its own in 3:40.");
    expect(panel).toHaveTextContent(/Stopping keeps the 42 sources found so far/);
  });

  it("offers exactly two decisions, both named for what they do", () => {
    render(
      <DeepResearchHoldPanel hold={hold()} nowMs={T0} onContinue={() => {}} onStop={() => {}} />,
    );
    expect(
      document.querySelector('[data-disco-control="dr.hold.continue"]'),
    ).toHaveTextContent("Continue waiting");
    expect(document.querySelector('[data-disco-control="dr.hold.stop"]')).toHaveTextContent(
      "Stop and keep what it has",
    );
  });

  it("Continue waiting is local — it calls back without touching the run", async () => {
    const user = userEvent.setup();
    const onContinue = vi.fn();
    const onStop = vi.fn();
    render(
      <DeepResearchHoldPanel hold={hold()} nowMs={T0} onContinue={onContinue} onStop={onStop} />,
    );
    await user.click(screen.getByRole("button", { name: /Continue waiting/i }));
    expect(onContinue).toHaveBeenCalledTimes(1);
    expect(onStop).not.toHaveBeenCalled();
  });

  it("Stop calls the run's stop", async () => {
    const user = userEvent.setup();
    const onStop = vi.fn();
    render(
      <DeepResearchHoldPanel hold={hold()} nowMs={T0} onContinue={() => {}} onStop={onStop} />,
    );
    await user.click(screen.getByRole("button", { name: /Stop and keep what it has/i }));
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("never dresses the wait as an error", () => {
    const { container } = render(
      <DeepResearchHoldPanel hold={hold()} nowMs={T0} onContinue={() => {}} onStop={() => {}} />,
    );
    expect(container.textContent).not.toMatch(/failed|failure|went wrong/i);
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });
});

/**
 * The queued queries are the work the hold is waiting to do, so the panel owes
 * them whole. Driven from `__fixtures__/hold-action.json` — the verbatim
 * `emit_hold` payload — because the third query is the one the trace row cuts
 * mid-word, and a shorter hand-written query would not reproduce that.
 */
describe("DeepResearchHoldPanel — the queued work", () => {
  function holdFromProducer(): ActiveHold {
    const event: ActionEvent = {
      ...holdEvent,
      tool_call: {
        tool_name: holdAction.tool_name,
        arguments: holdAction.arguments as unknown as Record<string, unknown>,
      },
    };
    const active = deriveActivity([event]).hold;
    if (!active) throw new Error("fixture did not produce a hold");
    return active;
  }

  it("lists every queued query in full, under the state line", () => {
    render(
      <DeepResearchHoldPanel
        hold={holdFromProducer()}
        nowMs={Date.parse("2026-09-02T08:31:52.000Z")}
        onContinue={() => {}}
        onStop={() => {}}
      />,
    );
    const list = document.querySelector('[data-dr-hold-queued="list"]');
    expect(list).not.toBeNull();
    expect(list!.querySelectorAll("li")).toHaveLength(3);
    for (const query of holdAction.arguments.queued_queries) {
      expect(list).toHaveTextContent(query);
    }
  });

  it("renders no list when the producer queued nothing", () => {
    const event: ActionEvent = {
      ...holdEvent,
      tool_call: {
        tool_name: holdAction.tool_name,
        arguments: { ...holdAction.arguments, queued_queries: [] } as unknown as Record<
          string,
          unknown
        >,
      },
    };
    const active = deriveActivity([event]).hold!;
    render(
      <DeepResearchHoldPanel hold={active} nowMs={T0} onContinue={() => {}} onStop={() => {}} />,
    );
    expect(document.querySelector('[data-dr-hold-queued="list"]')).toBeNull();
  });
});

describe("DeepResearchHoldLine (the collapsed form)", () => {
  it("keeps the live countdown and keeps Stop reachable", async () => {
    const user = userEvent.setup();
    const onStop = vi.fn();
    render(<DeepResearchHoldLine hold={hold()} nowMs={T0 + 100_000} onStop={onStop} />);
    expect(screen.getByText("Waiting for search engines — resumes in 2:00")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Stop" }));
    expect(onStop).toHaveBeenCalledTimes(1);
  });
});
