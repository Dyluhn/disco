/**
 * Settings → Build kernel selector (Disco Pi Build Kernel Campaign A2).
 *
 * Which inner Build agent the agent-server runs: `disco` (the current AgentLoop,
 * default) or `pi_experimental` (the experimental Pi-SDK kernel). The experimental
 * option is gated by the server's experimental access flag. The server
 * reports whether it is on (`experimental_enabled`); when off, the `pi_experimental`
 * choice is locked (no false affordance) and the server refuses to persist it.
 */

import { Cpu, FlaskConical } from "lucide-react";
import { cn } from "@/lib/cn";
import {
  useBuildKernelConfig,
  useUpdateBuildKernelConfig,
} from "@/hooks/useModels";

export function BuildKernelSection() {
  const { data, isLoading } = useBuildKernelConfig();
  const save = useUpdateBuildKernelConfig();

  const experimentalEnabled = data?.experimental_enabled ?? false;
  // A stale persisted `pi_experimental` reads as `disco` when the gate is off, so the
  // shown state is never ambiguous about which kernel actually runs.
  const effectiveKind =
    data?.kind === "pi_experimental" && experimentalEnabled
      ? "pi_experimental"
      : "disco";

  return (
    <section className="flex flex-col gap-inline">
      <header>
        <h3 className="font-ui text-[0.95rem] font-semibold text-text">
          Build kernel
        </h3>
        <p className="mt-hair font-ui text-[0.86rem] text-text-muted">
          Which inner agent drives the Build surface. Disco is the production
          loop. The experimental Pi kernel is a work in progress and only
          appears when the server has the experimental flag enabled.
        </p>
      </header>

      {isLoading || !data ? (
        <p className="font-ui text-[0.86rem] text-text-faint">Loading…</p>
      ) : (
        <div className="flex flex-col gap-inline">
          {!experimentalEnabled && (
            <p
              data-build-kernel-experimental="off"
              className="flex items-start gap-hair rounded-control border border-weak/50 px-body py-inline font-ui text-[0.82rem] leading-snug text-text-muted"
            >
              <FlaskConical
                className="mt-px size-4 shrink-0 text-text-faint"
                aria-hidden
              />
              <span>
                The experimental Pi kernel is off on this server. Until
                experimental access is enabled, only the Disco kernel can be
                selected.
              </span>
            </p>
          )}
          {(
            [
              {
                kind: "disco",
                Icon: Cpu,
                label: "Disco (default)",
                help: "The current Disco AgentLoop — planning gate, blast-radius confirmation, tools, and the full build loop.",
              },
              {
                kind: "pi_experimental",
                Icon: FlaskConical,
                label: "Pi (experimental)",
                help: "The experimental Pi-SDK kernel. Not implemented yet — selecting it requires the server experimental flag.",
              },
            ] as const
          ).map(({ kind, Icon, label, help }) => {
            const isActive = effectiveKind === kind;
            // The experimental choice is locked off until the server flag is on. Kept
            // clickable (not natively disabled) so a click is a visible no-op rather than
            // silently dead — but it never calls save.
            const locked = kind === "pi_experimental" && !experimentalEnabled;
            return (
              <button
                key={kind}
                type="button"
                data-disco-control="settings.build-kernel-select"
                data-kind={kind}
                data-locked={locked || undefined}
                onClick={() => {
                  if (locked) return;
                  if (!isActive) save.mutate({ kind });
                }}
                disabled={save.isPending}
                aria-disabled={locked || undefined}
                aria-pressed={isActive}
                className={cn(
                  "flex items-start gap-inline rounded-card border px-body py-inline text-left transition-colors",
                  isActive
                    ? "border-accent/50 bg-accent/5"
                    : "border-hairline hover:border-hairline-strong",
                  save.isPending && "opacity-60",
                  locked && "cursor-not-allowed opacity-50",
                )}
              >
                <Icon
                  className={cn(
                    "mt-px size-4 shrink-0",
                    isActive ? "text-accent" : "text-text-faint",
                  )}
                  aria-hidden
                />
                <span className="flex min-w-0 flex-col gap-hair">
                  <span className="font-ui text-[0.9rem] font-medium text-text">
                    {label}
                  </span>
                  <span className="font-ui text-[0.8rem] leading-relaxed text-text-faint">
                    {help}
                  </span>
                </span>
              </button>
            );
          })}
          {save.error && (
            <p className="font-ui text-[0.8rem] text-warn">
              Couldn't save: {(save.error as Error).message}
            </p>
          )}
        </div>
      )}
    </section>
  );
}
