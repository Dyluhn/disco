import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { HistoryView } from "./HistoryView";

function withProviders(ui: ReactElement, seedEmpty = false) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: seedEmpty ? Infinity : 0 } },
  });
  if (seedEmpty) qc.setQueryData(["conversations"], []);
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("History — conversation library", () => {
  it("lists ONLY the current owner's conversations (owner-scoped)", async () => {
    withProviders(<HistoryView />);
    await waitFor(() =>
      expect(screen.getByText(/reciprocal rank fusion/i)).toBeInTheDocument(),
    );
    expect(screen.getByText(/MIT and Apache 2.0/i)).toBeInTheDocument();
    // The other owner's conversation must never surface.
    expect(screen.queryByText(/owned by a different user/i)).not.toBeInTheDocument();
  });

  it("filters the list as you search", async () => {
    const user = userEvent.setup();
    withProviders(<HistoryView />);
    await waitFor(() => expect(screen.getByText(/reciprocal rank fusion/i)).toBeInTheDocument());
    await user.type(screen.getByRole("searchbox", { name: /search conversations/i }), "license");
    
    await waitFor(() => {
      expect(screen.getByText(/MIT and Apache 2.0/i)).toBeInTheDocument();
      expect(screen.queryByText(/reciprocal rank fusion/i)).not.toBeInTheDocument();
    });
  });

  it("shows a calm empty state when there are no conversations", () => {
    withProviders(<HistoryView />, true);
    expect(screen.getByText(/no conversations yet/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /start a query/i })).toBeInTheDocument();
  });

  it("deletes a conversation only after a confirm", async () => {
    const user = userEvent.setup();
    withProviders(<HistoryView />);
    await waitFor(() => expect(screen.getByText(/intermittent fasting/i)).toBeInTheDocument());

    await user.click(
      screen.getByRole("button", { name: /delete conversation: current evidence on intermittent fasting/i }),
    );
    // Confirm gate appears (destructive — never one-click).
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent(/can't be undone/i);
    await user.click(screen.getByRole("button", { name: /^Delete$/ }));

    await waitFor(() =>
      expect(screen.queryByText(/intermittent fasting/i)).not.toBeInTheDocument(),
    );
  });
});
