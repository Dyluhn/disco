/**
 * WO-C3 red test — the ProjectsView per-row self-host badge is honest about an
 * UNVERIFIED candidate (§7 acceptance 7, applied to the SECOND UI mounting point).
 *
 * FROZEN acceptance path (plan §1.1: `frontend/src/test/export-track1-closeout/**`).
 * Locked semantic §2.1 / plan §7 (WO-C3) acceptance 7 requires that at EVERY UI
 * mounting point a `candidate` release is presented as explicitly unverified — the
 * row says the bundle has NOT been run and the surface never claims the project is
 * "ready" or "verified". `SelfHostPanel` is one mounting point (covered by
 * `c3-candidate-copy.test.tsx`); the per-row badge in `ProjectsView` is a SECOND,
 * and it currently lies.
 *
 * UI-30 (2026-09-09) reworded the badge from the internal "NOT RUNTIME-VERIFIED"
 * to "Bundle not run yet" plus a tooltip saying what it means and what would change
 * it. The GUARANTEE is unchanged and wider: the row must still carry the
 * machine-checkable `data-self-host="candidate"` marker, must still say in words
 * that the bundle has not been run, must NOT stamp `data-self-host="ready"`, and its
 * text must now contain neither "ready" NOR "verified". The `it(...)` title is left
 * byte-identical on purpose — it is a sealed identifier in the closeout acceptance
 * manifest and in development/architecture/test-inventory.json.
 *
 * Boundary (plan §1.2 / §3.3): the real `ProjectsView` is rendered through its real
 * data hooks (`useProjects` + `useProjectRelease`) against the offline fixtures, so
 * the `conv_demo_snake` row resolves a genuine `candidate` (`self_host: true`)
 * verdict. Nothing is mocked; the badge is proven at the component-through-hook
 * boundary.
 *
 * WHY IT IS RED ON BASELINE `2ec1ceba`: `ProjectRow` renders
 * `<span data-self-host="ready">self-host</span>` whenever `release.self_host` is
 * true — which a `candidate` is — with no verification qualifier. So the readiness
 * claim `data-self-host="ready"` is present and "Not runtime-verified" is absent;
 * both assertions fail. WO-C3 must re-tie the readiness affordance to verification
 * and qualify the candidate as unverified at this mounting point too.
 *
 * NOTE for the WO-C3 implementer: the existing WO-9 test
 * `src/views/ProjectsView.test.tsx` ("shows a self-host badge …") asserts the
 * pre-fix lie (`data-self-host` == "ready" for the candidate row). That test is a
 * regular (non-frozen) WO-9 test and must be updated to the honest contract when
 * this badge is fixed.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
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

describe("WO-C3 — ProjectsView candidate badge is honestly unverified, never 'ready'", () => {
  it("does not stamp data-self-host='ready' and shows 'Not runtime-verified' for a candidate row", async () => {
    renderView(<ProjectsView />);

    // `conv_demo_snake` ("Snake game prototype") is the offline `candidate`
    // (`self_host: true`) project; wait for its row to render.
    const title = await screen.findByText("Snake game prototype");
    const row = title.closest("li");
    expect(row).not.toBeNull();
    const rowEl = row as HTMLElement;

    // Evaluate BOTH honesty conditions once the release verdict has RESOLVED — not in
    // the pre-resolve window where the badge simply has not rendered yet. On baseline
    // the resolved candidate stamps `data-self-host="ready"` and never shows the
    // qualifier, so this waitFor times out (RED for the right reason); after WO-C3 the
    // resolved candidate is honest and both conditions hold together (GREEN).
    await waitFor(() => {
      // A `candidate` is statically plausible and UNVERIFIED (§2.1) — the row must not
      // assert readiness the project has not earned.
      expect(rowEl.querySelector('[data-self-host="ready"]')).toBeNull();
      // §7 acceptance 7: this mounting point must carry the unverified qualifier — as
      // a machine-checkable marker AND as words the user can read.
      const badge = rowEl.querySelector('[data-self-host="candidate"]');
      expect(badge).not.toBeNull();
      expect((badge?.textContent ?? "").toLowerCase()).toContain("not run yet");
      // …and it must explain itself rather than leaving a bare stamp (UI-30).
      expect(badge?.getAttribute("title") ?? "").toMatch(/hasn't checked that it works/i);
      // No overclaim in the row's visible text: never "ready", never "verified".
      const rowText = (rowEl.textContent ?? "").toLowerCase();
      expect(rowText).not.toContain("ready");
      expect(rowText).not.toContain("verified");
    });
  });
});
