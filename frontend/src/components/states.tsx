import { AlertTriangle } from "lucide-react";
import { MODES } from "@/shell/mode";
import type { RunFailure, RunFailureClass } from "@/types/agent";

/** The default hero copy is the standard Research framing. Other surfaces pass
 * their own descriptor so the caption names the active surface. */
const DEFAULT_TITLE = "Research";
const DEFAULT_SUBTITLE = "Sourced answers on anything.";

/** Pre-first-query empty state. The brand is the constant — the Disco wordmark
 * with its Latin dictionary entry — with the surface's tagline directly under
 * it. Mode switching is the top bar's job alone (the old subchip row here
 * duplicated it exactly). `title`/`subtitle` name the active surface and its
 * descriptor. */
export function EmptyState({
  title = DEFAULT_TITLE,
  subtitle = DEFAULT_SUBTITLE,
}: {
  title?: string;
  subtitle?: string;
} = {}) {
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
      <p className="mt-3 max-w-measure font-reading text-[0.95rem] leading-relaxed text-text-muted">
        {title === DEFAULT_TITLE || MODES.some((m) => m.label === title.toLowerCase())
          ? subtitle
          : `${title} — ${subtitle}`}
      </p>
    </div>
  );
}

/** Which boundary raised, in the reader's words. The closed set is mirrored
 *  from `core.RunFailureClass`; a class with no entry here would be a silently
 *  unhandled branch, which the harness contract suite already fails on. */
const FAILURE_CLASS_LABEL: Record<RunFailureClass, string> = {
  search_infrastructure: "Search infrastructure",
  extraction_infrastructure: "Page extraction",
  no_usable_evidence: "No usable evidence",
  host_circuit_breaker: "Host circuit breaker",
  model_provider: "Model provider",
  model_protocol: "Model protocol",
  preflight: "Preflight check",
  internal_error: "Internal error",
};

/**
 * A terminal failure rendered AS THE FOUR PARTS the producer wrote, not as the
 * sentence it also renders from them.
 *
 * `failures.py` builds every deep-research failure as `why` / `state` / `next` /
 * `allowed` precisely so a reader never has to parse an explanation back out of
 * prose — and then the UI dropped all four and printed only `detail`. A wall
 * that says why it is up but not what state the run is in, what to do, or what
 * is still available scatters instead of funnelling.
 */
function RunFailureParts({ failure }: { failure: RunFailure }) {
  const parts: Array<{ label: string; body: string; raw?: boolean }> = [
    { label: "Why", body: failure.why, raw: true },
    { label: "Where the run stands", body: failure.state },
    { label: "Next", body: failure.next },
    { label: "Still available", body: failure.allowed },
  ];
  return (
    <dl className="flex flex-col gap-inline">
      {parts.map((part) => (
        <div key={part.label}>
          <dt className="font-ui text-[0.7rem] font-semibold uppercase tracking-wide text-text-faint">
            {part.label}
          </dt>
          <dd
            className={
              part.raw
                ? // `why` quotes the boundary's own message — mono, so it reads
                  // as the raw output it is.
                  "mt-hair rounded-control border-l-2 border-hairline-strong bg-bg px-inline py-hair font-mono text-[0.8rem] leading-relaxed text-text-muted"
                : "mt-hair font-ui text-[0.82rem] leading-snug text-text-muted"
            }
          >
            {part.body}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * A model/provider error — the model couldn't perform the request and the
 * provider said so (Prompt 3E, reactive surfacing). The REAL provider content is
 * shown verbatim, never flattened to "something failed" and never swallowed. This
 * is distinct from a single SOURCE failing (paywalled/blocked), which renders
 * inline in the source panel.
 *
 * When the producing code path named its own boundary (`ErrorEvent.failure`),
 * the four parts replace the single sentence and the heading names the class
 * that raised — "the model returned an error" is wrong for a dead search pool
 * or a failed preflight. `message` stays the fallback for everything else.
 */
export function ErrorState({
  message,
  onRetry,
  failure = null,
}: {
  message: string;
  onRetry: () => void;
  failure?: RunFailure | null;
}) {
  return (
    <div
      role="alert"
      className="mx-auto flex w-full max-w-measure flex-col gap-inline rounded-card border border-hairline border-l-2 border-l-warn bg-surface-1 p-body"
    >
      <div className="flex items-center gap-hair font-ui text-[0.9rem] font-medium text-text">
        <AlertTriangle className="size-4 text-warn" aria-hidden />
        {failure ? "The run stopped" : "The model returned an error"}
        {failure && (
          <span
            data-failure-class={failure.failure_class}
            className="ml-hair rounded-control border border-hairline px-inline font-mono text-[0.7rem] font-normal text-text-faint"
          >
            {FAILURE_CLASS_LABEL[failure.failure_class] ?? failure.failure_class}
          </span>
        )}
      </div>
      {failure ? (
        <RunFailureParts failure={failure} />
      ) : (
        /* the provider's real message, verbatim (mono so it reads as raw output) */
        <p className="rounded-control border-l-2 border-hairline-strong bg-bg px-inline py-hair font-mono text-[0.8rem] leading-relaxed text-text-muted">
          {message}
        </p>
      )}
      <div>
        <button
          type="button"
          onClick={onRetry}
          className="flex min-h-11 items-center justify-center rounded-control border border-hairline px-body py-hair font-ui text-[0.84rem] text-text-muted transition-colors hover:text-text lg:min-h-0"
        >
          Try again
        </button>
      </div>
    </div>
  );
}
