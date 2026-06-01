import { AlertTriangle } from "lucide-react";

/** Pre-first-query empty state — calm, reflects the philosophy (a vessel for
 * facts). The wordmark is the one place the display face appears. */
export function EmptyState() {
  return (
    <div className="flex flex-col items-center gap-section text-center">
      <div className="font-display text-[2.6rem] font-light tracking-tight text-text">
        perpleximanus
      </div>
      <p className="max-w-measure font-reading text-[1.05rem] leading-relaxed text-text-muted">
        Ask a question. Get a document-grade answer — every claim tethered to a source you can
        verify, and the ones that aren't, marked as such.
      </p>
    </div>
  );
}

/**
 * A model/provider error — the model couldn't perform the request and the
 * provider said so (Prompt 3E, reactive surfacing). The REAL provider content is
 * shown verbatim, never flattened to "something failed" and never swallowed. This
 * is distinct from a single SOURCE failing (paywalled/blocked), which renders
 * inline in the source panel.
 */
export function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div
      role="alert"
      className="mx-auto flex w-full max-w-measure flex-col gap-inline rounded-card border border-hairline border-l-2 border-l-warn bg-surface-1 p-body"
    >
      <div className="flex items-center gap-hair font-ui text-[0.9rem] font-medium text-text">
        <AlertTriangle className="size-4 text-warn" aria-hidden />
        The model returned an error
      </div>
      {/* the provider's real message, verbatim (mono so it reads as raw output) */}
      <p className="rounded-control border-l-2 border-hairline-strong bg-bg px-inline py-hair font-mono text-[0.8rem] leading-relaxed text-text-muted">
        {message}
      </p>
      <div>
        <button
          type="button"
          onClick={onRetry}
          className="rounded-control border border-hairline px-body py-hair font-ui text-[0.84rem] text-text-muted transition-colors hover:text-text"
        >
          Try again
        </button>
      </div>
    </div>
  );
}
