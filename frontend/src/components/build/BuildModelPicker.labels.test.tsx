import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";

vi.mock("@/hooks/useDriverModels", () => ({
  useDriverModels: () => ({ data: { default: "primary", models: [
    { id: "primary", label: "same-model", free: true, pricing_mode: "free" },
    { id: "alternate", label: "same-model", free: true, pricing_mode: "free" },
  ] } }),
  useLastSelectedModel: () => ({ data: null }),
}));
vi.mock("@/hooks/useModels", () => ({
  useModels: () => ({ data: [
    { id: "primary", label: "Primary endpoint — same-model", capabilities: [] },
    { id: "alternate", label: "Alternate endpoint — same-model", capabilities: [] },
  ] }),
  useAssignments: () => ({ data: { vision_model: "observer" } }),
  useUpdateAssignments: () => ({ mutate: vi.fn() }),
}));
vi.mock("@/components/toastApi", () => ({ useToast: () => ({ show: vi.fn() }) }));

import { BuildModelPicker } from "./BuildModelPicker";

it("distinguishes identical API model IDs and selects the intended catalogue entry", async () => {
  const user = userEvent.setup();
  const change = vi.fn();
  render(<MemoryRouter><BuildModelPicker value="primary" onChange={change} /></MemoryRouter>);
  await user.click(screen.getByRole("button", { name: "Choose the model that runs the agent" }));
  expect(screen.getByRole("menuitem", { name: "Primary endpoint — same-model" })).toBeVisible();
  await user.click(screen.getByRole("menuitem", { name: "Alternate endpoint — same-model" }));
  expect(change).toHaveBeenCalledWith("alternate");
});
