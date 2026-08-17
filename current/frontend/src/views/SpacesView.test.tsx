import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { SpacesView } from "./SpacesView";

function withProviders(ui: ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  qc.setQueryData(["spaces"], {
    status: "ok",
    spaces: [{
      space_id: "space_client",
      name: "Client Work",
      description: "Launch conversations",
      created_at: "2026-06-01T00:00:00Z",
      member_count: 1,
    }],
  });
  qc.setQueryData(["space", "space_client"], {
    space_id: "space_client",
    name: "Client Work",
    description: "Launch conversations",
    created_at: "2026-06-01T00:00:00Z",
    member_count: 1,
    members: [{
      id: "conv_member",
      owner_id: "local",
      space_id: "space_client",
      title: "Launch plan",
      created_at: "2026-06-02T12:00:00Z",
      status: "FINISHED",
      surface: "build",
      origin: null,
    }],
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Spaces — organization folders", () => {
  it("renders folders and conversation members without upload UI", async () => {
    withProviders(<SpacesView />);

    await waitFor(() => expect(screen.getAllByText("Client Work").length).toBeGreaterThan(0));
    expect(screen.getAllByText(/1 item/i).length).toBeGreaterThan(0);
    expect(screen.getByText("Launch plan")).toBeInTheDocument();
    expect(screen.getByText("build")).toBeInTheDocument();
    expect(screen.queryByText(/drop documents here/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /choose files/i })).not.toBeInTheDocument();
  });
});
