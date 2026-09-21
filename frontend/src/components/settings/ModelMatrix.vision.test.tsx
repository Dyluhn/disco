import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ModelAssignments, ModelInfo } from "@/types/models";
import { ModelMatrix } from "./ModelMatrix";

const state = vi.hoisted(() => ({
  models: [] as ModelInfo[],
  assignments: {
    default_model: "primary",
    vision_model: null,
    roles: { rag_answerer: "primary", query_rewriter: "primary", summarizer: "primary" },
  } as ModelAssignments,
}));

vi.mock("@/hooks/useModels", () => ({
  findModel: (models: ModelInfo[] | undefined, id: string | null) =>
    models?.find((model) => model.id === id) ?? null,
  useModels: () => ({ data: state.models }),
  useAssignments: () => ({
    data: state.assignments,
    isLoading: false,
    isError: false,
  }),
  useUpdateAssignments: () => ({
    mutate: vi.fn(),
    isPending: false,
    error: null,
  }),
}));

function model(overrides: Partial<ModelInfo> = {}): ModelInfo {
  return {
    id: "primary",
    label: "Primary model",
    provider: "openrouter",
    price_in_per_m: 0,
    price_out_per_m: 0,
    capabilities: [],
    model_id: "provider/model",
    base_url: "https://provider.example/v1",
    context_window: 128_000,
    ...overrides,
  };
}

describe("ModelMatrix — honest vision guidance", () => {
  beforeEach(() => {
    state.assignments = {
      default_model: "primary",
      vision_model: null,
      roles: { rag_answerer: "primary", query_rewriter: "primary", summarizer: "primary" },
    };
  });

  it("does not invent a text-only warning before a runnable primary exists", () => {
    state.models = [model({ id: "driver-unconfigured", base_url: null })];
    state.assignments.default_model = "driver-unconfigured";

    render(<ModelMatrix />);

    expect(screen.queryByText(/text-only|Vision hasn't been verified/i)).toBeNull();
  });

  it("honors a manual vision pin even when provider capabilities are stale", () => {
    state.models = [model({ vision: true, vision_status: "unknown" })];

    render(<ModelMatrix />);

    expect(screen.queryByText(/text-only|Vision hasn't been verified/i)).toBeNull();
  });

  it("shows an actionable warning only for an explicit text-only verdict", () => {
    state.models = [model({ vision: false, vision_status: "text-only" })];

    render(<ModelMatrix />);

    expect(screen.getByText(/Primary model is marked text-only/i)).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /Model library.*Image understanding/i }),
    ).toHaveAttribute("href", "#model-library");
  });

  it("targets an assigned visual model instead of warning about the primary", () => {
    state.models = [
      model({ vision: false, vision_status: "text-only" }),
      model({
        id: "visual",
        label: "Dedicated visual",
        model_id: "provider/visual",
        vision_status: "unknown",
      }),
    ];
    state.assignments.vision_model = "visual";

    render(<ModelMatrix />);

    expect(
      screen.getByText(/Vision hasn't been verified for Dedicated visual/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Primary model is marked text-only/i)).toBeNull();
  });

  it("states the verdict in one line and leaves the how-to to the link", () => {
    // UI-5: the status carried two extra clauses that only restated the link,
    // so an informational line ran three rows deep.
    state.models = [model({ vision_status: "unknown" })];

    render(<ModelMatrix />);

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent(
      /^Vision hasn't been verified for Primary model\.\s*Set it in Model library → Image understanding$/,
    );
  });
});
