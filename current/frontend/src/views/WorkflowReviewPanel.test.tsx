import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

async function card(name: string): Promise<HTMLElement> {
  const title = await screen.findByText(name);
  const article = title.closest("article");
  if (!article) throw new Error(`workflow card not found: ${name}`);
  return article;
}

describe("WorkflowReviewPanel", () => {
  it("describes all workflow instances, not only drafts", async () => {
    renderPanel();

    expect(await screen.findByRole("heading", { name: "Workflows" })).toBeInTheDocument();
    expect(screen.getByText("Reusable capabilities your Agent can use in any chat.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New workflow" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Available to your Agent" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Needs setup" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Off" })).toBeInTheDocument();
    expect(screen.getAllByText("Ready").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Needs setup").length).toBeGreaterThanOrEqual(1);
  });

  it("blocks approval when validation findings include an error", async () => {
    renderPanel();

    const valid = await card("Fixture Valid Workflow");
    const blocked = await card("Fixture Blocked Workflow");

    // Default view: name, purpose, concise readiness, and one Run surface.
    expect(within(valid).getByText(/Valid workflow review fixture/i)).toBeInTheDocument();
    expect(within(valid).getByText("Ready")).toBeInTheDocument();
    expect(within(valid).getByText("Required inputs")).toBeInTheDocument();
    expect(within(valid).getByLabelText("Workflow parameter query")).toHaveValue("fixture");
    expect(within(valid).getByRole("combobox", { name: /Agent conversation/i })).toBeInTheDocument();
    expect(within(valid).getByRole("button", { name: "Run" })).toBeInTheDocument();
    expect(within(blocked).getByText("Needs setup")).toBeInTheDocument();
    expect(within(valid).queryByText("Built-in")).not.toBeInTheDocument();
    expect(within(valid).queryByText("filesystem")).not.toBeInTheDocument();

    // Raw identifiers, validation messages, cron controls, and the scheduler's
    // default every-minute state must not leak into the default accessible/DOM view.
    expect(within(valid).queryByText("wf_fixture_valid")).not.toBeInTheDocument();
    expect(within(valid).queryByText("sha256:fixture-valid-definition")).not.toBeInTheDocument();
    expect(within(valid).queryByRole("button", { name: /Save schedule/i })).not.toBeInTheDocument();
    expect(within(valid).queryByLabelText(/Cron schedule/i)).not.toBeInTheDocument();
    expect(within(blocked).queryByText("unknown workflow builtin tool: missing_tool")).not.toBeInTheDocument();
    expect(within(blocked).getByRole("button", { name: "Set up Fixture Blocked Workflow" })).toBeDisabled();
    expect(within(valid).getByRole("switch", { name: "Enable Fixture Valid Workflow" })).toBeChecked();
    expect(within(blocked).getByRole("switch", { name: "Enable Fixture Blocked Workflow" })).toBeChecked();
  });

  it("renders the real compiled tool, MCP, skill, and policy surface payloads", async () => {
    const user = userEvent.setup();
    renderPanel();
    const valid = await card("Fixture Valid Workflow");
    const blocked = await card("Fixture Blocked Workflow");

    const advanced = within(valid).getByRole("button", { name: /Advanced details/i });
    expect(advanced).toHaveAttribute("aria-expanded", "false");
    await user.click(advanced);
    await waitFor(() => expect(advanced).toHaveAttribute("aria-expanded", "true"));
    expect(within(valid).getByText(/Instance ID: wf_fixture_valid/)).toBeInTheDocument();
    expect(within(valid).getByText(/Definition digest: sha256:fixture-valid-definition/)).toBeInTheDocument();
    expect(within(valid).getByLabelText("Fixture Valid Workflow tool surface")).toHaveTextContent(
      "parameters_schema",
    );

    const blockedAdvanced = within(blocked).getByRole("button", { name: /Advanced details/i });
    await user.click(blockedAdvanced);
    expect(within(blocked).getByText("unknown workflow builtin tool: missing_tool")).toBeInTheDocument();
    expect(within(blocked).getByText("Validation findings")).toBeInTheDocument();
  });

  it("keeps scheduling collapsed and exposes the existing scheduler in plain language when opened", async () => {
    const user = userEvent.setup();
    renderPanel();
    const valid = await card("Fixture Valid Workflow");

    const advanced = within(valid).getByRole("button", { name: /Advanced details/i });
    expect(advanced).toHaveAttribute("aria-expanded", "false");
    await user.click(advanced);
    expect(advanced).toHaveAttribute("aria-expanded", "true");
    const scheduling = within(valid).getByRole("button", { name: /^Scheduling/i });
    expect(scheduling).toHaveAttribute("aria-expanded", "false");
    expect(within(valid).queryByLabelText("Schedule for Fixture Valid Workflow")).not.toBeInTheDocument();
    expect(within(valid).queryByRole("button", { name: /Save schedule/i })).not.toBeInTheDocument();

    await user.click(scheduling);
    expect(scheduling).toHaveAttribute("aria-expanded", "true");
    const scheduleInput = within(valid).getByLabelText("Schedule for Fixture Valid Workflow");
    expect(scheduleInput).toHaveAttribute("placeholder", "e.g. every weekday or daily at 9am");
    expect(within(valid).getByRole("button", { name: /Save schedule/i })).toBeDisabled();

    await user.type(scheduleInput, "every weekday");
    expect(within(valid).getByRole("button", { name: /Save schedule/i })).toBeEnabled();
    await user.click(within(valid).getByRole("button", { name: /Save schedule/i }));
    expect(await within(valid).findByText(/every weekday at 9:00 AM/i)).toBeInTheDocument();
  });

  it("round-trips required input and the selected Agent conversation through Run", async () => {
    const user = userEvent.setup();
    renderPanel();
    const valid = await card("Fixture Valid Workflow");
    const input = within(valid).getByLabelText("Workflow parameter query");

    await user.clear(input);
    await user.type(input, "latest brief");
    expect(input).toHaveValue("latest brief");
    await user.click(within(valid).getByRole("button", { name: "Run" }));
    expect(await within(valid).findByRole("link", { name: "Open Agent conversation" })).toBeInTheDocument();
  });

  it("toggles an approved workflow and refreshes its truthful state", async () => {
    const user = userEvent.setup();
    renderPanel();
    const valid = await card("Fixture Valid Workflow");
    const toggle = within(valid).getByRole("switch", { name: "Enable Fixture Valid Workflow" });

    await user.click(toggle);

    await waitFor(() =>
      expect(screen.getByText("Fixture Valid Workflow").closest("article")).toHaveAttribute(
        "data-workflow-state",
        "off",
      ),
    );
    const updated = await card("Fixture Valid Workflow");
    expect(within(updated).getByRole("switch", { name: "Enable Fixture Valid Workflow" })).not.toBeChecked();
    expect(screen.getByRole("heading", { name: "Off" })).toBeInTheDocument();
  });

  it("always allows an enabled workflow to be turned off when setup becomes invalid", async () => {
    const user = userEvent.setup();
    renderPanel();
    const blocked = await card("Fixture Blocked Workflow");
    const toggle = within(blocked).getByRole("switch", {
      name: "Enable Fixture Blocked Workflow",
    });

    expect(toggle).toBeEnabled();
    await user.click(toggle);

    await waitFor(() =>
      expect(screen.getByText("Fixture Blocked Workflow").closest("article")).toHaveAttribute(
        "data-workflow-state",
        "off",
      ),
    );
    const updated = await card("Fixture Blocked Workflow");
    expect(within(updated).getByRole("switch", { name: "Enable Fixture Blocked Workflow" })).not.toBeChecked();
  });
});
