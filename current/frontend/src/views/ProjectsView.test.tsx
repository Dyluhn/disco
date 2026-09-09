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

  it("shows an honest, unverified self-host badge on a candidate project row", async () => {
    renderView(<ProjectsView />);
    // conv_demo_snake resolves to a `candidate` (self_host: true, UNVERIFIED) verdict.
    // WO-C3 (§7.7): an unverified candidate is NEVER stamped "ready" — it says plainly
    // that the bundle has not been run instead (UI-30 reworded, same guarantee).
    const badge = await screen.findByText(/bundle not run yet/i);
    expect(badge).toBeInTheDocument();
    // The badge is a non-interactive status marker (a <span>), not its own
    // (dead) button — it carries the capability signal, no false affordance.
    expect(badge.tagName).toBe("SPAN");
    // A candidate is unverified: the badge must NOT claim readiness.
    expect(badge.getAttribute("data-self-host")).toBe("candidate");
    const row = badge.closest("li") as HTMLElement;
    expect(row.querySelector('[data-self-host="ready"]')).toBeNull();
  });

  // UI-30 regression: the old badge read "NOT RUNTIME-VERIFIED" with nothing to
  // tell the user what it meant or what would change it. The label must stay plain
  // AND carry that explanation, so a bare internal stamp cannot come back.
  it("explains the candidate badge instead of stamping internal vocabulary", async () => {
    renderView(<ProjectsView />);
    const badge = await screen.findByText(/bundle not run yet/i);
    // No jargon in the visible label.
    expect(badge.textContent ?? "").not.toMatch(/runtime-verified/i);
    // The tooltip says what it means AND what would change it.
    const tip = badge.getAttribute("title") ?? "";
    expect(tip).toMatch(/hasn't started that bundle/i);
    expect(tip).toMatch(/hasn't checked that it works/i);
    expect(tip).toMatch(/run the bundle yourself/i);
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
