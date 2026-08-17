import { useId } from "react";
import { useSecrets } from "@/hooks/useSecrets";
import { cn } from "@/lib/cn";

const DEFAULT_FIELD_CLASS =
  "w-full rounded-control border border-hairline bg-bg px-inline py-hair font-mono text-[0.78rem] text-text outline-none transition-colors placeholder:text-text-faint focus:border-accent/60";

/**
 * A credential reference remains an env-var-style name on the wire. This field
 * keeps that contract, but lets people choose names already stored in Disco
 * instead of having to remember and retype them. Unknown legacy names remain
 * valid, and secret values are never read or rendered.
 */
export function StoredCredentialField({
  label,
  value,
  onChange,
  placeholder,
  optional = true,
  className,
  inputClassName,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  optional?: boolean;
  className?: string;
  inputClassName?: string;
}) {
  const { data } = useSecrets();
  const rawId = useId();
  const listId = `stored-credentials-${rawId.replace(/[^A-Za-z0-9_-]/g, "")}`;
  const names = [...new Set([...(data?.names ?? []), value].filter(Boolean))].sort();

  return (
    <label className={cn("flex flex-col gap-hair", className)}>
      <span className="flex items-baseline gap-hair font-ui text-[0.8rem] text-text">
        {label}
        {optional && (
          <span className="font-ui text-[0.72rem] text-text-faint">
            · optional
          </span>
        )}
      </span>
      <input
        aria-label={label}
        list={listId}
        spellCheck={false}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        className={cn(DEFAULT_FIELD_CLASS, inputClassName)}
      />
      <datalist id={listId}>
        {names.map((name) => (
          <option key={name} value={name} />
        ))}
      </datalist>
      <span className="font-ui text-[0.72rem] leading-snug text-text-faint">
        Choose a stored credential, or enter an environment key name.
      </span>
    </label>
  );
}
