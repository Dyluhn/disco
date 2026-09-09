/**
 * The finished-handoff region: the deliverable download/open panel and the
 * capability-driven Self-host panel. Extracted verbatim from BuildSurface.tsx
 * (PKG-12-FE-BUILD). Amendment A3: `canonicalPreviewBootstrapUrl` comes from
 * `@/api/agent` (re-exporting `@/api/client`'s builder), never `@/api/client`
 * directly — this file is under `src/components/`, so the eslint boundary rule
 * applies to it exactly as it did to the original BuildSurface.tsx.
 */

import { DeliverablePanel } from "@/components/build/DeliverablePanel";
import { SelfHostPanel } from "@/components/build/SelfHostPanel";
import { canonicalPreviewBootstrapUrl } from "@/api/agent";
import { openFreshPreview } from "@/lib/previewLaunch";
import { useToast } from "@/components/toastApi";
import type { useDownloadProject, useExportManifest, useProjectRelease } from "@/hooks/useProjects";
import type { DeliverableView } from "@/lib/buildTrace";
import type { BuildController } from "./types";

export function BuildDeliverableSection({
  b,
  deliverable,
  download,
  exportManifest,
  release,
  downloadTitle = null,
}: {
  b: BuildController;
  deliverable: DeliverableView | null;
  download: ReturnType<typeof useDownloadProject>;
  exportManifest: ReturnType<typeof useExportManifest>;
  release: ReturnType<typeof useProjectRelease>;
  /** The run's own title — names the SAVED ZIP (UI-40), nothing else. */
  downloadTitle?: string | null;
}) {
  const toast = useToast();
  return (
    <>
      {/* finished-artifact handoff: open the live app / download the files.
          WALK-09: gate on FINISHED — `serve` emits a DeliverableEvent mid-run
          (before the plan/build ends) so `deriveDeliverable` returns non-null
          while the manifest record doesn't exist yet → Manifest button 404s and
          Open 503s. Only show the panel once the run is truly done. */}
      <DeliverablePanel
        deliverable={b.status === "FINISHED" ? deliverable : null}
        cid={b.cid}
        onOpen={
          b.status === "FINISHED" && b.cid && deliverable?.kind === "app"
            ? () =>
                openFreshPreview(
                  () => canonicalPreviewBootstrapUrl(b.cid!, "/"),
                  (reason) =>
                    toast.show({
                      title:
                        reason === "popup_blocked"
                          ? "Preview popup blocked"
                          : "Couldn’t open preview",
                      body:
                        reason === "popup_blocked"
                          ? "Allow popups for this site, then try again."
                          : "Isolated preview access could not be established. Try again.",
                    }),
                )
            : undefined
        }
        onDownload={() =>
          b.cid && download.mutate({ id: b.cid, binding: null, title: downloadTitle })
        }
        onExportManifest={() => b.cid && exportManifest.mutate(b.cid)}
      />
      {/* WO-9: capability-driven Self-host handoff — renders from the release
          verdict ALONE (no mode/framing knowledge), so Build and Agent surfaces
          inherit an identical panel. Shown alongside the DeliverablePanel once
          the run is finished and the verdict has resolved. */}
      {b.status === "FINISHED" && release.data && (
        <SelfHostPanel
          release={release.data}
          onDownload={() => {
            const r = release.data;
            if (!b.cid || !r) return;
            // Bind the download to the release ONLY when it is a genuine
            // self-host candidate whose source is fully named — both
            // version_seq AND spec_digest concrete. This narrowing is the guard:
            // because the release type makes those fields nullable, passing them
            // without the `!== null` narrowing is a compile error (a null binding
            // can never ride a bound URL). A non-candidate (needs_review / not_web)
            // downloads the plain, unbound zip.
            const binding =
              r.self_host && r.version_seq !== null && r.spec_digest !== null
                ? { version_seq: r.version_seq, spec_digest: r.spec_digest }
                : null;
            download.mutate({ id: b.cid, binding, title: downloadTitle });
          }}
        />
      )}
    </>
  );
}
