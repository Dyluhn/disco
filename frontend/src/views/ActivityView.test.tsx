import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { ActivityView } from "./ActivityView";

/** Offline (no VITE_AGENT_BASE) → getActivity replays the in-repo fixture: one
 * running task + two scheduled runs (one coalesced). Asserts the dashboard renders
 * both sections honestly. */
function renderActivity() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ActivityView />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Activity view", () => {
  it("shows what's running now and the recent scheduled runs", async () => {
    renderActivity();
    // running task (fixture)
    expect(await screen.findByText("Draft the Q3 competitive brief")).toBeInTheDocument();
    expect(screen.getByText("Running now")).toBeInTheDocument();
    // recent scheduled runs (fixture: two "Morning market digest", one coalesced)
    expect(screen.getByText("Recent scheduled runs")).toBeInTheDocument();
    expect(screen.getAllByText("Morning market digest").length).toBe(2);
    expect(screen.getByText(/coalesced/i)).toBeInTheDocument();
  });
});
