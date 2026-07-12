/**
 * ProjectsView (WO-9) — the per-row workspace-zip action is relabelled from
 * "Download a zip" to "Download source", and each row grows a capability-driven
 * self-host affordance sourced from `useProjectRelease`.
 *
 * Runs against the offline fixtures (no live agent server): three rows, one of
 * which (`conv_demo_snake`) has a `candidate`/self-hostable release verdict.
 */

import { render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";
import { ProjectsView } from "@/views/ProjectsView";

function renderView(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ProjectsView — WO-9 relabel + self-host affordance", () => {
  it("labels the per-row download action 'Download source' (not 'Download a zip')", async () => {
    renderView(<ProjectsView />);
    // Rows load from the offline fixtures.
    const buttons = await screen.findAllByRole("button", { name: /download source/i });
    expect(buttons.length).toBeGreaterThan(0);
    // The old title string is gone from every row.
    expect(document.querySelector('[title="Download a zip"]')).toBeNull();
    // The workspace-zip action carries the new title.
    expect(document.querySelector('[title="Download source"]')).not.toBeNull();
  });

  it("shows a self-host badge on a self-hostable project row", async () => {
    renderView(<ProjectsView />);
    // conv_demo_snake resolves to a `candidate` (self_host: true) verdict.
    const badge = await screen.findByText(/self-host/i);
    expect(badge).toBeInTheDocument();
    // The badge is a non-interactive status marker (a <span>), not its own
    // (dead) button — it carries the capability signal, no false affordance.
    expect(badge.tagName).toBe("SPAN");
    expect(badge.getAttribute("data-self-host")).toBe("ready");
  });

  it("does not badge the files-missing row as self-hostable", async () => {
    renderView(<ProjectsView />);
    // Wait for rows to render.
    await screen.findAllByRole("button", { name: /download source/i });
    // The orphan row (files missing) shows the files-missing marker but no
    // self-host badge — its query is skipped, so no false capability signal.
    const orphanRow = screen.getByText("Deleted-workspace demo").closest("li");
    expect(orphanRow).not.toBeNull();
    expect(within(orphanRow as HTMLElement).queryByText(/self-host/i)).toBeNull();
  });
});
