import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as clientModule from "@/api/client";
import type { ConversationSummary } from "@/types/conversation";
import type { SpacesList } from "@/types/spaces";
import { HistoryView } from "./HistoryView";

function withProviders(
  ui: ReactElement,
  seedEmpty = false,
  seed?: {
    spaces?: SpacesList;
    conversations?: ConversationSummary[];
    filtered?: Record<string, ConversationSummary[]>;
  },
) {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: seedEmpty || seed ? Infinity : 0 },
    },
  });
  if (seedEmpty) qc.setQueryData(["conversations"], []);
  if (seed?.spaces) qc.setQueryData(["spaces"], seed.spaces);
  if (seed?.conversations) qc.setQueryData(["conversations"], seed.conversations);
  if (seed?.filtered) {
    for (const [spaceId, rows] of Object.entries(seed.filtered)) {
      qc.setQueryData(["conversations", spaceId], rows);
    }
  }
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("History — conversation library", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

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

  it("filters conversations by Space", async () => {
    const user = userEvent.setup();
    const clientWork: ConversationSummary = {
      id: "c-client",
      owner_id: "owner-me",
      space_id: "space_client",
      title: "Client research plan",
      created_at: "2026-06-01T10:00:00Z",
      surface: "deep_research",
    };
    const unfiled: ConversationSummary = {
      id: "c-unfiled",
      owner_id: "owner-me",
      space_id: null,
      title: "Loose note",
      created_at: "2026-06-02T10:00:00Z",
      surface: "research",
    };
    withProviders(<HistoryView />, false, {
      spaces: {
        status: "ok",
        spaces: [{
          space_id: "space_client",
          name: "Client Work",
          description: "",
          created_at: "2026-06-01T00:00:00Z",
          member_count: 1,
        }],
      },
      conversations: [clientWork, unfiled],
      filtered: { space_client: [clientWork] },
    });

    await waitFor(() => expect(screen.getByText(/Loose note/i)).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText(/space filter/i), "space_client");

    await waitFor(() => {
      expect(screen.getByText(/Client research plan/i)).toBeInTheDocument();
      expect(screen.queryByText(/Loose note/i)).not.toBeInTheDocument();
    });
  });

  it("moves a conversation to a Space from the row menu", async () => {
    const user = userEvent.setup();
    vi.spyOn(clientModule, "agentLive").mockReturnValue(true);
    const send = vi
      .spyOn(clientModule, "agentSend")
      .mockResolvedValue({ ok: true } as never);
    const row: ConversationSummary = {
      id: "c-loose",
      owner_id: "owner-me",
      space_id: null,
      title: "Loose note",
      created_at: "2026-06-02T10:00:00Z",
      surface: "research",
    };
    withProviders(<HistoryView />, false, {
      spaces: {
        status: "ok",
        spaces: [{
          space_id: "space_client",
          name: "Client Work",
          description: "",
          created_at: "2026-06-01T00:00:00Z",
          member_count: 0,
        }],
      },
      conversations: [row],
    });

    await waitFor(() => expect(screen.getByText(/Loose note/i)).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: /move conversation to space: loose note/i }));
    const menu = await screen.findByRole("menu");
    await user.click(within(menu).getByRole("menuitem", { name: /Client Work/i }));

    await waitFor(() =>
      expect(send).toHaveBeenCalledWith(
        "POST",
        "/api/conversations/c-loose/space",
        { space_id: "space_client" },
      ),
    );
  });
});
