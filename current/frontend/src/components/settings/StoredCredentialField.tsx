import { useId, useState } from "react";
import type { FormEvent } from "react";
import { Check, KeyRound, Plus, X } from "lucide-react";
import { useSecrets, useSetSecret } from "@/hooks/useSecrets";
import { cn } from "@/lib/cn";
import { TAP_TARGET } from "@/lib/tapTarget";

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
  suggestedName,
  optional = true,
  className,
  inputClassName,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  suggestedName?: string;
  optional?: boolean;
  className?: string;
  inputClassName?: string;
}) {
  const { data } = useSecrets();
  const setSecret = useSetSecret();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [newValue, setNewValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const rawId = useId();
  const listId = `stored-credentials-${rawId.replace(/[^A-Za-z0-9_-]/g, "")}`;
  const names = [...new Set([...(data?.names ?? []), value].filter(Boolean))].sort();

  const openDialog = () => {
    setError(null);
    setNewName((current) => current || suggestedName || "");
    setDialogOpen(true);
  };

  const choose = (name: string) => {
    onChange(name);
    setDialogOpen(false);
  };

  const add = async (event: FormEvent) => {
    event.preventDefault();
    const name = newName.trim();
    const secret = newValue.trim();
    if (!name || !secret) return;
    setError(null);
    try {
      await setSecret.mutateAsync({ name, value: secret });
      onChange(name);
      setNewName("");
      setNewValue("");
      setDialogOpen(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Couldn't save credential.");
    }
  };

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
      <div className="flex gap-hair">
        <input
          aria-label={label}
          list={listId}
          spellCheck={false}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder={placeholder}
          className={cn(DEFAULT_FIELD_CLASS, TAP_TARGET, inputClassName)}
        />
        <button
          type="button"
          className="min-h-11 shrink-0 rounded-control border border-hairline px-inline py-hair font-ui text-[0.76rem] text-text-muted hover:border-hairline-strong hover:text-text lg:min-h-0"
          onClick={openDialog}
          aria-haspopup="dialog"
        >
          Choose or add
        </button>
      </div>
      <datalist id={listId}>
        {names.map((name) => (
          <option key={name} value={name} />
        ))}
      </datalist>
      <span className="font-ui text-[0.72rem] leading-snug text-text-faint">
        Choose a stored credential, add one securely, or enter an environment key name.
      </span>

      {dialogOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-bg/70 p-body backdrop-blur-sm"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setDialogOpen(false);
          }}
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby={`${listId}-dialog-title`}
            className="flex w-full max-w-lg flex-col gap-body rounded-card border border-hairline bg-surface-1 p-section shadow-2xl"
          >
            <header className="flex items-start justify-between gap-inline">
              <div>
                <h3 id={`${listId}-dialog-title`} className="font-ui text-[0.96rem] font-semibold text-text">
                  Choose or add credential
                </h3>
                <p className="mt-hair font-ui text-[0.78rem] text-text-muted">
                  Values are encrypted at rest and never shown after saving.
                </p>
              </div>
              <button
                type="button"
                onClick={() => setDialogOpen(false)}
                aria-label="Close credential dialog"
                className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-control text-text-muted hover:text-text lg:min-h-0 lg:min-w-0"
              >
                <X className="size-4" aria-hidden />
              </button>
            </header>

            {names.length > 0 && (
              <div className="flex flex-col gap-hair">
                <span className="font-ui text-[0.76rem] font-medium uppercase tracking-wide text-text-faint">
                  Stored credentials
                </span>
                <ul className="flex max-h-48 flex-col gap-hair overflow-y-auto">
                  {names.map((name) => (
                    <li key={name}>
                      <button
                        type="button"
                        onClick={() => choose(name)}
                        className="flex w-full items-center justify-between gap-inline rounded-control border border-hairline bg-bg px-body py-inline text-left hover:border-accent/60"
                      >
                        <span className="flex items-center gap-hair font-mono text-[0.8rem] text-text">
                          <KeyRound className="size-3.5 text-supported" aria-hidden />
                          {name}
                        </span>
                        {name === value && <Check className="size-3.5 text-supported" aria-label="Selected" />}
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            <form onSubmit={add} className="flex flex-col gap-inline border-t border-hairline pt-body">
              <span className="flex items-center gap-hair font-ui text-[0.76rem] font-medium uppercase tracking-wide text-text-faint">
                <Plus className="size-3.5" aria-hidden /> Add a credential
              </span>
              <label className="flex flex-col gap-hair">
                <span className="font-ui text-[0.78rem] text-text-muted">Credential name</span>
                <input
                  value={newName}
                  onChange={(event) => setNewName(event.target.value)}
                  placeholder={suggestedName || placeholder}
                  aria-label="New credential name"
                  className={cn(DEFAULT_FIELD_CLASS, TAP_TARGET)}
                  autoComplete="off"
                />
              </label>
              <label className="flex flex-col gap-hair">
                <span className="font-ui text-[0.78rem] text-text-muted">Secret value</span>
                <input
                  type="password"
                  value={newValue}
                  onChange={(event) => setNewValue(event.target.value)}
                  placeholder="Value (encrypted at rest)"
                  aria-label="New credential value"
                  className={cn(DEFAULT_FIELD_CLASS, TAP_TARGET)}
                  autoComplete="new-password"
                />
              </label>
              {error && <p role="alert" className="font-ui text-[0.78rem] text-unsupported">{error}</p>}
              <div className="flex justify-end gap-inline">
                <button type="button" onClick={() => setDialogOpen(false)} className="min-h-11 rounded-control border border-hairline px-body py-hair font-ui text-[0.8rem] text-text-muted lg:min-h-0">Cancel</button>
                <button type="submit" disabled={setSecret.isPending || !newName.trim() || !newValue.trim()} className="min-h-11 rounded-control bg-accent px-body py-hair font-ui text-[0.8rem] font-medium text-bg disabled:opacity-60 lg:min-h-0">
                  {setSecret.isPending ? "Saving…" : "Save credential"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </label>
  );
}
