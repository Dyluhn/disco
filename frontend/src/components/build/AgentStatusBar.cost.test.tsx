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

  it("calculates cumulative cost from events", () => {
    const events: any[] = [
      {
        kind: "action",
        meta: {
          model_id: "paid",
          usage: { input_tokens: 100_000, output_tokens: 50_000 },
        },
      },
      {
        kind: "action",
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
    const events: any[] = [
      {
        kind: "action",
        meta: {
          model_id: "paid",
          usage: { input_tokens: 100_000, output_tokens: 50_000 },
        },
      },
      {
        kind: "action",
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
