import { AlertTriangle } from "lucide-react";

/** The default hero copy is the standard Research framing. Other surfaces pass
 * their own title/subtitle so the landing block names the active surface. */
const DEFAULT_TITLE = "Research";
const DEFAULT_SUBTITLE = "Sourced answers on anything.";

/** Pre-first-query empty state — calm, reflects the philosophy (a vessel for
 * facts). The display face appears here as the active surface title. */
export function EmptyState({
  title = DEFAULT_TITLE,
  subtitle = DEFAULT_SUBTITLE,
}: {
  title?: string;
  subtitle?: string;
} = {}) {
  return (
    <div className="flex flex-col items-center gap-section text-center">
      <h1 className="font-display text-[2.6rem] font-light tracking-tight text-text">{title}</h1>
      <p className="max-w-measure font-reading text-[1.05rem] leading-relaxed text-text-muted">
        {subtitle}
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
