import { Loader2 } from "lucide-react";
import { cn } from "@/lib/cn";
import { OPTIONS, type Provider } from "./providerOptions";

export function ProviderOptionList({
  active,
  pending,
  onSelect,
}: {
  active: Provider | undefined;
  pending: boolean;
  onSelect: (provider: Provider) => void;
}) {
  return (
    <>
      {OPTIONS.map((opt) => {
        const isActive = active === opt.provider;
        return (
          <button
            key={opt.provider}
            type="button"
            data-disco-control="settings.imagegen-provider"
            data-provider={opt.provider}
            onClick={() => onSelect(opt.provider)}
            disabled={pending}
            aria-pressed={isActive}
            className={cn(
              "flex items-start gap-inline rounded-card border px-body py-inline text-left transition-colors",
              isActive
                ? "border-accent/50 bg-accent/5"
                : "border-hairline hover:border-hairline-strong",
              pending && "opacity-60",
            )}
          >
            <opt.Icon
              className={cn(
                "mt-px size-4 shrink-0",
                isActive ? "text-accent" : "text-text-faint",
              )}
              aria-hidden
            />
            <span className="flex min-w-0 flex-col gap-hair">
              <span className="flex items-center gap-hair font-ui text-[0.9rem] font-medium text-text">
                {opt.label}
                {isActive && pending && (
                  <Loader2
                    className="size-3 animate-spin text-accent"
                    aria-hidden
                  />
                )}
              </span>
              <span className="font-ui text-[0.8rem] leading-relaxed text-text-faint">
                {opt.help}
              </span>
            </span>
          </button>
        );
      })}
    </>
  );
}
