import { AlertTriangle } from "lucide-react";
import { MODES, useMode } from "@/shell/mode";

/** The default hero copy is the standard Research framing. Other surfaces pass
 * their own descriptor so the caption names the active surface. */
const DEFAULT_TITLE = "Research";
const DEFAULT_SUBTITLE = "Sourced answers on anything.";

/** Pre-first-query empty state. The brand is the constant — the Disco wordmark
 * with its Latin dictionary entry — and the active surface reads as a quiet
 * subchip row beneath it (the surfaces are framings of Disco, not separate
 * products). `title`/`subtitle` name the active surface and its descriptor. */
export function EmptyState({
  title = DEFAULT_TITLE,
  subtitle = DEFAULT_SUBTITLE,
}: {
  title?: string;
  subtitle?: string;
} = {}) {
  const { mode, setMode } = useMode();
  return (
    <div className="flex flex-col items-center text-center">
      <h1 className="font-display text-[3rem] font-light tracking-tight text-text">Disco</h1>
      {/* the Latin entry — same copy as the brand chrome (core/brand/mark.py) */}
      <p className="mt-1 font-reading text-[0.95rem] italic leading-relaxed text-text-muted">
        <span className="not-italic font-mono text-[0.8rem] text-text-faint">/ˈdɪs.koː/</span>
        <span className="mx-2 text-text-faint">·</span>
        Latin, verb
        <span className="mx-2 text-text-faint">·</span>
        I learn; I become acquainted with.
      </p>
      {/* surface subchips — the active framing, switchable in place */}
      <div className="mt-6 flex items-center gap-2" data-disco-control="splash.surface-chips">
        {MODES.filter((m) => !m.dormant).map((m) => (
          <button
            key={m.id}
            type="button"
            onClick={() => setMode(m.id)}
            aria-pressed={m.id === mode}
            className={
              m.id === mode
                ? "rounded-full border border-hairline-strong bg-surface-1 px-4 py-1 font-ui text-[0.8rem] text-text"
                : "rounded-full border border-transparent px-4 py-1 font-ui text-[0.8rem] text-text-faint transition-colors hover:text-text-muted"
            }
          >
            {m.label}
          </button>
        ))}
      </div>
      <p className="mt-3 max-w-measure font-reading text-[0.95rem] leading-relaxed text-text-muted">
        {title === DEFAULT_TITLE || MODES.some((m) => m.label === title.toLowerCase())
          ? subtitle
          : `${title} — ${subtitle}`}
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
