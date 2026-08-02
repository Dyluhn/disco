import { AlertTriangle } from "lucide-react";
import { cn } from "@/lib/cn";
import type { ApiErrorPredicate } from "./helpers";
import { errorText } from "./helpers";
import { FIELD } from "./styles";

/** Shown when a provider's /models probe fails: the honest failure + a manual
 * "add by model ID" escape hatch (context window is REQUIRED here — there's no
 * catalogue to trust for a default). */
export function CatalogueErrorBanner({
  providerLabel,
  error,
  isApiError,
  manualId,
  setManualId,
  manualCtx,
  setManualCtx,
  onManualAdd,
  addPending,
}: {
  providerLabel: string;
  error: unknown;
  isApiError: ApiErrorPredicate;
  manualId: string;
  setManualId: (value: string) => void;
  manualCtx: string;
  setManualCtx: (value: string) => void;
  onManualAdd: (e: React.FormEvent) => void;
  addPending: boolean;
}) {
  return (
    <div
      role="alert"
      className="flex flex-col gap-inline rounded-control border border-warn/50 bg-warn/5 px-body py-inline"
    >
      <div className="flex items-start gap-hair">
        <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-warn" aria-hidden />
        <p className="font-ui text-[0.8rem] text-text">
          {providerLabel} did not return a usable /models catalogue:{" "}
          <span className="text-text-muted">{errorText(error, isApiError)}</span>
        </p>
      </div>
      <form onSubmit={onManualAdd} className="flex gap-inline">
        <input
          value={manualId}
          onChange={(e) => setManualId(e.target.value)}
          aria-label={`Manual model ID for ${providerLabel}`}
          placeholder="model ID"
          className={cn(FIELD, "font-mono")}
        />
        <input
          value={manualCtx}
          onChange={(e) => setManualCtx(e.target.value)}
          inputMode="numeric"
          aria-label={`Context window for manual model on ${providerLabel}`}
          placeholder="context window"
          className={cn(FIELD, "w-36 font-mono")}
        />
        <button
          type="submit"
          data-disco-control="settings.provider-manual-add"
          disabled={!manualId.trim() || !(Number(manualCtx) >= 1024) || addPending}
          className="shrink-0 rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:border-hairline-strong hover:text-text disabled:opacity-50"
        >
          Add model ID
        </button>
      </form>
    </div>
  );
}
