import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { WorkflowAuthorForm } from "./WorkflowAuthorForm";

function renderForm() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <WorkflowAuthorForm />
    </QueryClientProvider>,
  );
}

describe("WorkflowAuthorForm", () => {
  it("renders the describe-first workflow creator", async () => {
    renderForm();

    expect(
      await screen.findByLabelText("What should this workflow do?"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Draft it →" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Edit details (Advanced)" })).toBeInTheDocument();
  });

  it("shows the recap actions after a description draft", async () => {
    const user = userEvent.setup();
    renderForm();

    await user.type(
      await screen.findByLabelText("What should this workflow do?"),
      "Summarize a topic into a markdown brief.",
    );
    await user.click(screen.getByRole("button", { name: "Draft it →" }));

    expect(await screen.findByText(/Drafts a workflow from this request/)).toBeInTheDocument();
    expect(screen.getByText("Drafted Workflow")).toBeInTheDocument();
    expect(screen.getByText("Uses")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create & enable" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit details" })).toBeInTheDocument();
  });

  it("keeps detailed authoring behind the advanced affordance", async () => {
    const user = userEvent.setup();
    renderForm();

    await user.click(await screen.findByRole("button", { name: "Edit details (Advanced)" }));
    expect(await screen.findByText("file_read")).toBeInTheDocument();
    expect(screen.getByText("Read a file from the workspace.")).toBeInTheDocument();
    expect(screen.getByText("file_write")).toBeInTheDocument();

    await user.type(await screen.findByLabelText("Name"), "Writable GitHub Workflow");
    await user.type(
      screen.getByLabelText("Card"),
      "Workflow that needs a writable connector mount.",
    );
    await user.click(screen.getByText("MCP mounts"));
    await user.click(screen.getByRole("button", { name: "Add MCP mount" }));
    await user.click(screen.getByLabelText("create_issue"));
    await user.click(screen.getByLabelText("Read-only mount"));

    expect(screen.getByRole("button", { name: "Create draft" })).toBeDisabled();
    expect(
      screen.getByText("Pick at least one tool per mount; writable mounts require Allows writes."),
    ).toBeInTheDocument();

    await user.click(screen.getByLabelText(/Allows writes/));
    expect(screen.getByRole("button", { name: "Create draft" })).toBeEnabled();
  });

  it("shows validation findings and simulation after advanced authoring", async () => {
    const user = userEvent.setup();
    renderForm();

    await user.click(await screen.findByRole("button", { name: "Edit details (Advanced)" }));
    await user.type(await screen.findByLabelText("Name"), "Fixture Author Workflow");
    await user.type(screen.getByLabelText("Card"), "Workflow authored in a form test.");
    await user.click(screen.getByRole("button", { name: "Create draft" }));

    expect(await screen.findByText("Configuration checks")).toBeInTheDocument();
    expect(screen.getByText("No issues found.")).toBeInTheDocument();
    expect(
      screen.getByText((_, element) => element?.textContent === "Configuration checks pass"),
    ).toBeInTheDocument();
  });
});
