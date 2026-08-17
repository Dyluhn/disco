import type { LiveStartFailure } from "./useLiveBrowserSession";

/** The visible, bounded fallback notice for a live-browser auto-start failure —
 * honest instead of silently hiding that the requested live stream failed. */
export function LiveFailureBanner({ failure }: { failure: LiveStartFailure }) {
  if (!failure) return null;
  return (
    <div
      role="status"
      data-testid="live-fallback-notice"
      className="shrink-0 border-b border-warn/30 bg-warn/10 px-body py-hair font-ui text-[0.72rem] text-warn"
    >
      {failure === "retrying"
        ? "Live view temporarily unavailable; retrying. Showing captured screenshots meanwhile."
        : "Live view unavailable after bounded startup attempts. Showing captured screenshots instead."}
    </div>
  );
}
