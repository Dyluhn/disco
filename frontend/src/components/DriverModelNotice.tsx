import { cn } from "@/lib/cn";
import { TAP_TARGET } from "@/lib/tapTarget";
import { useDriverModels } from "@/hooks/useDriverModels";

/**
 * The driver-model notice — a faint, notification-styled line in the composer
 * card's bottom-right corner naming the model that currently drives the
 * surface. It deliberately does not LOOK interactive (it is a notice, not a
 * config control), but clicking it opens the composer's options disclosure and
 * hands focus to the real model control there.
 *
 * ONE label source across all surfaces: the agent-server driver catalogue's
 * short display label (what BuildModelPicker's face shows), resolved here by
 * `modelId`. The caller-supplied `label` (each surface's own catalogue label)
 * is only the fallback for models the driver catalogue doesn't list, with any
 * role prefix stripped. No new endpoint either way.
 */
interface Props {
  /** Fallback display label from the surface's own catalogue; renders nothing
   *  when neither source resolves (no false "Default" claim). */
  label: string | null;
  /** The effective model id — resolved against the driver catalogue so every
   *  surface's notice shows the same short name for the same model. */
  modelId?: string | null;
  /** Stable registry id for the click-coverage harness (per surface). */
  controlId: string;
  /** Opens the options disclosure and focuses/scrolls the model control. */
  onReveal: () => void;
}

/** Catalogue labels may carry a "Driver Local — " / "Driver Overflow — " role
 * prefix; the notice shows just the model name (the role is the pickers'
 * concern, not the notice's). */
function modelNameOnly(label: string): string {
  return label.replace(/^Driver\s[^—]*—\s*/u, "");
}

export function DriverModelNotice({ label, modelId, controlId, onReveal }: Props) {
  const { data } = useDriverModels();
  const driverLabel =
    modelId != null ? data?.models.find((m) => m.id === modelId)?.label ?? null : null;
  const source = driverLabel ?? label;
  if (!source) return null;
  const name = modelNameOnly(source);
  return (
    <button
      type="button"
      onClick={onReveal}
      aria-label={`Driver model: ${name} — open model options`}
      title="Change the model in the options below"
      data-disco-control={controlId}
      className={cn(
        "inline-flex items-center justify-end self-end px-hair font-ui text-[0.68rem] text-text-faint transition-colors hover:text-text-muted",
        TAP_TARGET,
      )}
    >
      {name}
    </button>
  );
}
