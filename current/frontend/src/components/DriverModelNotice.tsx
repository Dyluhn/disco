import { cn } from "@/lib/cn";
import { TAP_TARGET } from "@/lib/tapTarget";

/**
 * The driver-model notice — a faint, notification-styled line in the composer
 * card's bottom-right corner naming the model that currently drives the
 * surface. It deliberately does not LOOK interactive (it is a notice, not a
 * config control), but clicking it opens the composer's options disclosure and
 * hands focus to the real model control there. The model name comes from the
 * same hooks the surface's existing picker reads — no new endpoint.
 */
interface Props {
  /** Display label of the effective driver model; renders nothing when the
   *  catalogue hasn't resolved one yet (no false "Default" claim). */
  label: string | null;
  /** Stable registry id for the click-coverage harness (per surface). */
  controlId: string;
  /** Opens the options disclosure and focuses/scrolls the model control. */
  onReveal: () => void;
}

export function DriverModelNotice({ label, controlId, onReveal }: Props) {
  if (!label) return null;
  return (
    <button
      type="button"
      onClick={onReveal}
      aria-label={`Driver model: ${label} — open model options`}
      title="Change the model in the options below"
      data-disco-control={controlId}
      className={cn(
        "inline-flex items-center justify-end self-end px-hair font-ui text-[0.68rem] text-text-faint transition-colors hover:text-text-muted",
        TAP_TARGET,
      )}
    >
      {label}
    </button>
  );
}
