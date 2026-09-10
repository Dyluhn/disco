/**
 * WO-C3 §7.8 — `needs_review` renders EVERY blocker, NO run command, NO bound
 * self-host action; the PLAIN "Download source" remains present and wired.
 *
 * FROZEN acceptance path (plan §1.1: `frontend/src/test/export-track1-closeout/**`).
 * Locked semantics §2.3/§2.6 + plan §7 (WO-C3) acceptance 8: for a `needs_review`
 * release every blocker is rendered, no run command appears, and a bound self-host
 * download is unavailable — but the plain source download stays available (the
 * source is always retrievable). No false affordance.
 *
 * Boundary (plan §1.2 / §3.3): driven through the REAL data hook `useProjectRelease`
 * (real `getProjectRelease` → the offline `needs_review` fixture `conv_demo_landing`)
 * rendering the REAL `SelfHostPanel`. Nothing is mocked.
 *
 * RED/GREEN on baseline `2ec1ceba`: this is a GREEN-PRESERVATION guard — the
 * baseline `needs_review` branch of `SelfHostPanel` already renders each blocker,
 * suppresses the run command, and keeps only the plain download. It pins that honest
 * behavior so a C3/C6 change that adds a bound self-host action cannot let it leak
 * into the review state.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { SelfHostPanel } from "@/components/build/SelfHostPanel";
import { useProjectRelease } from "@/hooks/useProjects";

/** Real component + real hook: resolve the release verdict for `cid` through
 * `useProjectRelease`, then render the real `SelfHostPanel` with it. */
function ReleasePanelThroughHook({
  cid,
  onDownload,
}: {
  cid: string;
  onDownload: () => void;
}): ReactElement {
  const { data } = useProjectRelease(cid);
  if (!data) return <div data-testid="loading">loading</div>;
  return <SelfHostPanel release={data} onDownload={onDownload} />;
}

function renderThroughHook(cid: string, onDownload: () => void) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ReleasePanelThroughHook cid={cid} onDownload={onDownload} />
    </QueryClientProvider>,
  );
}

describe("WO-C3 §7.8 — needs_review shows blockers, no run command, plain download only", () => {
  it("renders each blocker (code + message), no run command, and keeps the plain wired download", async () => {
    const onDownload = vi.fn();
    // `conv_demo_landing` is the offline `needs_review` fixture (self_host:false,
    // one repairable blocker `release_field_unresolved`).
    const { container } = renderThroughHook("conv_demo_landing", onDownload);

    // The panel mounts once the verdict resolves and reports the review state.
    await waitFor(() =>
      expect(container.querySelector('[data-disco-control="build.self-host"]')).not.toBeNull(),
    );
    expect(container.querySelector('[data-self-host-status="needs_review"]')).not.toBeNull();

    // EVERY blocker in the verdict is rendered — asserted by code AND message, not a
    // count. The fixture carries `release_field_unresolved`.
    expect(container.querySelector('[data-blocker-code="release_field_unresolved"]')).not.toBeNull();
    expect(
      screen.getByText(/no single web entrypoint could be resolved/i),
    ).toBeInTheDocument();

    // NO run command in the review state (a false affordance — it isn't runnable yet).
    expect(container.querySelector('[data-disco-control="build.self-host-command"]')).toBeNull();
    // No self-host-ready wording either.
    expect(screen.queryByText(/ready to self-host/i)).not.toBeInTheDocument();

    // The PLAIN "Download source" action remains present and actually wired.
    const download = screen.getByRole("button", { name: "Download source" });
    expect(container.querySelector('[data-disco-control="build.download-source"]')).not.toBeNull();
    expect(download).toBeEnabled();
    await userEvent.click(download);
    expect(onDownload).toHaveBeenCalledOnce();
  });
});
