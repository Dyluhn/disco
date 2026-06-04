import { cn } from "@/lib/cn";
import { useSkills, useToggleSkill } from "@/hooks/useConfig";
import { NotWired } from "./NotWired";
import { PendingBadge } from "./PendingBadge";

/**
 * Skills configuration (Prompt 4) — view, enable/disable, and (eventually)
 * configure reusable capability modules. Scaffolded against fixtures and marked
 * WIRING-PENDING: the toggles reflect intent but the skills subsystem lands later,
 * so the surface doesn't pretend to control a live system. Looks complete; honest.
 */
function Switch({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        "relative h-5 w-9 shrink-0 rounded-full border transition-colors disabled:cursor-not-allowed disabled:opacity-50",
        checked ? "border-accent/50 bg-accent/30" : "border-hairline bg-surface-2",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "absolute top-1/2 size-3.5 -translate-y-1/2 rounded-full transition-all",
          checked ? "left-[1.15rem] bg-accent" : "left-[0.15rem] bg-text-faint",
        )}
      />
    </button>
  );
}

export function SkillsSection() {
  const { data: skills, isLoading } = useSkills();
  const toggle = useToggleSkill();

  return (
    <section aria-labelledby="skills-heading" className="flex flex-col gap-inline">
      <div className="flex items-center gap-inline">
        <h2 id="skills-heading" className="font-ui text-[1.05rem] font-semibold text-text">
          Skills
        </h2>
        <PendingBadge />
      </div>
      <p className="font-ui text-[0.84rem] text-text-muted">
        Reusable capability modules.
      </p>
      <NotWired detail="There is no skills subsystem yet: these toggles change nothing. 'Web research' actually runs (it's hardwired into the research pipeline, not gated by this toggle); 'Code execution' is not implemented at all (the agent loop has no tool executor). Toggles and Configure are disabled until the subsystem exists." />

      <ul className="overflow-hidden rounded-card border border-hairline bg-surface-1">
        {isLoading && (
          <li className="px-body py-body font-ui text-[0.84rem] text-text-muted">Loading skills…</li>
        )}
        {(skills ?? []).map((s) => (
          <li
            key={s.id}
            className="flex items-center justify-between gap-section border-b border-hairline px-body py-inline last:border-b-0"
          >
            <div className="min-w-0">
              <div className="font-ui text-[0.88rem] font-medium text-text">{s.name}</div>
              <p className="font-ui text-[0.8rem] leading-snug text-text-muted">{s.description}</p>
            </div>
            <div className="flex items-center gap-inline">
              <button
                type="button"
                disabled
                title="Configuration lands with the skills subsystem"
                className="rounded-control border border-hairline px-inline py-hair font-ui text-[0.76rem] text-text-faint"
              >
                Configure
              </button>
              <Switch
                checked={s.enabled}
                onChange={(next) => toggle.mutate({ id: s.id, enabled: next })}
                label={`Enable ${s.name}`}
                disabled
              />
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
