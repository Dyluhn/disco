import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ModelInfo } from "@/types/models";
import { ModelCatalogue } from "./ModelCatalogue";

const hooks = vi.hoisted(() => ({
  create: vi.fn(),
  update: vi.fn(),
  remove: vi.fn(),
  models: [] as ModelInfo[],
  createPending: false,
  updatePending: false,
}));

vi.mock("@/hooks/useModels", () => ({
  useModels: () => ({ data: hooks.models }),
  useCreateModel: () => ({
    mutateAsync: hooks.create,
    isPending: hooks.createPending,
    error: null,
  }),
  useUpdateModel: () => ({
    mutateAsync: hooks.update,
    isPending: hooks.updatePending,
    error: null,
  }),
  useDeleteModel: () => ({ mutate: hooks.remove, error: null }),
}));

vi.mock("@/hooks/useSecrets", () => ({
  useSecrets: () => ({ data: { names: [] } }),
}));

function makeModel(id: string, modelId: string): ModelInfo {
  return {
    id,
    label: id,
    provider: "local",
    price_in_per_m: 0,
    price_out_per_m: 0,
    capabilities: [],
    vision: null,
    note: "test model",
    model_id: modelId,
    base_url: "http://localhost:8080/v1",
    api_key_env: "",
    requires_api_key: false,
    context_window: 8192,
    max_output_tokens: null,
    quantization: "",
  };
}

const ALPHA = () => makeModel("catalogue-alpha", "alpha-70b.gguf");
const BETA = () => makeModel("catalogue-beta", "beta-8b.gguf");

beforeEach(() => {
  hooks.create.mockReset().mockResolvedValue({});
  hooks.update.mockReset().mockResolvedValue({});
  hooks.remove.mockReset();
  hooks.createPending = false;
  hooks.updatePending = false;
  hooks.models = [ALPHA(), BETA()];
});

afterEach(() => {
  cleanup();
});

async function openLibrary() {
  const user = userEvent.setup();
  render(<ModelCatalogue />);
  await user.click(screen.getByText("Model library"));
  // Row openers prove the collapsed <details> is expanded; without this the
  // focus assertions below would fail for the wrong reason.
  await screen.findByRole("button", { name: "Edit catalogue-alpha" });
  return user;
}

function addOpener() {
  return screen.getByRole("button", { name: "Add model" });
}

function dialog() {
  return screen.getByRole("dialog");
}

