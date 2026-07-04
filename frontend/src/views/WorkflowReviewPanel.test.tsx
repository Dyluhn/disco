import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { WorkflowReviewPanel } from "./WorkflowReviewPanel";

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <WorkflowReviewPanel />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("WorkflowReviewPanel", () => {
  it("blocks approval when validation findings include an error", async () => {
    renderPanel();

    expect(await screen.findByText("Fixture Blocked Workflow")).toBeInTheDocument();
    expect(screen.getByText("unknown workflow builtin tool: missing_tool")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Approve Fixture Blocked Workflow" }),
    ).toBeDisabled();
  });

  it("renders the real compiled tool, MCP, skill, and policy surface payloads", async () => {
    renderPanel();

    expect(await screen.findByText("Fixture Valid Workflow")).toBeInTheDocument();
    expect(screen.getByLabelText("Fixture Valid Workflow tool surface")).toHaveTextContent(
      "file_read",
    );
    expect(screen.getByLabelText("Fixture Valid Workflow tool surface")).toHaveTextContent(
      "parameters_schema",
    );
    expect(screen.getByLabelText("Fixture Valid Workflow MCP mounts")).toHaveTextContent("[]");
    expect(screen.getByLabelText("Fixture Valid Workflow skills")).toHaveTextContent("[]");
    expect(screen.getByLabelText("Fixture Valid Workflow policies")).toHaveTextContent(
      "allows_writes",
    );
  });
});
