/**
 * The finished-handoff region: the deliverable download/open panel and the
 * capability-driven Self-host panel. Extracted verbatim from BuildSurface.tsx
 * (PKG-12-FE-BUILD). Amendment A3: `canonicalPreviewBootstrapUrl` comes from
 * `@/api/agent` (re-exporting `@/api/client`'s builder), never `@/api/client`
 * directly — this file is under `src/components/`, so the eslint boundary rule
 * applies to it exactly as it did to the original BuildSurface.tsx.
 */

import { LoaderCircle, RefreshCw } from "lucide-react";
import { DeliverablePanel } from "@/components/build/DeliverablePanel";
import { SelfHostPanel } from "@/components/build/SelfHostPanel";
import { canonicalPreviewBootstrapUrl } from "@/api/agent";
import { openFreshPreview } from "@/lib/previewLaunch";
import { useToast } from "@/components/toastApi";
import type { useDownloadProject, useExportManifest, useProjectRelease } from "@/hooks/useProjects";
import type { DeliverableView } from "@/lib/buildTrace";
import type { CommittedFinish } from "@/lib/committedFinish";
import type { BuildController } from "./types";

export function BuildDeliverableSection({
  b,
  deliverable,
  download,
  exportManifest,
  release,
  committedFinish,
}: {
  b: BuildController;
  deliverable: DeliverableView | null;
  download: ReturnType<typeof useDownloadProject>;
  exportManifest: ReturnType<typeof useExportManifest>;
  release: ReturnType<typeof useProjectRelease>;
  committedFinish: CommittedFinish | null;
}) {
  const toast = useToast();
  const handoffReady = b.status === "FINISHED" && committedFinish !== null;
  const releaseLoading = handoffReady && (release.isLoading || release.isFetching);
  const releaseError = handoffReady ? release.error : null;
  const releaseErrorMessage =
    releaseError?.name === "ReleaseSealMismatchError"
      ? "Self-host details no longer match the finished workspace."
      : "Self-host details could not be loaded.";
  return (
    <>
      {/* finished-artifact handoff: open the live app / download the files.
          WALK-09: gate on FINISHED — `serve` emits a DeliverableEvent mid-run
          (before the plan/build ends) so `deriveDeliverable` returns non-null
          while the manifest record doesn't exist yet → Manifest button 404s and
          Open 503s. Only show the panel once the run is truly done. */}
      <DeliverablePanel
        deliverable={handoffReady ? deliverable : null}
        cid={b.cid}
        onOpen={
          handoffReady && b.cid && deliverable?.kind === "app"
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
        onDownload={handoffReady ? () => b.cid && download.mutate({ id: b.cid, binding: null }) : undefined}
        onExportManifest={handoffReady ? () => b.cid && exportManifest.mutate(b.cid) : undefined}
      />
      {/* WO-9: capability-driven Self-host handoff — renders from the release
          verdict ALONE (no mode/framing knowledge), so Build and Agent surfaces
          inherit an identical panel. Shown alongside the DeliverablePanel once
          the run is finished and the verdict has resolved. */}
      {releaseLoading && (
        <div
          role="status"
          aria-busy="true"
          data-disco-control="build.self-host-loading"
          className="flex items-center gap-hair rounded-control border border-hairline px-body py-inline font-ui text-[0.78rem] text-text-muted"
        >
          <LoaderCircle className="size-3.5 animate-spin" aria-hidden />
          Preparing self-host details…
        </div>
      )}
      {releaseError && (
        <div
          role="alert"
          data-disco-control="build.self-host-error"
          className="flex items-center gap-inline rounded-control border border-unsupported/40 bg-unsupported/5 px-body py-inline"
        >
          <p className="flex-1 font-ui text-[0.78rem] text-unsupported">
            {releaseErrorMessage} Try again to refresh it.
          </p>
          <button
            type="button"
            onClick={() => void release.refetch()}
            disabled={release.isFetching}
            className="inline-flex shrink-0 items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted hover:text-text disabled:opacity-50"
          >
            <RefreshCw className="size-3.5" aria-hidden />
            {release.isFetching ? "Retrying…" : "Retry"}
          </button>
        </div>
      )}
      {handoffReady && release.data && (
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
            download.mutate({ id: b.cid, binding });
          }}
        />
      )}
    </>
  );
}
