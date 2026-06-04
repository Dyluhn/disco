import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";
import { SettingsView } from "./SettingsView";

function withQuery(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("Settings — model-assignment matrix", () => {
  it("shows the absolute/manual story with a default primary + every role, cost-legible", async () => {
    withQuery(<SettingsView />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Choose model for Default primary/i })).toBeInTheDocument(),
    );
    // The absolute, no-automatic-routing story is stated.
    expect(screen.getByText(/no automatic routing/i)).toBeInTheDocument();
    // Every non-driver role has its own selector.
    for (const role of ["RAG answerer", "Query rewriter", "Summarizer", "NLI verifier"]) {
      expect(
        screen.getByRole("button", { name: new RegExp(`Choose model for ${role}`, "i") }),
      ).toBeInTheDocument();
    }
    // Cost is visible per assignment (all local → Free).
    expect(screen.getAllByText("Free").length).toBeGreaterThan(0);
  });

  it("changes a role assignment (the picker is enabled and the row reflects it)", async () => {
    // Assignments are now wired end-to-end (ConfigStore -> agent-server routing),
    // so the picker is operable, not a flagged dead control.
    const user = userEvent.setup();
    withQuery(<SettingsView />);
    const ragTrigger = await screen.findByRole("button", {
      name: /Choose model for RAG answerer/i,
    });
    expect(ragTrigger).toBeEnabled();
    expect(ragTrigger).toHaveTextContent(/Rag Local/i);

    await user.click(ragTrigger);
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByText(/Summarizer Local/i));

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /Choose model for RAG answerer/i }),
      ).toHaveTextContent(/Summarizer Local/i),
    );
  });
});

describe("Settings — model catalogue (CRUD)", () => {
  it("adds a new model via the form and it appears in the catalogue", async () => {
    const user = userEvent.setup();
    withQuery(<SettingsView />);
    await screen.findByText("Catalogue");

    await user.click(screen.getByRole("button", { name: /Add model/i }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByPlaceholderText("my-llama"), "test-model");
    await user.type(within(dialog).getByPlaceholderText(/llama-3.3-70b/i), "test.gguf");
    await user.click(within(dialog).getByRole("button", { name: /^Add model$/i }));

    // the new model shows in the catalogue list (label derived like the backend)
    await waitFor(() =>
      expect(screen.getByText(/Test Model — test/i)).toBeInTheDocument(),
    );
  });
});

describe("Settings — skills + MCP scaffolds", () => {
  it("lists skills with disabled toggles, honestly flagged not wired", async () => {
    withQuery(<SettingsView />);
    const sw = await screen.findByRole("switch", { name: /Enable Web research/i });
    // The toggle does not control anything yet, so it must be disabled, not fake.
    expect(sw).toBeDisabled();
    // Red markers present (the badge + the specific NotWired explanation).
    expect(screen.getAllByText(/not wired/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/not implemented at all/i)).toBeInTheDocument();
  });

  it("lists MCP connections with an inert (pending) add affordance", async () => {
    withQuery(<SettingsView />);
    expect(await screen.findByText("Filesystem")).toBeInTheDocument();
    expect(screen.getByText("Connected")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Add connection/i })).toBeDisabled();
  });
});