describe("ModelCatalogue focus", () => {
  it("Add dialog initially focuses Catalogue id", async () => {
    const user = await openLibrary();
    await user.click(addOpener());
    await waitFor(() =>
      expect(
        within(dialog()).getByRole("textbox", { name: "Catalogue id" }),
      ).toHaveFocus(),
    );
  });

  it("Edit dialog initially focuses first editable Model id", async () => {
    const user = await openLibrary();
    await user.click(
      screen.getByRole("button", { name: "Edit catalogue-alpha" }),
    );
    const box = dialog();
    expect(box).toHaveAccessibleName("Edit catalogue-alpha");
    expect(
      within(box).getByRole("textbox", { name: "Catalogue id" }),
    ).toBeDisabled();
    await waitFor(() =>
      expect(
        within(box).getByRole("textbox", {
          name: "Model id (sent to the API)",
        }),
      ).toHaveFocus(),
    );
  });

  it("Add Cancel restores the Add opener", async () => {
    const user = await openLibrary();
    const opener = addOpener();
    await user.click(opener);
    await user.click(
      within(dialog()).getByRole("button", { name: "Cancel" }),
    );
    await waitFor(() => expect(opener).toHaveFocus());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(hooks.create).not.toHaveBeenCalled();
  });

  it("Add Escape restores the Add opener", async () => {
    const user = await openLibrary();
    const opener = addOpener();
    await user.click(opener);
    await screen.findByRole("dialog");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(opener).toHaveFocus());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("Edit Cancel restores each exact row opener", async () => {
    const user = await openLibrary();
    for (const id of ["catalogue-alpha", "catalogue-beta"]) {
      const opener = screen.getByRole("button", { name: `Edit ${id}` });
      await user.click(opener);
      expect(dialog()).toHaveAccessibleName(`Edit ${id}`);
      await user.click(
        within(dialog()).getByRole("button", { name: "Cancel" }),
      );
      await waitFor(() => expect(opener).toHaveFocus());
    }
  });

  it("Edit Escape restores the exact row opener", async () => {
    const user = await openLibrary();
    const opener = screen.getByRole("button", {
      name: "Edit catalogue-beta",
    });
    await user.click(opener);
    await screen.findByRole("dialog");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(opener).toHaveFocus());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("successful Add save restores the Add opener", async () => {
    const user = await openLibrary();
    const opener = addOpener();
    await user.click(opener);
    const box = dialog();
    await user.type(
      within(box).getByRole("textbox", { name: "Catalogue id" }),
      "catalogue-gamma",
    );
    await user.type(
      within(box).getByRole("textbox", { name: "Model id (sent to the API)" }),
      "gamma-70b.gguf",
    );
    await user.click(
      within(box).getByRole("button", { name: "Add model" }),
    );
    await waitFor(() => expect(hooks.create).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(opener).toHaveFocus());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("successful Edit save restores the Edit opener", async () => {
    const user = await openLibrary();
    const opener = screen.getByRole("button", {
      name: "Edit catalogue-alpha",
    });
    await user.click(opener);
    await user.click(
      within(dialog()).getByRole("button", { name: "Save changes" }),
    );
    await waitFor(() => expect(hooks.update).toHaveBeenCalledTimes(1));
    expect(hooks.update).toHaveBeenCalledWith(
      expect.objectContaining({ id: "catalogue-alpha" }),
    );
    await waitFor(() => expect(opener).toHaveFocus());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("unsuccessful Edit save retains dialog, draft, and focus", async () => {
    hooks.update.mockRejectedValueOnce(new Error("Save rejected"));
    const user = await openLibrary();
    await user.click(
      screen.getByRole("button", { name: "Edit catalogue-alpha" }),
    );
    const box = dialog();
    const modelId = within(box).getByRole("textbox", {
      name: "Model id (sent to the API)",
    });
    await user.clear(modelId);
    await user.type(modelId, "alpha-draft.gguf");
    const save = within(box).getByRole("button", { name: "Save changes" });
    await user.click(save);
    await waitFor(() => expect(hooks.update).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(modelId).toHaveValue("alpha-draft.gguf");
    expect(dialog().contains(document.activeElement)).toBe(true);
  });

  it("pending save does not prematurely close the dialog", async () => {
    hooks.update.mockImplementationOnce(() => new Promise(() => {}));
    const user = await openLibrary();
    const opener = screen.getByRole("button", {
      name: "Edit catalogue-alpha",
    });
    await user.click(opener);
    await user.click(
      within(dialog()).getByRole("button", { name: "Save changes" }),
    );
    await waitFor(() => expect(hooks.update).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(opener).not.toHaveFocus();
  });

  it("Remove confirmation Cancel restores the trash opener and preserves the model", async () => {
    const user = await openLibrary();
    const opener = screen.getByRole("button", {
      name: "Remove catalogue-alpha",
    });
    await user.click(opener);
    await user.click(
      within(dialog()).getByRole("button", { name: "Cancel" }),
    );
    await waitFor(() => expect(opener).toHaveFocus());
    expect(hooks.remove).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "Edit catalogue-alpha" }),
    ).toBeInTheDocument();
  });

  it("Remove confirmation Escape restores the trash opener", async () => {
    const user = await openLibrary();
    const opener = screen.getByRole("button", {
      name: "Remove catalogue-beta",
    });
    await user.click(opener);
    await screen.findByRole("dialog");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("vanished row opener falls back to the stable Add button", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<ModelCatalogue />);
    await user.click(screen.getByText("Model library"));
    const opener = await screen.findByRole("button", {
      name: "Edit catalogue-alpha",
    });
    await user.click(opener);
    await screen.findByRole("dialog");
    hooks.models = [BETA()];
    rerender(<ModelCatalogue />);
    // Simulate the row disappearing externally while the dialog is open.
    expect(opener.isConnected).toBe(false);
    await user.keyboard("{Escape}");
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    const fallback = screen.getByRole("button", { name: "Add model" });
    expect(fallback.isConnected).toBe(true);
    await waitFor(() => expect(fallback).toHaveFocus());
    expect(document.activeElement).not.toBe(document.body);
  });
});

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
