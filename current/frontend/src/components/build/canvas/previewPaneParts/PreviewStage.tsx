import type { MutableRefObject } from "react";
import { Download, RotateCw } from "lucide-react";
import type { PreviewLaunch } from "@/api/preview";
import { PreviewLaunchFrame } from "@/components/PreviewLaunchFrame";
import { EditAffordance } from "@/components/build/canvas/EditAffordance";
import { SelectionOverlay } from "@/components/build/canvas/SelectionOverlay";
import type { ElementSelectState } from "@/hooks/useElementSelect";
import { formatSelectionContext } from "@/lib/resolvers/appResolver";
import {
  isPreviewReasonCode,
  previewStagePlaceholderMessage,
  shouldRenderPreviewFrame,
  shouldShowStatusOverlay,
  statusOverlayMessage,
  statusOverlayRole,
} from "./helpers";

export function PreviewStage({
  frameRef,
  launch,
  onFrameLoad,
  onBootstrapReady,
  visibleFailure,
  dataReason,
  minting,
  runtimeUnavailable,
  frameReady,
  editMode,
  untrusted,
  selection,
  onSteer,
  onDisarmEdit,
  onApplyEdit,
  onRefresh,
  onDownloadSource,
  downloadPending,
}: {
  frameRef: MutableRefObject<HTMLIFrameElement | null>;
  launch: PreviewLaunch | null;
  onFrameLoad: () => void;
  onBootstrapReady: () => void;
  visibleFailure: string | null;
  dataReason: string | undefined;
  minting: boolean;
  runtimeUnavailable: boolean;
  frameReady: boolean;
  editMode: boolean;
  untrusted: boolean;
  selection: ElementSelectState;
  onSteer?: (text: string) => void;
  onDisarmEdit: () => void;
  onApplyEdit: (instruction: string) => void;
  onRefresh: () => void;
  /** Null when there is no conversation to download (the static run view). */
  onDownloadSource: (() => void) | null;
  downloadPending: boolean;
}) {
  // UI-44: a frame that never loaded while a failure is visible is showing the
  // proxy's own error page. Replace it with the failure card rather than
  // overlaying a progress strip on top of a server error.
  const showFrame = shouldRenderPreviewFrame(launch, visibleFailure, frameReady);
  return (
    <div className="relative min-h-0 flex-1 bg-white">
      {showFrame && launch ? (
        <PreviewLaunchFrame
          ref={frameRef}
          title="Preview"
          launch={launch}
          onLoad={onFrameLoad}
          onBootstrapReady={onBootstrapReady}
          sandbox="allow-scripts allow-forms allow-same-origin allow-popups allow-downloads"
          className="h-full w-full border-0 bg-white"
        />
      ) : (
        <div
          role={visibleFailure ? "alert" : "status"}
          className="flex h-full flex-col items-center justify-center gap-hair px-body text-center font-ui"
        >
          <p className="text-[0.9rem] text-text-muted">
            {visibleFailure ? "Preview unavailable" : "Preparing Preview"}
          </p>
          <p className="max-w-measure text-[0.8rem] text-text-faint">
            {previewStagePlaceholderMessage(visibleFailure, dataReason)}
          </p>
          {visibleFailure && isPreviewReasonCode(visibleFailure) && (
            <p className="font-mono text-[0.7rem] text-text-faint">Reason code: {visibleFailure}</p>
          )}
          {visibleFailure && (
            <div className="mt-hair flex flex-wrap items-center justify-center gap-inline">
              <button
                type="button"
                onClick={onRefresh}
                aria-label="Refresh Preview"
                data-disco-control="build.preview-failure-refresh"
                className="flex max-lg:min-h-11 items-center gap-hair rounded-control border border-hairline px-inline py-hair text-[0.78rem] text-text-muted transition-colors hover:text-text"
              >
                <RotateCw className="size-3.5" aria-hidden /> Refresh
              </button>
              {onDownloadSource && (
                <button
                  type="button"
                  onClick={onDownloadSource}
                  disabled={downloadPending}
                  aria-label="Download source"
                  title="Download the project source (.zip)"
                  data-disco-control="build.preview-failure-download"
                  className="flex max-lg:min-h-11 items-center gap-hair rounded-control border border-hairline px-inline py-hair text-[0.78rem] text-text-muted transition-colors hover:text-text disabled:opacity-40"
                >
                  <Download className="size-3.5" aria-hidden />
                  {downloadPending ? "Preparing…" : "Download source"}
                </button>
              )}
            </div>
          )}
        </div>
      )}
      {showFrame &&
        shouldShowStatusOverlay(launch, minting, runtimeUnavailable, visibleFailure, frameReady) && (
          <div
            role={statusOverlayRole(visibleFailure, runtimeUnavailable)}
            data-testid="preview-status-overlay"
            // UI-15: the strip spans the FULL stage width. Inset from the sides it
            // left a few pixels of the frame's own first line showing past its left
            // edge — when that frame was a bare proxy error ("preview origin
            // expired"), the user saw a stray "p" glyph floating beside this
            // message. A full-bleed strip covers the line it is speaking for.
            className="pointer-events-none absolute inset-x-0 top-0 z-20 border-b border-hairline bg-bg/95 px-body py-hair font-ui text-[0.76rem] text-text-muted shadow-sm backdrop-blur"
          >
            {statusOverlayMessage(visibleFailure, runtimeUnavailable, dataReason)}
          </div>
        )}
      <SelectionOverlay
        armed={editMode && selection.armed}
        untrusted={untrusted}
        selection={selection.selection}
        onArm={selection.arm}
        onDisarm={onDisarmEdit}
        onWalkUp={selection.walkUp}
        onDiscuss={
          onSteer
            ? (selected) => {
                onSteer(formatSelectionContext(selected));
                selection.resetSelection();
                onDisarmEdit();
              }
            : undefined
        }
      />
      {editMode && selection.selection !== null && (
        <EditAffordance
          targetLabel={selection.selection.human_label}
          rect={selection.selection.rect}
          onApply={onApplyEdit}
          onCancel={selection.resetSelection}
        />
      )}
    </div>
  );
}
