import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";

vi.mock("@/hooks/useDriverModels", () => ({
  useDriverModels: () => ({ data: { default: "primary", models: [
    { id: "primary", label: "same-model", free: true, pricing_mode: "free" },
    { id: "alternate", label: "same-model", free: true, pricing_mode: "free" },
  ] } }),
  useLastSelectedModel: () => ({ data: "alternate" }),
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

it.each(["build", "agent"] as const)(
  "pins the explicit default row despite a different sticky pick (%s)",
  async (surface) => {
    const user = userEvent.setup();
    const selected = vi.fn();
    function Composer() {
      const [value, setValue] = useState<string | null>(null);
      return <BuildModelPicker value={value} surface={surface} onChange={(id) => {
        selected(id);
        setValue(id);
      }} />;
    }
    render(<MemoryRouter><Composer /></MemoryRouter>);
    const trigger = screen.getByRole("button", { name: "Choose the model that runs the agent" });
    expect(trigger).toHaveTextContent("Alternate endpoint — same-model");
    await user.click(trigger);
    await user.click(screen.getByRole("menuitem", { name: "Primary endpoint — same-model" }));
    expect(selected).toHaveBeenCalledWith("primary");
    expect(trigger).toHaveTextContent("Primary endpoint — same-model");
    expect(trigger).not.toHaveTextContent("Alternate endpoint");
  },
);
