import { describe, it, expect } from "vitest";
import { calculateUsageCost, formatCost } from "./cost";
import type { ModelInfo, TokenUsage } from "@/types/models";

const mockPaidModel: ModelInfo = {
  id: "paid",
  label: "Paid Model",
  provider: "openrouter",
  price_in_per_m: 10,
  price_out_per_m: 20,
  capabilities: [],
  model_id: "paid-model",
  context_window: 1000,
};

const mockFreeModel: ModelInfo = {
  id: "free",
  label: "Free Model",
  provider: "local",
  price_in_per_m: 0,
  price_out_per_m: 0,
  capabilities: [],
  model_id: "free-model",
  context_window: 1000,
};

describe("cost calculation", () => {
  it("calculates cost correctly for paid models", () => {
    const usage: TokenUsage = { input_tokens: 100_000, output_tokens: 50_000 };
    // (100k * 10 / 1M) + (50k * 20 / 1M) = 1 + 1 = 2
    expect(calculateUsageCost(mockPaidModel, usage)).toBe(2);
  });

  it("returns 0 for free models", () => {
    const usage: TokenUsage = { input_tokens: 100_000, output_tokens: 50_000 };
    expect(calculateUsageCost(mockFreeModel, usage)).toBe(0);
  });

  it("uses pre-calculated cost_usd if available", () => {
    const usage: TokenUsage = { input_tokens: 100_000, output_tokens: 50_000, cost_usd: 5.5 };
    expect(calculateUsageCost(mockPaidModel, usage)).toBe(5.5);
  });

  it("formats cost correctly", () => {
    expect(formatCost(0)).toBe("$0.00");
    expect(formatCost(2.5)).toBe("$2.50");
    expect(formatCost(0.00123)).toBe("$0.0012");
  });
});
