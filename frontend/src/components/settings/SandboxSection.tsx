/**
 * Settings → Sandbox. Select + configure the backend the agent runs in. Reuses the
 * settings config plumbing (useSandboxConfig / useUpdateSandboxConfig → the shared store
 * the agent-server reads). Isolation tier is legible at the point of choice; the
 * local→tighter-confirmation coupling is surfaced; Podman is an honest, labeled stub;
 * the keyless tailnet connections are non-secret host/socket detail (no key entry).
 */

import { useEffect, useState } from "react";
import { AlertTriangle, ShieldCheck, ShieldHalf } from "lucide-react";
import { cn } from "@/lib/cn";
import { useSandboxConfig, useUpdateSandboxConfig } from "@/hooks/useModels";
import {
  BACKEND_META,
  backendMeta,
  fieldLabel,
  type SandboxConfig,
  type SandboxField as SandboxFieldId,
} from "@/types/sandbox";

/** W-49: one labelled connection input — reused for the primary field and each
 * Advanced field so the markup (and the disabled-while-stub behaviour) stays in sync. */
function SandboxField({
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

function BackendCard({
  id,
  selected,
  onSelect,
}: {
  id: string;
  selected: boolean;
  onSelect: () => void;
}) {
  const m = backendMeta(id)!;
  const Shield = m.adversarialSafe ? ShieldCheck : ShieldHalf;
  return (
    <button
      type="button"
      role="radio"
      data-disco-control="settings.sandbox-backend"
      data-backend-id={id}
      data-stub={m.stub ? true : undefined}
      aria-checked={selected}
      aria-label={`Use the ${m.name} sandbox backend`}
      onClick={onSelect}
      className={cn(
        "flex flex-col gap-hair rounded-card border p-body text-left transition-colors",
        selected ? "border-accent bg-surface-1" : "border-hairline hover:border-hairline-strong",
        m.stub && "opacity-90",
      )}
    >
      <div className="flex items-center gap-inline">
        <span className="font-ui text-[0.92rem] font-medium text-text">{m.name}</span>
        {m.stub && (
          <span className="flex items-center gap-hair rounded-full border border-weak px-inline py-px font-ui text-[0.64rem] uppercase tracking-wide text-weak">
            <AlertTriangle className="size-3" aria-hidden />
            stub here
          </span>
        )}
      </div>
      <span
        className={cn(
          "flex items-center gap-hair font-ui text-[0.72rem]",
          m.adversarialSafe ? "text-supported" : "text-weak",
        )}
      >
        <Shield className="size-3.5" aria-hidden />
        {m.tier}
      </span>
      <p className="font-ui text-[0.78rem] leading-snug text-text-muted">{m.blurb}</p>
    </button>
  );
}

export function SandboxSection() {
  const { data } = useSandboxConfig();
  const save = useUpdateSandboxConfig();
  const [draft, setDraft] = useState<SandboxConfig | null>(null);

  useEffect(() => {
    if (data && draft === null) setDraft(data);
  }, [data, draft]);

  if (!draft) {
    return (
      <section className="border-t border-hairline pt-section">
        <h2 className="font-display text-[1.3rem] tracking-tight text-text">Sandbox</h2>
        <p className="mt-inline font-ui text-[0.82rem] text-text-faint">Loading…</p>
      </section>
    );
  }

  const meta = backendMeta(draft.backend);
  const dirty = JSON.stringify(draft) !== JSON.stringify(data);

  const selectBackend = (id: string) =>
    setDraft({ ...draft, backend: id, runtime: backendMeta(id)?.defaultRuntime ?? draft.runtime });

  return (
    <section className="flex flex-col gap-section border-t border-hairline pt-section">
      <header>
        <h2 className="font-display text-[1.3rem] tracking-tight text-text">Sandbox</h2>
        <p className="mt-hair font-ui text-[0.86rem] text-text-muted">
          Where the agent runs its tools. Stronger isolation is safer for untrusted work;
          weaker, local isolation is fine for trusted tasks.
        </p>
      </header>

      <div role="radiogroup" aria-label="Sandbox backend" className="grid gap-inline sm:grid-cols-3">
        {BACKEND_META.map((b) => (
          <BackendCard
            key={b.id}
            id={b.id}
            selected={draft.backend === b.id}
            onSelect={() => selectBackend(b.id)}
          />
        ))}
      </div>

      {/* the isolation → confirmation coupling, surfaced for the selected backend */}
      {meta && (
        <p
          className={cn(
            "flex items-start gap-hair rounded-control border px-body py-inline font-ui text-[0.8rem] leading-snug",
            meta.adversarialSafe ? "border-hairline text-text-muted" : "border-weak/50 text-text-muted",
          )}
        >
          <ShieldHalf className="mt-px size-3.5 shrink-0 text-text-faint" aria-hidden />
          {meta.confirmNote}
        </p>
      )}

      {/* connection — W-49 progressive disclosure: ONE varying field up front, the rest
          (runtime is dropped from the form; image/workspace_root + any non-primary
          connection field) folded under Advanced with their saved defaults. Payload shape
          is unchanged — selectBackend seeds runtime, and the hidden fields keep their
          values, so Local + Save is zero typing. */}
      {meta && (
        <div className="flex flex-col gap-inline">
          {/* the "what you provide" subline — sets expectations before any input */}
          <p className="font-ui text-[0.78rem] text-text-faint">{meta.provides}</p>

          {/* the ONE connection field that varies per backend (null → none shown) */}
          {meta.primaryField && (
            <SandboxField
              field={meta.primaryField}
              label={meta.primaryLabel ?? fieldLabel(meta.primaryField)}
              value={draft[meta.primaryField]}
              disabled={meta.stub}
              onChange={(v) => setDraft({ ...draft, [meta.primaryField!]: v })}
            />
          )}

          {/* Advanced — everything else, collapsed by default. runtime is NOT in the form
              (it rides along in the saved payload via the per-backend default). */}
          {(() => {
            const advanced = meta.fields.filter(
              (f) => f !== "runtime" && f !== meta.primaryField,
            );
            if (advanced.length === 0) return null;
            return (
              <details className="rounded-control border border-hairline px-body py-inline">
                <summary
                  data-disco-control="settings.sandbox-advanced"
                  className="cursor-pointer font-ui text-[0.78rem] text-text-muted"
                >
                  Advanced
                </summary>
                <div className="mt-inline flex flex-col gap-inline">
                  {advanced.map((f) => (
                    <SandboxField
                      key={f}
                      field={f}
                      label={fieldLabel(f)}
                      value={draft[f]}
                      disabled={meta.stub}
                      onChange={(v) => setDraft({ ...draft, [f]: v })}
                    />
                  ))}
                </div>
              </details>
            );
          })()}

          {meta.stub && (
            <p data-disco-flag="sandbox-stub" className="font-ui text-[0.76rem] text-weak">
              Podman is a stub in this environment — it configures but doesn’t run here; completed at deployment.
            </p>
          )}
        </div>
      )}

      <div className="flex items-center gap-inline">
        <button
          type="button"
          data-disco-control="settings.sandbox-save"
          onClick={() => draft && save.mutate(draft)}
          disabled={!dirty || save.isPending}
          className="rounded-control bg-accent px-body py-hair font-ui text-[0.82rem] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          {save.isPending ? "Saving…" : dirty ? "Save sandbox" : "Saved"}
        </button>
        {save.error && (
          <span role="alert" className="font-ui text-[0.78rem] text-unsupported">
            {(save.error as Error).message}
          </span>
        )}
      </div>
    </section>
  );
}
