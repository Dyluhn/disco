import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ImportProjectDialog } from "./ImportProjectDialog";

const { importMock } = vi.hoisted(() => ({ importMock: vi.fn() }));
vi.mock("@/api/projects", () => ({ importProject: importMock }));
vi.mock("@/api/liveness", () => ({ agentIsLive: () => true }));

async function openDialog() {
  const user = userEvent.setup();
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter><ImportProjectDialog /></MemoryRouter>
    </QueryClientProvider>,
  );
  await user.click(screen.getByRole("button", { name: "Import project" }));
  return user;
}

async function expectSelected(name: string) {
  const selected = screen.getByRole("tab", { name });
  await waitFor(() => expect(selected).toHaveFocus());
  expect(selected).toHaveAttribute("aria-selected", "true");
  expect(selected).toHaveAttribute("tabindex", "0");
  for (const tab of screen.getAllByRole("tab")) {
    if (tab !== selected) {
      expect(tab).toHaveAttribute("aria-selected", "false");
      expect(tab).toHaveAttribute("tabindex", "-1");
    }
  }
  const panel = screen.getByRole("tabpanel", { name });
  expect(screen.getAllByRole("tabpanel")).toHaveLength(1);
  expect(selected).toHaveAttribute("aria-controls", panel.id);
  expect(panel).toHaveAttribute("aria-labelledby", selected.id);
}

beforeEach(() => {
  importMock.mockReset();
  importMock.mockResolvedValue({ conversation_id: "imported-conversation" });
});

