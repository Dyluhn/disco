/**
 * Composer redesign — the search-type slider, the depth dropdown, and the
 * driver-model notice all live INSIDE the composer card now.
 *
 *  - The slider (Search · Deep Research) replaces the standalone "Search type"
 *    row that sat above the box; switching it swaps the surface (same wire
 *    behavior as the old ScopeControl path).
 *  - The depth-tier dropdown renders only while Deep Research is selected.
 *  - The driver-model notice names the effective model and, on click, expands
 *    the options disclosure where the real model control lives.
 *
 * Rendered offline (agentLive false) like ResearchSurface.draft.test.tsx —
 * the controls under test are pure UI.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ModeProvider } from "@/shell/ModeProvider";
import { ResearchSurface } from "@/components/ResearchSurface";

const { agentLiveMock } = vi.hoisted(() => ({ agentLiveMock: vi.fn(() => false) }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  agentLive: () => agentLiveMock(),
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

describe("composer redesign — search-type slider + depth tier", () => {
  beforeEach(() => {
    window.localStorage.clear();
    agentLiveMock.mockReturnValue(false);
  });
  afterEach(() => vi.restoreAllMocks());

  it("the in-box slider switches between Search and Deep Research", async () => {
    await act(() => {
      renderSurface();
    });
    const user = userEvent.setup();

    // Splash: the slider sits in the card; no standalone "Search type" row.
    expect(screen.queryByText("Search type")).not.toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Search" })).toHaveAttribute(
      "aria-checked",
      "true",
    );

    await user.click(screen.getByRole("radio", { name: "Deep Research" }));
    expect(
      await screen.findByPlaceholderText(/multi-page report/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Deep Research" })).toHaveAttribute(
      "aria-checked",
      "true",
    );

    await user.click(screen.getByRole("radio", { name: "Search" }));
    expect(await screen.findByPlaceholderText(/ask anything/i)).toBeInTheDocument();
  });

  it("the depth control exists only under Deep Research, inside the options menu", async () => {
    await act(() => {
      renderSurface();
    });
    const user = userEvent.setup();

    // Standard search: no depth control anywhere — not even inside the menu.
    await user.click(screen.getByRole("button", { name: /^Options/ }));
    expect(screen.queryByRole("button", { name: /depth tier/i })).not.toBeInTheDocument();

    // Deep Research: the trigger summarizes the depth; the control itself is
    // in the menu, not the primary row.
    await user.click(screen.getByRole("radio", { name: "Deep Research" }));
    const options = await screen.findByRole("button", { name: /^Options/ });
    expect(options).toHaveTextContent(/· Standard/);
    expect(screen.queryByRole("button", { name: /depth tier/i })).not.toBeInTheDocument();
    await user.click(options);
    expect(
      await screen.findByRole("button", { name: /depth tier: Standard/i }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("radio", { name: "Search" }));
    await screen.findByPlaceholderText(/ask anything/i);
    expect(screen.queryByRole("button", { name: /depth tier/i })).not.toBeInTheDocument();
  });

  it("the driver-model notice names the model and expands the options area on click", async () => {
    await act(() => {
      renderSurface();
    });
    const user = userEvent.setup();

    // The notice resolves the same catalogue the pill reads (settings default
    // here — no explicit pick).
    const notice = await screen.findByRole("button", { name: /driver model:/i });
    expect(notice).toHaveAttribute("data-disco-control", "search.driver-model");
    expect(notice.textContent).not.toHaveLength(0);

    // Options closed → the model control is not mounted yet.
    expect(
      screen.queryByRole("button", { name: /choose the model that leads/i }),
    ).not.toBeInTheDocument();

    await user.click(notice);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /choose the model that leads/i }),
      ).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /^Options/ })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });
});
