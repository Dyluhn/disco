import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as spacesApi from "@/api/spaces";
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
  afterEach(() => vi.restoreAllMocks());

  it("returns confirmation cancellation to its trigger without deleting", async () => {
    const remove = vi.spyOn(spacesApi, "deleteSpace");
    const user = userEvent.setup();
    withProviders(<SpacesView />);
    const trigger = await screen.findByRole("button", { name: "Delete Space: Client Work" });
    await user.click(trigger);
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(remove).not.toHaveBeenCalled();
  });

  it("keeps keyboard focus on a stable control after the deleted Space disappears", async () => {
    vi.spyOn(spacesApi, "deleteSpace").mockResolvedValue({ space_id: "space_client" });
    vi.spyOn(spacesApi, "listSpaces").mockResolvedValue({ status: "ok", spaces: [] });
    const user = userEvent.setup();
    withProviders(<SpacesView />);
    await user.click(await screen.findByRole("button", { name: "Delete Space: Client Work" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(screen.queryByRole("button", {
      name: "Delete Space: Client Work",
    })).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole("searchbox", { name: "Search Spaces" })).toHaveFocus());
  });

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