describe("ImportProjectDialog keyboard tabs", () => {
  it.each(["Escape", "Cancel", "Close import project"])("%s discards the ZIP on reopen and submits only a fresh selection", async (method) => {
    const user = await openDialog();
    const oldZip = new File(["old bytes"], "old.zip", { type: "application/zip" });
    await user.upload(screen.getByLabelText("Project zip file"), oldZip);
    expect(screen.getByRole("button", { name: "Import" })).toBeEnabled();
    if (method === "Escape") await user.keyboard("{Escape}");
    else await user.click(screen.getByRole("button", { name: method }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Import project" }));
    const picker = screen.getByLabelText("Project zip file") as HTMLInputElement;
    expect(picker.files).toHaveLength(0);
    expect(screen.getByRole("button", { name: "Import" })).toBeDisabled();
    expect(importMock).not.toHaveBeenCalled();
    const freshZip = new File(["fresh bytes"], "fresh.zip", { type: "application/zip" });
    await user.upload(picker, freshZip);
    await user.click(screen.getByRole("button", { name: "Import" }));
    await waitFor(() => expect(importMock).toHaveBeenCalledExactlyOnceWith({ kind: "zip", file: freshZip }));
    expect(importMock.mock.calls[0][0].file).toBe(freshZip);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    // Success follows the same close boundary even if navigation keeps this component mounted.
    await user.click(screen.getByRole("button", { name: "Import project" }));
    expect((screen.getByLabelText("Project zip file") as HTMLInputElement).files).toHaveLength(0);
    expect(screen.getByRole("button", { name: "Import" })).toBeDisabled();
  });

  it("keeps the selected ZIP after a failed import so retry uses the same file", async () => {
    importMock.mockRejectedValueOnce(new Error("Archive validation failed"));
    const user = await openDialog();
    const zip = new File(["retry bytes"], "retry.zip", { type: "application/zip" });
    await user.upload(screen.getByLabelText("Project zip file"), zip);
    await user.click(screen.getByRole("button", { name: "Import" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Archive validation failed");
    expect((screen.getByLabelText("Project zip file") as HTMLInputElement).files?.[0]).toBe(zip);
    expect(screen.getByRole("button", { name: "Import" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Import" }));
    await waitFor(() => expect(importMock).toHaveBeenCalledTimes(2));
    expect(importMock.mock.calls[0][0].file).toBe(zip);
    expect(importMock.mock.calls[1][0].file).toBe(zip);
  });

  it("preserves path and Git drafts when the dialog closes", async () => {
    const user = await openDialog();
    await user.click(screen.getByRole("tab", { name: "Local folder path" }));
    await user.type(screen.getByRole("textbox", { name: "Local folder path" }), "/tmp/project-draft");
    await user.click(screen.getByRole("tab", { name: "Git URL" }));
    await user.type(screen.getByRole("textbox", { name: "Git URL" }), "https://example.test/draft.git");
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Import project" }));
    expect(screen.getByRole("textbox", { name: "Git URL" })).toHaveValue("https://example.test/draft.git");
    await user.click(screen.getByRole("tab", { name: "Local folder path" }));
    expect(screen.getByRole("textbox", { name: "Local folder path" })).toHaveValue("/tmp/project-draft");
    expect(importMock).not.toHaveBeenCalled();
  });

  it("moves selection and focus together with arrows, wraparound, Home and End", async () => {
    const user = await openDialog();
    expect(screen.getByRole("tab", { name: "Upload zip" })).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("tablist")).toHaveAttribute("tabindex", "-1");
    expect(screen.getByRole("tab", { name: "Local folder path" })).toHaveAttribute("tabindex", "-1");
    expect(screen.getByRole("tab", { name: "Git URL" })).toHaveAttribute("tabindex", "-1");
    await user.click(screen.getByRole("tab", { name: "Upload zip" }));
    await expectSelected("Upload zip");
    for (const [key, name] of [
      ["{ArrowRight}", "Local folder path"], ["{ArrowRight}", "Git URL"],
      ["{ArrowRight}", "Upload zip"], ["{ArrowLeft}", "Git URL"],
      ["{Home}", "Upload zip"], ["{End}", "Git URL"], ["{Enter} ", "Git URL"],
    ]) {
      await user.keyboard(key);
      await expectSelected(name);
    }
    expect(importMock).not.toHaveBeenCalled();
  });

  it.each([
    ["Upload zip", "Project zip file"],
    ["Local folder path", "Local folder path"],
    ["Git URL", "Git URL"],
  ])("Tab leaves %s directly for its input and Shift+Tab returns", async (tab, label) => {
    const user = await openDialog();
    await user.click(screen.getByRole("tab", { name: tab }));
    await expectSelected(tab);
    await user.tab();
    expect(screen.getByLabelText(label, { selector: "input" })).toHaveFocus();
    await user.tab({ shift: true });
    await expectSelected(tab);
    expect(importMock).not.toHaveBeenCalled();
  });

  it("preserves all drafts including the native file selection across keyboard navigation", async () => {
    const user = await openDialog();
    const zip = new File(["fixture zip"], "project.zip", { type: "application/zip" });
    await user.upload(screen.getByLabelText("Project zip file"), zip);
    await user.click(screen.getByRole("tab", { name: "Upload zip" }));
    await user.keyboard("{ArrowRight}");
    await expectSelected("Local folder path");
    await user.tab();
    await user.type(screen.getByRole("textbox", { name: "Local folder path" }), "/tmp/project-draft");
    await user.tab({ shift: true });
    await user.keyboard("{ArrowRight}");
    await expectSelected("Git URL");
    await user.tab();
    await user.type(screen.getByRole("textbox", { name: "Git URL" }), "https://example.test/project.git");
    await user.tab({ shift: true });
    await user.keyboard("{ArrowRight}");
    await expectSelected("Upload zip");
    expect((screen.getByLabelText("Project zip file") as HTMLInputElement).files?.[0]).toBe(zip);
    await user.keyboard("{ArrowRight}");
    await expectSelected("Local folder path");
    expect(screen.getByRole("textbox", { name: "Local folder path" })).toHaveValue("/tmp/project-draft");
    await user.keyboard("{ArrowRight}");
    await expectSelected("Git URL");
    expect(screen.getByRole("textbox", { name: "Git URL" })).toHaveValue("https://example.test/project.git");
    expect(importMock).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Import" }));
    await waitFor(() => expect(importMock).toHaveBeenCalledExactlyOnceWith({
      kind: "git", gitUrl: "https://example.test/project.git",
    }));
  });

  it.each(["Escape", "Cancel"])("%s closes and returns focus to the trigger", async (method) => {
    const user = await openDialog();
    const trigger = screen.getByLabelText("Import project", { selector: "button" });
    await user.click(screen.getByRole("tab", { name: "Git URL" }));
    await user.tab();
    await user.type(screen.getByRole("textbox", { name: "Git URL" }), "https://example.test/draft.git");
    if (method === "Escape") await user.keyboard("{Escape}");
    else await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
    expect(importMock).not.toHaveBeenCalled();
  });
});
