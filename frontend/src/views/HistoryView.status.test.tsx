
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import type { ConversationSummary } from "@/types/conversation";
import { HistoryView } from "./HistoryView";

function withConversations(conversations: ConversationSummary[]) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  qc.setQueryData(["conversations"], conversations);
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <HistoryView />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("History — status chips", () => {
  it("renders a chip for each status category", () => {
    const data = [
      { id: "c1", owner_id: "me", title: "Conversation 1", created_at: "2026-06-10T10:00:00Z", status: "RUNNING" },
      { id: "c2", owner_id: "me", title: "Conversation 2", created_at: "2026-06-10T11:00:00Z", status: "PAUSED" },
      { id: "c3", owner_id: "me", title: "Conversation 3", created_at: "2026-06-10T12:00:00Z", status: "FINISHED" },
      { id: "c4", owner_id: "me", title: "Conversation 4", created_at: "2026-06-10T13:00:00Z", status: "STUCK" },
      { id: "c5", owner_id: "me", title: "Conversation 5", created_at: "2026-06-10T14:00:00Z", status: "ERROR" },
    ];
    withConversations(data);
    
    expect(screen.getByText(/Running/i)).toBeInTheDocument();
    expect(screen.getByText(/Paused/i)).toBeInTheDocument();
    expect(screen.getByText(/Finished/i)).toBeInTheDocument();
    expect(screen.getByText(/Stuck/i)).toBeInTheDocument();
    expect(screen.getByText(/Error/i)).toBeInTheDocument();
  });

  it("renders no chip when status is missing, but renders neutral chip for unknown statuses", () => {
    const data = [
      { id: "c1", owner_id: "me", title: "No Status", created_at: "2026-06-10T10:00:00Z" },
      { id: "c2", owner_id: "me", title: "Unknown Status", created_at: "2026-06-10T11:00:00Z", status: "IDLE" },
    ];
    withConversations(data);
    
    expect(screen.queryByText(/Running/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Paused/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Finished/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Stuck/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Error/i)).not.toBeInTheDocument();
    
    // Unknown status should now be visible as a neutral chip
    expect(screen.getByText(/Idle/i)).toBeInTheDocument();
  });
});
