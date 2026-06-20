import { cn } from "@/lib/cn";
import type { Template } from "@/hooks/useTemplates";

/** A compact template selector — a swatch (the chosen template's bg + accent dot)
 *  next to a native <select>. Shared by the DR-report PDF export and the slide-deck
 *  export so the two surfaces look and behave identically. */
export function TemplatePicker({
  templates,
  value,
  onChange,
  disabled,
  label = "Template",
  id = "template-picker",
}: {
  templates: Template[];
  value: string;
  onChange: (id: string) => void;
  disabled?: boolean;
  label?: string;
  id?: string;
}) {
  const current = templates.find((t) => t.id === value) ?? templates[0];
  return (
    <div className="flex items-center justify-between gap-inline px-inline">
      <label htmlFor={id} className="font-ui text-[0.74rem] text-text-faint">
        {label}
      </label>
      <div className="flex items-center gap-hair">
        {/* Swatch: template canvas with an accent dot — a tiny live preview. */}
        <span
          aria-hidden
          className="size-3.5 shrink-0 rounded-full border border-hairline"
          style={{ background: current?.bg ?? "#fff" }}
        >
          <span
            className="block size-1.5 translate-x-[3px] translate-y-[3px] rounded-full"
            style={{ background: current?.accent ?? "#888" }}
          />
        </span>
        <select
          id={id}
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
          title={current?.description}
          className={cn(
            "rounded-control border border-hairline bg-surface-1 px-inline py-px font-ui text-[0.74rem] text-text",
            "focus:border-hairline-strong focus:outline-none",
            disabled && "opacity-60",
          )}
        >
          {templates.map((t) => (
            <option key={t.id} value={t.id}>
              {t.label}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}
