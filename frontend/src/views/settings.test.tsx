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

  it("changes a role assignment absolutely (the row reflects exactly what was set)", async () => {
    const user = userEvent.setup();
    withQuery(<SettingsView />);
    const ragTrigger = await screen.findByRole("button", {
      name: /Choose model for RAG answerer/i,
    });
    expect(ragTrigger).toHaveTextContent(/Local RAG/i);

    await user.click(ragTrigger);
    const dialog = screen.getByRole("dialog");
    // Picker is cost-legible and exposes the paid overflow option too.
    expect(within(dialog).getByText("$3 / $15 / Mtok")).toBeInTheDocument();
    await user.click(within(dialog).getByText(/Local Summarizer/i));

    // Absolute: the RAG row now shows exactly the chosen model.
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /Choose model for RAG answerer/i }),
      ).toHaveTextContent(/Local Summarizer/i),
    );
  });
});

describe("Settings — skills + MCP scaffolds", () => {
  it("lists skills with working toggles, honestly marked wiring-pending", async () => {
    const user = userEvent.setup();
    withQuery(<SettingsView />);
    const sw = await screen.findByRole("switch", { name: /Enable Web research/i });
    expect(sw).toHaveAttribute("aria-checked", "true");
    // Honest scaffolding marker present.
    expect(screen.getAllByText(/wiring pending/i).length).toBeGreaterThanOrEqual(1);
    await user.click(sw);
    await waitFor(() => expect(sw).toHaveAttribute("aria-checked", "false"));
  });

  it("lists MCP connections with an inert (pending) add affordance", async () => {
    withQuery(<SettingsView />);
    expect(await screen.findByText("Filesystem")).toBeInTheDocument();
    expect(screen.getByText("Connected")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Add connection/i })).toBeDisabled();
  });
});
