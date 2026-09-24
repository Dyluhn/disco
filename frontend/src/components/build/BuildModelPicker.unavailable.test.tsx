import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ empty: false, show: vi.fn() }));
vi.mock("@/hooks/useDriverModels", () => ({
  useDriverModels: () => ({ data: { default: "available", models: mocks.empty ? [] : [
    { id: "available", label: "Available choice", free: false, pricing_mode: "metered", capabilities: [] },
  ] } }),
  useLastSelectedModel: () => ({ data: "available" }),
}));
vi.mock("@/hooks/useModels", () => ({
  useModels: () => ({ data: [{ id: "available", label: "Available choice", capabilities: [] }] }),
  useAssignments: () => ({ data: { vision_model: "observer" } }),
  useUpdateAssignments: () => ({ mutate: vi.fn() }),
}));
vi.mock("@/components/toastApi", () => ({ useToast: () => ({ show: mocks.show }) }));

import { BuildModelPicker } from "./BuildModelPicker";

beforeEach(() => { mocks.empty = false; mocks.show.mockClear(); });

it.each([false, true])("exposes a removed pinned model without a default fallback (empty catalogue=%s)", (empty) => {
  mocks.empty = empty;
  const change = vi.fn();
  render(<MemoryRouter><BuildModelPicker value="removed-choice" onChange={change} /></MemoryRouter>);
  const trigger = screen.getByRole("button", { name: "Choose the model that runs the agent" });
  expect(trigger).toHaveTextContent("Unavailable model: removed-choice");
  expect(trigger).not.toHaveTextContent("Default model");
  expect(trigger).toHaveAttribute("title", expect.stringContaining("Choose another model"));
  expect(trigger).toBeEnabled();
  expect(change).not.toHaveBeenCalled();
});

it("requires an explicit available replacement and describes it as the next request's choice", async () => {
  const user = userEvent.setup();
  const change = vi.fn();
  function Picker() {
    const [value, setValue] = useState<string | null>("removed-choice");
    return <BuildModelPicker value={value} onChange={(id) => { change(id); setValue(id); }} />;
  }
  render(<MemoryRouter><Picker /></MemoryRouter>);
  const trigger = screen.getByRole("button", { name: "Choose the model that runs the agent" });
  await user.click(trigger);
  await user.click(screen.getByRole("menuitem", { name: "Available choice" }));
  expect(change).toHaveBeenCalledExactlyOnceWith("available");
  expect(trigger).toHaveTextContent("Available choice");
  expect(trigger).not.toHaveTextContent("Unavailable model");
  expect(trigger).toHaveAttribute("title", "Model changes apply when you start or continue a task.");
  expect(mocks.show).toHaveBeenCalledWith(expect.objectContaining({
    title: "Selected Available choice for your next request",
    body: expect.stringContaining("Applied when you start or continue a task."),
  }));
});
