import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { AgentStatusBar } from "./AgentStatusBar";
import type { AgentEvent } from "@/types/agent";

// Mock useModels
vi.mock("@/hooks/useModels", () => ({
  useModels: () => ({
    data: [
      {
        id: "paid",
        provider: "openrouter",
        price_in_per_m: 10,
        price_out_per_m: 20,
      },
      {
        id: "free",
        provider: "local",
        price_in_per_m: 0,
        price_out_per_m: 0,
      },
      {
        id: "sub",
        provider: "openrouter",
        price_in_per_m: 0,
        price_out_per_m: 0,
        pricing_mode: "subscription",
      },
    ],
  }),
}));

describe("AgentStatusBar cost meter", () => {
  it("shows 'Free' for a free model", () => {
    render(
      <AgentStatusBar
        status="RUNNING"
        isolation={null}
        onKill={() => {}}
        modelId="free"
      />
    );
    expect(screen.getByText("Free")).toBeDefined();
  });

  it("shows 'Subscription' (never 'Free', never a price) for a subscription model", () => {
    render(
      <AgentStatusBar
        status="RUNNING"
        isolation={null}
        onKill={() => {}}
        modelId="sub"
      />
    );
    const meter = screen.getByText("Subscription");
    expect(meter).toBeDefined();
    expect(meter.getAttribute("data-cost-state")).toBe("subscription");
    // W-05: a subscription must NEVER read as free, and NEVER show a per-token price.
    expect(screen.queryByText("Free")).toBeNull();
    expect(screen.queryByText(/\$/)).toBeNull();
  });

  it("calculates cumulative cost from events", () => {
    const events: AgentEvent[] = [
      {
        kind: "action",
        id: "a1",
        thought: "",
        tool_call: null,
        meta: {
          model_id: "paid",
          usage: { input_tokens: 100_000, output_tokens: 50_000 },
        },
      },
      {
        kind: "action",
        id: "a2",
        thought: "",
        tool_call: null,
        meta: {
          model_id: "paid",
          usage: { input_tokens: 200_000, output_tokens: 100_000 },
        },
      },
    ];

    render(
      <AgentStatusBar
        status="RUNNING"
        isolation={null}
        onKill={() => {}}
        events={events}
        modelId="paid"
      />
    );
    
    // Turn 1: (100k*10 + 50k*20)/1M = 2
    // Turn 2: (200k*10 + 100k*20)/1M = 4
    // Total = 6
    expect(screen.getByText("$6.00")).toBeDefined();
  });

  it("shows '≥' when usage is missing for some turns", () => {
    const events: AgentEvent[] = [
      {
        kind: "action",
        id: "a1",
        thought: "",
        tool_call: null,
        meta: {
          model_id: "paid",
          usage: { input_tokens: 100_000, output_tokens: 50_000 },
        },
      },
      {
        kind: "action",
        id: "a2",
        thought: "",
        tool_call: null,
        meta: { model_id: "paid" }, // missing usage
      },
    ];

    render(
      <AgentStatusBar
        status="RUNNING"
        isolation={null}
        onKill={() => {}}
        events={events}
        modelId="paid"
      />
    );
    
    expect(screen.getByText("≥")).toBeDefined();
    expect(screen.getByText("$2.00")).toBeDefined();
  });
});
