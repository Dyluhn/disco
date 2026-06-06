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
import { BACKEND_META, backendMeta, fieldLabel, type SandboxConfig } from "@/types/sandbox";

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

      {/* connection — non-secret host/socket/runtime detail (keyless over Tailscale SSH) */}
      {meta && (
        <div className="flex flex-col gap-inline">
          {meta.fields.map((f) => (
            <label key={f} className="flex flex-col gap-hair">
              <span className="font-ui text-[0.78rem] text-text-muted">{fieldLabel(f)}</span>
              <input
                value={draft[f]}
                spellCheck={false}
                disabled={meta.stub}
                onChange={(e) => setDraft({ ...draft, [f]: e.target.value })}
                className="rounded-control border border-hairline bg-surface-1 px-inline py-hair font-mono text-[0.82rem] text-text outline-none transition-colors focus:border-hairline-strong disabled:opacity-50"
              />
            </label>
          ))}
          {meta.stub && (
            <p className="font-ui text-[0.76rem] text-weak">
              Podman is a stub in this environment — it configures but doesn’t run here; completed at deployment.
            </p>
          )}
        </div>
      )}

      <div className="flex items-center gap-inline">
        <button
          type="button"
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
