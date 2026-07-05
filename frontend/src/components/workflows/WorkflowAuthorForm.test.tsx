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
  it("renders pickable builtin tools from the authoring context", async () => {
    renderForm();

    expect(await screen.findByText("file_read")).toBeInTheDocument();
    expect(screen.getByText("Read a file from the workspace.")).toBeInTheDocument();
    expect(screen.getByText("file_write")).toBeInTheDocument();
  });

  it("blocks submit when a writable MCP mount lacks the write policy", async () => {
    const user = userEvent.setup();
    renderForm();

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

  it("shows validation findings and simulation after authoring", async () => {
    const user = userEvent.setup();
    renderForm();

    await user.type(await screen.findByLabelText("Name"), "Fixture Author Workflow");
    await user.type(screen.getByLabelText("Card"), "Workflow authored in a form test.");
    await user.click(screen.getByRole("button", { name: "Create draft" }));

    expect(await screen.findByText("Validation findings")).toBeInTheDocument();
    expect(screen.getByText("No validation findings.")).toBeInTheDocument();
    expect(screen.getByText("Simulation result")).toBeInTheDocument();
  });
});
