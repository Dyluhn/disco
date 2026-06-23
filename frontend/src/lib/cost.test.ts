import { describe, it, expect } from "vitest";
import { calculateUsageCost, costLabel, costTag, formatCost } from "./cost";
import { isFree, isMetered, isSubscription, type ModelInfo, type TokenUsage } from "@/types/models";

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

// W-05: a subscription model — flat plan, 0 per-token price, explicit pricing_mode.
const mockSubscriptionModel: ModelInfo = {
  id: "sub",
  label: "Subscription Model",
  provider: "openrouter",
  price_in_per_m: 0,
  price_out_per_m: 0,
  pricing_mode: "subscription",
  capabilities: [],
  model_id: "minimax/minimax-m2",
  context_window: 200_000,
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

describe("W-05 — subscription pricing across surfaces", () => {
  it("classifies a subscription model as neither free nor metered", () => {
    expect(isSubscription(mockSubscriptionModel)).toBe(true);
    expect(isFree(mockSubscriptionModel)).toBe(false); // NOT free — it costs a plan fee
    expect(isMetered(mockSubscriptionModel)).toBe(false); // no per-token bill / $ meter
  });

  it("renders 'Subscription' (not Free, not a price) in the label and the tag", () => {
    expect(costLabel(mockSubscriptionModel)).toBe("Subscription");
    expect(costTag(mockSubscriptionModel)).toBe("Subscription");
    // contrast: free shows Free, metered shows a $ rate
    expect(costLabel(mockFreeModel)).toBe("Free");
    expect(costTag(mockFreeModel)).toBe("Free");
    expect(costLabel(mockPaidModel)).toBe("$10 / $20 / Mtok");
    expect(costTag(mockPaidModel)).toBe("$10/Mtok");
  });

  it("has no per-token usage cost for a subscription model", () => {
    const usage: TokenUsage = { input_tokens: 100_000, output_tokens: 50_000 };
    expect(calculateUsageCost(mockSubscriptionModel, usage)).toBe(0);
  });

  it("still meters metered models and treats free as free", () => {
    expect(isMetered(mockPaidModel)).toBe(true);
    expect(isMetered(mockFreeModel)).toBe(false);
  });
});
