/**
 * Settings → Sandbox. Select + configure the backend the agent runs in. Reuses the
 * settings config plumbing (useSandboxConfig / useUpdateSandboxConfig → the shared store
 * the agent-server reads). Isolation tier is legible at the point of choice; the
 * local→tighter-confirmation coupling is surfaced; Podman is temporarily hidden from the
 * picker (no live host in this environment — see BACKEND_META in types/sandbox.ts);
 * the keyless tailnet connections are non-secret host/socket detail (no key entry).
 */

import { useEffect, useState } from "react";
import { AlertTriangle, ShieldCheck, ShieldHalf } from "lucide-react";
import { cn } from "@/lib/cn";
import {
  useSandboxConfig,
  useUpdateSandboxConfig,
  useTestSandbox,
} from "@/hooks/useModels";
import type { ProbeResult } from "@/types/probe";
import {
  BACKEND_META,
  backendMeta,
  type SandboxConfig,
  type SandboxField as SandboxFieldId,
} from "@/types/sandbox";
import { SandboxConfirmNote } from "./sandboxSectionParts/SandboxConfirmNote";
import { SandboxConnectionFields } from "./sandboxSectionParts/SandboxConnectionFields";
import { SandboxSaveRow } from "./sandboxSectionParts/SandboxSaveRow";

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
        selected
          ? "border-accent bg-surface-1"
          : "border-hairline hover:border-hairline-strong",
        m.stub && "opacity-90",
      )}
    >
      <div className="flex items-center gap-inline">
        <span className="font-ui text-[0.92rem] font-medium text-text">
          {m.name}
        </span>
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
      <p className="font-ui text-[0.78rem] leading-snug text-text-muted">
        {m.blurb}
      </p>
    </button>
  );
}

export function SandboxSection() {
  const { data } = useSandboxConfig();
  const save = useUpdateSandboxConfig();
  const test = useTestSandbox();
  const [draft, setDraft] = useState<SandboxConfig | null>(null);
  const [probe, setProbe] = useState<ProbeResult | null>(null);

  useEffect(() => {
    if (data && draft === null) setDraft(data);
  }, [data, draft]);

  if (!draft) {
    return (
      <section className="flex flex-col gap-inline">
        <h3 className="font-ui text-[0.95rem] font-semibold text-text">
          Sandbox
        </h3>
        <p className="mt-inline font-ui text-[0.82rem] text-text-faint">
          Loading…
        </p>
      </section>
    );
  }

  const meta = backendMeta(draft.backend);
  const dirty = JSON.stringify(draft) !== JSON.stringify(data);

  const selectBackend = (id: string) => {
    const m = backendMeta(id);
    // Persistence fix (2026-06-23 outage + P1 bleed): RESTORE this backend's OWN saved
    // connection block — and NOTHING from the previous backend's active socket. The
    // original bug was a SHARED docker_socket that blanked gVisor's `ssh://sandbox@<host>`
    // to a host-less `ssh://sandbox@`; the P1 follow-up was that an old flat config's bad
    // gvisor socket bled into Local on switch. We pull ONLY connections[id] (the server
    // guarantees a clean backend-appropriate block for every backend) so each backend gets
    // its own slate. We deliberately do NOT spread `draft` here — that's what leaked the
    // old socket across backends.
    const saved =
      data?.connections?.[id] ??
      // Defensive fallback if the server didn't send a block: a clean per-backend default,
      // never the previous backend's socket. gVisor gets the prefill below; others stay local.
      (id === "gvisor"
        ? {
            docker_socket: "",
            podman_url: draft.podman_url,
            runtime: "runsc",
            image: draft.image,
            workspace_root: draft.workspace_root,
          }
        : id === "podman"
          ? {
              docker_socket: "",
              podman_url: draft.podman_url,
              runtime: "crun",
              image: draft.image,
              workspace_root: draft.workspace_root,
            }
          : {
              docker_socket: "unix:///var/run/docker.sock",
              podman_url: draft.podman_url,
              runtime: m?.defaultRuntime ?? "runc",
              image: draft.image,
              workspace_root: draft.workspace_root,
            });
    const next: SandboxConfig = {
      backend: id,
      docker_socket: saved.docker_socket,
      podman_url: saved.podman_url,
      runtime: saved.runtime || m?.defaultRuntime || draft.runtime,
      image: saved.image,
      workspace_root: saved.workspace_root,
      connections: draft.connections,
    };
    // W-48(b): seed the gVisor host template ONLY when its restored socket is EMPTY
    // (a first-time/unconfigured gvisor) — never overwrite a restored real host (any
    // user@host). After this the user only edits the host part of `ssh://sandbox@<host>`.
    if (m?.primaryField && m.primaryPrefill && !next[m.primaryField]) {
      next[m.primaryField] = m.primaryPrefill;
    }
    setProbe(null);
    setDraft(next);
  };

  const onFieldChange = (field: SandboxFieldId, value: string) =>
    setDraft({ ...draft, [field]: value });

  // W-48: preflight on SAVE — persist, then run the real connectivity probe and surface
  // a typed, host-naming reachability verdict (so a misconfigured/unreachable backend is
  // visible up-front, not as a silent failure on the first build).
  const onSave = () => {
    setProbe(null);
    save.mutate(draft, {
      onSuccess: () => test.mutate(draft, { onSuccess: setProbe }),
    });
  };
  const onTest = () => {
    setProbe(null);
    test.mutate(draft, { onSuccess: setProbe });
  };

  return (
    <section className="flex flex-col gap-inline">
      <header>
        <h3 className="font-ui text-[0.95rem] font-semibold text-text">
          Sandbox
        </h3>
        <p className="mt-hair font-ui text-[0.86rem] text-text-muted">
          Where the agent runs its tools. Stronger isolation is safer for
          untrusted work; weaker, local isolation is fine for trusted tasks.
        </p>
      </header>

      <div
        role="radiogroup"
        aria-label="Sandbox backend"
        className="grid gap-inline sm:grid-cols-2"
      >
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
      <SandboxConfirmNote meta={meta} />

      {/* connection — W-49 progressive disclosure: ONE varying field up front, the rest
          (runtime is dropped from the form; image/workspace_root + any non-primary
          connection field) folded under Advanced with their saved defaults. Payload shape
          is unchanged — selectBackend seeds runtime, and the hidden fields keep their
          values, so Local + Save is zero typing. */}
      <SandboxConnectionFields
        meta={meta}
        draft={draft}
        onFieldChange={onFieldChange}
      />

      <SandboxSaveRow
        dirty={dirty}
        savePending={save.isPending}
        testPending={test.isPending}
        stub={meta?.stub}
        onSave={onSave}
        onTest={onTest}
        saveError={save.error}
        probe={probe}
      />
    </section>
  );
}
