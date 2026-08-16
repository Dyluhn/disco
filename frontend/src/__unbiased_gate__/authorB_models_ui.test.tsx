import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
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
  visionModel: null as string | null,
  updateAssignments: vi.fn(),
  showToast: vi.fn(),
}));

vi.mock("@/hooks/useModels", () => ({
  useModels: () => ({ data: hookState.models }),
  useAssignments: () => ({
    data: {
      default_model: hookState.driverDefault ?? "driver-local",
      roles: {},
      vision_model: hookState.visionModel,
    },
  }),
  useUpdateAssignments: () => ({
    mutateAsync: hookState.updateAssignments,
    mutate: hookState.updateAssignments,
    isPending: false,
    error: null,
  }),
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

vi.mock("@/components/toastApi", () => ({
  useToast: () => ({ show: hookState.showToast }),
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
    hookState.visionModel = null;
    hookState.updateAssignments.mockReset();
    hookState.showToast.mockReset();
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
        capabilities: ["tool_calling"],
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

  it("uses task copy when Agent selects a paid driver", async () => {
    const user = userEvent.setup();
    hookState.driverModels = [
      {
        id: "free-driver",
        label: "Free Driver",
        provider: "local",
        free: true,
        context_window: 128_000,
        capabilities: ["tool_calling", "vision"],
      },
      {
        id: "paid-driver",
        label: "Paid Driver",
        provider: "openrouter",
        free: false,
        context_window: 128_000,
        capabilities: ["tool_calling", "vision"],
      },
    ];
    hookState.driverDefault = "free-driver";

    render(
      <MemoryRouter>
        <BuildModelPicker value={null} onChange={() => {}} surface="agent" />
      </MemoryRouter>,
    );
    await user.click(screen.getByRole("button", { name: /choose the model/i }));
    await user.click(await screen.findByText("Paid Driver"));

    expect(hookState.showToast).toHaveBeenCalledWith({
      tone: "cost",
      title: "Now working with Paid Driver",
      body: "This paid model drives every step of the agent for this task.",
    });
  });

  it("uses task copy when resuming the Agent surface", () => {
    render(
      <AgentStatusBar
        status="PAUSED"
        isolation={{ tier: "local", label: "Local sandbox", adversarialSafe: true }}
        onKill={() => {}}
        onResume={() => {}}
        surface="agent"
      />,
    );

    expect(
      screen.getByRole("button", { name: /continue this task where it left off/i }),
    ).toBeInTheDocument();
  });

  it("recommends and assigns a bounded visual model after a text-only driver pick", async () => {
    const user = userEvent.setup();
    hookState.models = [
      subscriptionModel,
      {
        ...subscriptionModel,
        id: "visual-model",
        label: "Visual Model",
        pricing_mode: "metered",
        capabilities: ["vision"],
      },
    ];
    hookState.driverModels = [
      {
        id: "visual-driver",
        label: "Visual Driver",
        provider: "local",
        free: true,
        context_window: 128_000,
        capabilities: ["tool_calling", "vision"],
      },
      {
        id: "text-driver",
        label: "Text Driver",
        provider: "local",
        free: true,
        context_window: 128_000,
        capabilities: ["tool_calling"],
      },
    ];
    hookState.driverDefault = "visual-driver";
    hookState.updateAssignments.mockResolvedValue({
      default_model: "visual-driver",
      roles: {},
      vision_model: "visual-model",
    });

    render(
      <MemoryRouter>
        <BuildModelPicker value="visual-driver" onChange={() => {}} />
      </MemoryRouter>,
    );
    await user.click(screen.getByRole("button", { name: /choose the model/i }));
    await user.click(await screen.findByText("Text Driver"));

    const dialog = await screen.findByRole("dialog", { name: /Add visual inspection/i });
    expect(within(dialog).getByText(/no tools or conversation history/i)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: /Visual Model/i }));
    expect(hookState.updateAssignments).toHaveBeenCalledWith(
      { vision_model: "visual-model" },
      { onSuccess: expect.any(Function) },
    );
  });
});
