import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AgentStatusBar } from "@/components/build/AgentStatusBar";
import { BuildModelPicker } from "@/components/build/BuildModelPicker";
import { ModelCatalogue } from "@/components/settings/ModelCatalogue";
import { createModel } from "@/api/models";
import { calculateUsageCost, costLabel, costTag } from "@/lib/cost";
import { isFree, isMetered, isSubscription, type ModelInfo } from "@/types/models";
import type { DriverModel } from "@/types/agent";

const hookState = vi.hoisted(() => ({
  models: [] as ModelInfo[],
  driverModels: [] as DriverModel[],
  driverDefault: null as string | null,
  lastSelected: null as string | null,
}));

vi.mock("@/hooks/useModels", () => ({
  useModels: () => ({ data: hookState.models }),
  useDeleteModel: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useCreateModel: () => ({ mutateAsync: vi.fn(), isPending: false, error: null }),
  useUpdateModel: () => ({ mutateAsync: vi.fn(), isPending: false, error: null }),
}));

vi.mock("@/hooks/useDriverModels", () => ({
  useDriverModels: () => ({
    data: { models: hookState.driverModels, default: hookState.driverDefault },
  }),
  useLastSelectedModel: () => ({ data: hookState.lastSelected }),
}));

vi.mock("@/components/Toast", () => ({
  useToast: () => ({ show: vi.fn() }),
}));

const subscriptionModel: ModelInfo = {
  id: "minimax-subscription",
  label: "MiniMax M2",
  provider: "openrouter",
  price_in_per_m: 0,
  price_out_per_m: 0,
  pricing_mode: "subscription",
  capabilities: ["tool_calling"],
  note: "200K ctx",
  model_id: "minimax/minimax-m2",
  base_url: "https://minimax.example/v1",
  api_key_env: "DISCO_MINIMAX_API_KEY",
  context_window: 200_000,
  quantization: null,
};

describe("AuthorB unbiased gate — W-04/W-05 model display", () => {
  beforeEach(() => {
    hookState.models = [];
    hookState.driverModels = [];
    hookState.driverDefault = null;
    hookState.lastSelected = null;
  });

  it("W-04 keeps the OpenRouter or- catalogue key but removes the bogus 'Or ' label prefix", async () => {
    const id = `or-authorb-${Math.random().toString(36).slice(2)}`;
    const models = await createModel({
      id,
      model_id: "authorb/super-model",
      context_window: 128_000,
      capabilities: ["tool_calling"],
      price_in_per_m: 1.25,
      price_out_per_m: 2.5,
      pricing_mode: "metered",
      base_url: "https://openrouter.ai/api/v1",
      api_key_env: "DISCO_OPENROUTER_API_KEY",
      quantization: null,
    });

    const added = models.find((m) => m.id === id);
    expect(added).toBeTruthy();
    expect(added!.id).toBe(id);
    expect(added!.label).not.toMatch(/^Or\b/);
    expect(added!.label).toContain("Authorb");
    expect(added!.label).toContain("super-model");
  });

  it("W-05 treats a zero-price subscription as subscription, not free or metered", () => {
    expect(isSubscription(subscriptionModel)).toBe(true);
    expect(isFree(subscriptionModel)).toBe(false);
    expect(isMetered(subscriptionModel)).toBe(false);
    expect(costLabel(subscriptionModel)).toBe("Subscription");
    expect(costTag(subscriptionModel)).toBe("Subscription");
    expect(
      calculateUsageCost(subscriptionModel, {
        input_tokens: 500_000,
        output_tokens: 500_000,
        cost_usd: 12.34,
      }),
    ).toBe(0);
  });

  it("W-05 status bar shows Subscription and never Free or a per-token price", () => {
    hookState.models = [subscriptionModel];

    render(
      <AgentStatusBar
        status="RUNNING"
        isolation={{ tier: "local", label: "Local sandbox", adversarialSafe: true }}
        onKill={() => {}}
        onStop={() => {}}
        modelId={subscriptionModel.id}
        events={[]}
      />,
    );

    const meter = screen.getByText("Subscription");
    expect(meter).toBeInTheDocument();
    expect(meter.closest("[data-disco-control='build.cost-meter']")).toHaveAttribute(
      "data-cost-state",
      "subscription",
    );
    expect(screen.queryByText("Free")).not.toBeInTheDocument();
    expect(screen.queryByText(/\$.*Mtok/i)).not.toBeInTheDocument();
  });

  it("W-05 catalogue row labels a subscription model as Subscription", () => {
    hookState.models = [subscriptionModel];

    render(<ModelCatalogue />);

    const row = screen.getByText(subscriptionModel.label).closest("li");
    expect(row).toBeTruthy();
    expect(within(row!).getByText("Subscription")).toBeInTheDocument();
    expect(within(row!).queryByText("Free")).not.toBeInTheDocument();
    expect(within(row!).queryByText(/\$.*Mtok/i)).not.toBeInTheDocument();
  });

  it("W-05 model picker selected state says Subscription, not Free or a price", async () => {
    const user = userEvent.setup();
    hookState.driverModels = [
      {
        id: "sub-driver",
        label: "MiniMax M2",
        provider: "openrouter",
        free: false,
        pricing_mode: "subscription",
        context_window: 200_000,
      },
    ];
    hookState.driverDefault = "sub-driver";

    render(<BuildModelPicker value="sub-driver" onChange={() => {}} />);

    const trigger = screen.getByRole("button", { name: /choose the model/i });
    expect(trigger).toHaveTextContent("Subscription");
    expect(trigger).not.toHaveTextContent("Free");
    expect(trigger).not.toHaveTextContent(/\$.*Mtok/i);

    await user.click(trigger);
    expect(await screen.findByText("Subscription")).toBeInTheDocument();
  });
});
