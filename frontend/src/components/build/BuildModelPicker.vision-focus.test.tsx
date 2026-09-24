import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";

const save = vi.hoisted(() => vi.fn());
vi.mock("@/hooks/useDriverModels", () => ({
  useDriverModels: () => ({ data: { default: "text", models: [
    { id: "text", label: "Text driver", free: true, capabilities: ["tool_calling"] },
  ] } }),
  useLastSelectedModel: () => ({ data: null }),
}));
vi.mock("@/hooks/useModels", () => ({
  useModels: () => ({ data: [
    { id: "text", label: "Text driver", capabilities: ["tool_calling"] },
    { id: "observer", label: "Image observer", capabilities: ["vision"], price_in_per_m: 0 },
  ] }),
  useAssignments: () => ({ data: { vision_model: null } }),
  useUpdateAssignments: () => ({ mutate: save, isPending: false, error: null }),
}));
vi.mock("@/components/toastApi", () => ({ useToast: () => ({ show: vi.fn() }) }));

import { BuildModelPicker } from "./BuildModelPicker";

beforeEach(() => { save.mockReset(); });

it.each(["Escape", "fallback", "saved observer"])(
  "returns focus to the surviving driver picker after recommendation %s",
  async (dismissal) => {
    const user = userEvent.setup();
    const change = vi.fn();
    save.mockImplementation((_value, handlers) => handlers.onSuccess());
    render(<MemoryRouter><BuildModelPicker value="text" onChange={change} /></MemoryRouter>);
    const trigger = screen.getByRole("button", { name: "Choose the model that runs the agent" });
    await user.click(trigger);
    await user.click(screen.getByRole("menuitem", { name: "Text driver" }));
    const dialog = await screen.findByRole("dialog", { name: "Add visual inspection?" });
    expect(change).toHaveBeenCalledWith("text");
    if (dismissal === "Escape") await user.keyboard("{Escape}");
    else await user.click(within(dialog).getByRole("button", {
      name: dismissal === "fallback" ? "Keep text/DOM fallback" : /Image observer/,
    }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(trigger).toHaveFocus());
    if (dismissal === "saved observer") {
      expect(save).toHaveBeenCalledWith({ vision_model: "observer" }, expect.any(Object));
    } else expect(save).not.toHaveBeenCalled();
    await user.keyboard("{Enter}");
    expect(await screen.findByRole("menuitem", { name: "Text driver" })).toBeVisible();
  },
);
