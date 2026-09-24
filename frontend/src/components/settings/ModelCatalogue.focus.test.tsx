import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MODEL_CATALOGUE } from "@/fixtures/models";
import { ModelCatalogue } from "./ModelCatalogue";

const hooks = vi.hoisted(() => ({
  create: vi.fn(), update: vi.fn(), models: [] as typeof MODEL_CATALOGUE,
}));
vi.mock("@/hooks/useModels", () => ({
  useModels: () => ({ data: hooks.models }),
  useCreateModel: () => ({ mutateAsync: hooks.create, isPending: false, error: null }),
  useUpdateModel: () => ({ mutateAsync: hooks.update, isPending: false, error: null }),
  useDeleteModel: () => ({ mutate: vi.fn(), error: null }),
}));
vi.mock("@/hooks/useSecrets", () => ({ useSecrets: () => ({ data: { names: [] } }) }));

beforeEach(() => {
  hooks.create.mockReset().mockResolvedValue({});
  hooks.update.mockReset().mockResolvedValue({});
  hooks.models = MODEL_CATALOGUE.slice(0, 2);
});
async function openLibrary() {
  const user = userEvent.setup();
  render(<ModelCatalogue />);
  await user.click(screen.getByText("Model library"));
  return user;
}

describe("controlled model form returns focus to its real opener", () => {
  it("restores Add model after keyboard dismissal and supports reopening and Cancel", async () => {
    const user = await openLibrary();
    const opener = screen.getByRole("button", { name: "Add model" });
    opener.focus();
    await user.keyboard("{Enter}");
    const id = screen.getByRole("textbox", { name: "Catalogue id" });
    await waitFor(() => expect(id).toHaveFocus());
    await user.tab();
    expect(screen.getByRole("textbox", { name: "Model id (sent to the API)" })).toHaveFocus();
    await user.tab({ shift: true });
    expect(id).toHaveFocus();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(opener).toHaveFocus());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.keyboard("{Enter}");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(opener).toHaveFocus());
    expect(hooks.create).not.toHaveBeenCalled();
  });

  it("restores each specific Edit opener instead of Add or a different model row", async () => {
    const user = await openLibrary();
    for (const model of hooks.models) {
      const opener = screen.getByRole("button", { name: `Edit ${model.id}` });
      await user.click(opener);
      expect(screen.getByRole("dialog")).toHaveAccessibleName(`Edit ${model.id}`);
      await user.click(screen.getByRole("button", { name: "Cancel" }));
      await waitFor(() => expect(opener).toHaveFocus());
    }
  });

  it("restores the Edit opener after a successful save and preserves submission", async () => {
    const user = await openLibrary();
    const model = hooks.models[0];
    const opener = screen.getByRole("button", { name: `Edit ${model.id}` });
    await user.click(opener);
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(opener).toHaveFocus());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(hooks.update).toHaveBeenCalledWith(expect.objectContaining({ id: model.id }));
  });

  it("does not move focus outside a form when saving fails", async () => {
    hooks.update.mockRejectedValueOnce(new Error("Save rejected"));
    const user = await openLibrary();
    await user.click(screen.getByRole("button", { name: `Edit ${hooks.models[0].id}` }));
    const save = screen.getByRole("button", { name: "Save changes" });
    await user.click(save);
    await waitFor(() => expect(hooks.update).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(save).toHaveFocus();
  });

  it("preserves Radix Trigger focus restoration for the independent Remove confirmation", async () => {
    const user = await openLibrary();
    const opener = screen.getByRole("button", { name: `Remove ${hooks.models[0].id}` });
    await user.click(opener);
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("does not focus a detached Edit opener when its row disappears", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<ModelCatalogue />);
    await user.click(screen.getByText("Model library"));
    const opener = screen.getByRole("button", { name: `Edit ${hooks.models[0].id}` });
    await user.click(opener);
    const focus = vi.spyOn(opener, "focus");
    hooks.models = [];
    rerender(<ModelCatalogue />);
    expect(opener.isConnected).toBe(false);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(focus).not.toHaveBeenCalled();
    focus.mockRestore();
  });
});
