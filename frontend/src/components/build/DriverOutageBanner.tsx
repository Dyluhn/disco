/**
 * Honest provider-outage banner. When a run parks because the model provider
 * stayed unavailable through the driver's bounded retry ladder AND the provider
 * answered with an HTTP status, name the condition instead of hiding it behind
 * generic "blocked/paused" copy: a 429 is a usage/rate limit (resume when quota
 * returns), any other status is a provider outage. The adapter's sanitized
 * provider line is shown verbatim in mono (mirrors ErrorState's
 * verbatim-provider-message pattern; the raw body never reaches the client).
 *
 * NON-interactive by design — the Resume button (AgentStatusBar) and the answer
 * composer (AskPanel) are the already-wired continuations; this only informs.
 */

import { AlertTriangle } from "lucide-react";
import type { DriverOutage } from "@/hooks/useBuildStream";

export function DriverOutageBanner({ outage }: { outage: DriverOutage }) {
  const headline =
    outage.httpStatus === 429
      ? "The model provider reported a usage/rate limit (HTTP 429) — the run paused after bounded retries. Resume when the provider's quota is available."
      : `The model provider is unavailable (HTTP ${outage.httpStatus}) — the run paused after bounded retries.`;
  return (
    <div
      role="status"
      data-testid="driver-outage-banner"
      className="flex w-full flex-col gap-hair rounded-card border border-hairline border-l-2 border-l-warn bg-surface-1 p-body"
    >
      <div className="flex items-start gap-hair font-ui text-[0.84rem] leading-relaxed text-text">
        <AlertTriangle className="mt-px size-4 shrink-0 text-warn" aria-hidden />
        <span>{headline}</span>
      </div>
      {outage.message && (
        // the provider's sanitized line, verbatim (mono so it reads as raw output)
        <p className="rounded-control border-l-2 border-hairline-strong bg-bg px-inline py-hair font-mono text-[0.8rem] leading-relaxed text-text-muted">
          {outage.message}
        </p>
      )}
    </div>
  );
}
