import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
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

  it("disables the assignment pickers and flags in red that they are not wired", async () => {
    // Honesty over polish: the runtime doesn't consume saved assignments yet, so
    // the pickers must NOT look operable. They are disabled and the gap is flagged.
    withQuery(<SettingsView />);
    const ragTrigger = await screen.findByRole("button", {
      name: /Choose model for RAG answerer/i,
    });
    expect(ragTrigger).toBeDisabled();
    // The red NotWired banner states the specific gap (no silent dead control).
    expect(screen.getAllByText(/not functional yet/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/does not read these assignments/i)).toBeInTheDocument();
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
