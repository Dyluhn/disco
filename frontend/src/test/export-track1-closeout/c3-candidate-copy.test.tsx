/**
 * WO-C3 red test — honest `candidate` copy, rendered through the REAL hook.
 *
 * FROZEN acceptance path (plan §1.1: `frontend/src/test/export-track1-closeout/**`).
 * Locked semantic §2.1 / plan §7 (WO-C3) acceptance 7: for a `candidate` release
 * every UI mounting point must render the EXACT visible strings "Bundle available"
 * and "Not runtime-verified", and case-insensitive DOM text must contain no
 * "ready". `candidate` means statically plausible and UNVERIFIED — it may never say
 * "Ready", "verified", or an equivalent.
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

    // Exact honest copy for a candidate (locked §2.1).
    expect(screen.getByText("Bundle available")).toBeInTheDocument();
    expect(screen.getByText(/not runtime-verified/i)).toBeInTheDocument();

    // Never "Ready"/"verified"-as-ready anywhere in the rendered DOM.
    const domText = (container.textContent ?? "").toLowerCase();
    expect(domText).not.toContain("ready");
  });
});
