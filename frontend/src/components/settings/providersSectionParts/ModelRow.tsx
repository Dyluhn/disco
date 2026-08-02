import { Check } from "lucide-react";
import { cn } from "@/lib/cn";
import type { ProviderCatalogueModel } from "@/types/models";
import { contextLabel, priceLabel } from "./helpers";
import { FIELD } from "./styles";
import { ToggleSwitch } from "./ToggleSwitch";

/** One row of the provider's model browser — the model's label/id/price plus
 * its enable/disable toggle and (when the catalogue omits a context window)
 * the inline "ask for one" form. */
export function ModelRow({
  model,
  providerLabel,
  enabled,
  pending,
  contextWindow,
  ctxAskValue,
  onToggle,
  onCtxAskChange,
  onCtxAskSubmit,
}: {
  model: ProviderCatalogueModel;
  providerLabel: string;
  enabled: boolean;
  pending: boolean;
  contextWindow: number | null | undefined;
  ctxAskValue: string | undefined;
  onToggle: () => void;
  onCtxAskChange: (value: string) => void;
  onCtxAskSubmit: (e: React.FormEvent) => void;
}) {
  const asking = ctxAskValue !== undefined;

  return (
    <li className="flex items-start justify-between gap-inline border-b border-hairline px-body py-inline last:border-b-0">
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-hair">
          {enabled && <Check className="size-3.5 shrink-0 text-supported" aria-hidden />}
          <span className="truncate font-ui text-[0.84rem] text-text">
            {model.label}
          </span>
        </div>
        <p className="break-all font-mono text-[0.7rem] text-text-faint">
          {model.model_id} · {contextLabel(contextWindow)} · {priceLabel(model)}
        </p>
      </div>
      <div className="flex shrink-0 flex-col items-end gap-hair">
        <ToggleSwitch
          checked={enabled}
          busy={pending}
          label={`${enabled ? "Disable" : "Enable"} ${model.model_id}`}
          onClick={onToggle}
        />
        {asking && (
          <form className="flex items-center gap-hair" onSubmit={onCtxAskSubmit}>
            <input
              autoFocus
              inputMode="numeric"
              value={ctxAskValue}
              onChange={(e) => onCtxAskChange(e.target.value)}
              aria-label={`Context window for ${model.model_id}`}
              placeholder="context window (tokens)"
              className={cn(FIELD, "w-44 font-mono text-[0.74rem]")}
            />
            <button
              type="submit"
              data-disco-control="settings.provider-ctx-confirm"
              disabled={!(Number(ctxAskValue) >= 1024)}
              className="shrink-0 rounded-control border border-hairline px-inline py-hair font-ui text-[0.74rem] text-text-muted transition-colors hover:text-text disabled:opacity-50"
            >
              Enable
            </button>
          </form>
        )}
        {asking && (
          <p className="max-w-56 text-right font-ui text-[0.7rem] text-text-faint">
            {providerLabel} doesn't report context windows — enter this
            model's real limit.
          </p>
        )}
      </div>
    </li>
  );
}
