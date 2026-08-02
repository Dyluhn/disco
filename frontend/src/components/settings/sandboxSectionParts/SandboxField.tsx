/**
 * W-49: one labelled connection input — reused for the primary field and each
 * Advanced field so the markup (and the disabled-while-stub behaviour) stays in sync.
 * Relocated out of SandboxSection.tsx (still a private, non-exported implementation
 * detail) so SandboxConnectionFields can use it too.
 */

import type { SandboxField as SandboxFieldId } from "@/types/sandbox";

export function SandboxField({
  field,
  label,
  value,
  disabled,
  onChange,
}: {
  field: SandboxFieldId;
  label: string;
  value: string;
  disabled: boolean;
  onChange: (v: string) => void;
}) {
  return (
    <label className="flex flex-col gap-hair">
      <span className="font-ui text-[0.78rem] text-text-muted">{label}</span>
      <input
        data-sandbox-field={field}
        value={value}
        spellCheck={false}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        className="rounded-control border border-hairline bg-surface-1 px-inline py-hair font-mono text-[0.82rem] text-text outline-none transition-colors focus:border-hairline-strong disabled:opacity-50"
      />
    </label>
  );
}
