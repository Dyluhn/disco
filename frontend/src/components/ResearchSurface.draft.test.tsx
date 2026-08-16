/**
 * W-06 — the typed draft survives the standard ↔ Deep Research toggle.
 *
 * ROOT: QueryInput owned the draft in LOCAL state; toggling scope swaps the
 * standard search input for DeepResearchSurface (a DIFFERENT QueryInput),
 * unmounting the old input and losing whatever was typed. FIX: the draft is
 * lifted to the shared parent (ResearchSurface) and passed (controlled) to BOTH
 * inputs, so the same string survives the mount swap.
 *
 * We render the REAL ResearchSurface (real hooks) in OFFLINE mode (agentLive
 * false → no pre-create network noise; the scope toggle + draft is pure UI).
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ModeProvider } from "@/shell/ModeProvider";
import { ResearchSurface } from "@/components/ResearchSurface";

// Epic 12-C / Amendment A3: components/ and views/ may not import `@/api/client`.
// `vi.mock` intercepts by specifier and needs no static import, so this controls
// exactly the same `agentLive` the previous `vi.spyOn(clientModule, …)` did.
const { agentLiveMock } = vi.hoisted(() => ({ agentLiveMock: vi.fn(() => false) }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  agentLive: () => agentLiveMock(),
}));

// ScopeControl's Radix menu is covered independently in controls.test.tsx. This
// parent-state test needs only its value/callback contract.
vi.mock("@/components/ScopeControl", () => ({
  ScopeControl: ({
    value,
    onChange,
  }: {
    value: "standard" | "deep_research";
    onChange: (scope: "standard" | "deep_research") => void;
  }) => (
    <button
      type="button"
      aria-label={`Scope: ${value === "standard" ? "Standard" : "Deep Research"}`}
      onClick={() => onChange(value === "standard" ? "deep_research" : "standard")}
    >
      Change scope
    </button>
  ),
}));

function renderSurface() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/"]}>
        <ModeProvider>
          <Routes>
            <Route path="/" element={<ResearchSurface />} />
          </Routes>
        </ModeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("W-06 — draft persists across Search ↔ Deep Research toggle", () => {
  beforeEach(() => {
    agentLiveMock.mockReturnValue(false);
  });
  afterEach(() => vi.restoreAllMocks());

  it("keeps the typed query when toggling standard → deep research → back", async () => {
    await act(() => {
      renderSurface();
    });
    const user = userEvent.setup();

    const DRAFT = "quantum error correction survey";

    // Type into the standard search box.
    const stdInput = screen.getByPlaceholderText(/ask anything/i);
    await user.type(stdInput, DRAFT);
    expect(stdInput).toHaveValue(DRAFT);

    // Toggle scope → Deep Research. The standard input unmounts; DR mounts its
    // OWN QueryInput. The draft must still be there (shared parent state).
    await user.click(screen.getByRole("button", { name: /Scope: Standard/i }));

    const drInput = await screen.findByPlaceholderText(/multi-page report/i);
    expect(drInput).toHaveValue(DRAFT);

    // Toggle back → standard; the draft still persists across the second swap.
    await user.click(screen.getByRole("button", { name: /Research options/i }));
    await user.click(screen.getByRole("button", { name: /Scope: Deep Research/i }));

    expect(await screen.findByPlaceholderText(/ask anything/i)).toHaveValue(DRAFT);
  });
});
