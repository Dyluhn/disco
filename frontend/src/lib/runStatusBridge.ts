/**
 * Run-status publish seam for the W6 E2E bridge (gap #4).
 *
 * The active surface publishes its current conversation status here (and clears
 * it on unmount); the bridge getter in App.tsx reads it LIVE on every access, so
 * the harness can `await` the run reaching RUNNING / AWAITING_* / FINISHED
 * without a re-render of the mounter.
 *
 * Lives in its own module (not App.tsx) so the surfaces can import it without a
 * circular dependency back through the app root.
 */
let _publishedRunStatus: string | null = null;

/** Publish the active surface's live conversation status to the E2E bridge.
 *  Pass `null` to clear (e.g. on surface unmount). */
export function publishRunStatus(status: string | null): void {
  _publishedRunStatus = status;
}

/** The latest published run status, read live by the bridge getter. */
export function getPublishedRunStatus(): string | null {
  return _publishedRunStatus;
}
