/**
 * WO-C3 red test — honest `candidate` copy, rendered through the REAL hook.
 *
 * FROZEN acceptance path (plan §1.1: `frontend/src/test/export-track1-closeout/**`).
 * Locked semantic §2.1 / plan §7 (WO-C3) acceptance 7: for a `candidate` release
 * every UI mounting point must render the "Bundle available" status AND an explicit
 * statement that the bundle has NOT been run, and case-insensitive DOM text must
 * contain no "ready" and no "verified". `candidate` means statically plausible and
 * UNVERIFIED — it may never say "Ready", "verified", or an equivalent.
 *
 * UI-16 (2026-09-09) reworded the unverified qualifier from the internal phrase
 * "Not runtime-verified — the self-host bundle is available but has not been run"
 * into user language ("Disco hasn't started it yet, so it hasn't checked that it
 * works"). The GUARANTEE this node enforces is unchanged and, if anything, wider:
 * the panel must still carry the machine-checkable unverified marker
 * (`[data-self-host-note="unverified"]`), must still say IN WORDS that Disco has
 * not started/checked it, and the whole rendered DOM must now contain neither
 * "ready" NOR "verified" (the old node only banned "ready"). The `it(...)` title is
 * left byte-identical on purpose: it is a sealed identifier in three separate
 * authority files (the closeout acceptance manifest's frontend_closeout_inventory
 * and red_tests node_id, and development/architecture/test-inventory.json under the
 * attestation seal), so renaming it is a governance action, not a copy fix.
 *
 * Boundary (plan §1.2 / §3.3): the panel is driven through its real data hook
 * `useProjectRelease` (real `getProjectRelease` → the offline `candidate` fixture
 * `conv_demo_snake`, which has `self_host: true`) rather than a hand-built prop, so
 * the copy is proven at the component-through-hook boundary. Nothing is mocked.
 *
 * WHY IT IS RED ON BASELINE `2ec1ceba`: `SelfHostPanel`'s status pill renders
 * "Ready to self-host" for a self-hostable candidate, so "Bundle available" is
 * absent and the DOM contains "ready" — both assertions fail. WO-C3 fixes the copy.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";
import { SelfHostPanel } from "@/components/build/SelfHostPanel";
import { useProjectRelease } from "@/hooks/useProjects";

/** Real component + real hook: resolve the release verdict for `cid` through
 * `useProjectRelease`, then render the real `SelfHostPanel` with it. */
function ReleasePanelThroughHook({ cid }: { cid: string }): ReactElement {
  const { data } = useProjectRelease(cid);
  if (!data) return <div data-testid="loading">loading</div>;
  return <SelfHostPanel release={data} onDownload={() => {}} />;
}

function renderThroughHook(cid: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ReleasePanelThroughHook cid={cid} />
    </QueryClientProvider>,
  );
}

describe("WO-C3 — candidate UI is explicitly unverified, never 'Ready'", () => {
  it("renders 'Bundle available' + 'Not runtime-verified' and no case-insensitive 'ready'", async () => {
    // `conv_demo_snake` is the offline `candidate` fixture with `self_host: true`.
    const { container } = renderThroughHook("conv_demo_snake");

    // The self-host panel always mounts once the verdict resolves.
    await waitFor(() =>
      expect(container.querySelector('[data-disco-control="build.self-host"]')).not.toBeNull(),
    );

    // Honest status for a candidate (locked §2.1): a bundle exists, nothing more.
    expect(screen.getByText("Bundle available")).toBeInTheDocument();

    // The unverified state is marked for machines AND stated in words for the
    // user: Disco has not started the bundle, so it has not checked that it works.
    const note = container.querySelector('[data-self-host-note="unverified"]');
    expect(note).not.toBeNull();
    const noteText = (note?.textContent ?? "").toLowerCase();
    expect(noteText).toContain("hasn't started it");
    expect(noteText).toContain("hasn't checked that it works");
    // The VERIFIED note is the mutually exclusive branch — it must not be mounted.
    expect(container.querySelector('[data-self-host-note="verified"]')).toBeNull();

    // No overclaim anywhere in the rendered DOM: never "Ready", and (stricter than
    // the pre-UI-16 node) never "verified" either.
    const domText = (container.textContent ?? "").toLowerCase();
    expect(domText).not.toContain("ready");
    expect(domText).not.toContain("verified");
  });
});
