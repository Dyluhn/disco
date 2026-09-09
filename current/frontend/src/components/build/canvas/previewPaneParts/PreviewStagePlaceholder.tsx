import { Download, RotateCw } from "lucide-react";
import { isPreviewReasonCode, previewStagePlaceholderMessage } from "./helpers";

/** What the stage shows INSTEAD of the preview frame.
 *
 * Two states, one card: "Preparing Preview" while the launch is still being
 * minted, and the actionable failure card once something has gone wrong. The
 * failure card is the UI-44 replacement for a frame that never loaded — it
 * carries the plain-words explanation, the raw reason code as a secondary
 * line, and the Refresh / Download source actions. Plain-value props only, so
 * this part stays inside the same bounded context as the stage. */
export function PreviewStagePlaceholder({
  visibleFailure,
  dataReason,
  onRefresh,
  onDownloadSource,
  downloadPending,
}: {
  visibleFailure: string | null;
  dataReason: string | undefined;
  onRefresh: () => void;
  /** Null when there is no conversation to download (the static run view). */
  onDownloadSource: (() => void) | null;
  downloadPending: boolean;
}) {
  return (
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
  );
}
