/**
 * The save/test/probe action row for SandboxSection — W-48's explicit
 * connectivity preflight. Pulled out of the component purely to shed
 * cyclomatic complexity (TS-0035); the parent still owns the draft/save/test
 * state and passes it down.
 */

import { cn } from "@/lib/cn";
import type { ProbeResult } from "@/types/probe";

export function SandboxSaveRow({
  dirty,
  savePending,
  testPending,
  stub,
  onSave,
  onTest,
  saveError,
  probe,
}: {
  dirty: boolean;
  savePending: boolean;
  testPending: boolean;
  stub: boolean | undefined;
  onSave: () => void;
  onTest: () => void;
  saveError: Error | null;
  probe: ProbeResult | null;
}) {
  return (
    <div className="flex flex-col gap-inline">
      <div className="flex items-center gap-inline">
        <button
          type="button"
          data-disco-control="settings.sandbox-save"
          onClick={onSave}
          disabled={!dirty || savePending || testPending}
          className="rounded-control bg-accent px-body py-hair font-ui text-[0.82rem] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          {savePending ? "Saving…" : dirty ? "Save sandbox" : "Saved"}
        </button>
        {/* W-48: explicit connectivity preflight — a real probe of the configured
            endpoint, returning a typed host-naming verdict. Hidden for the Podman stub. */}
        {!stub && (
          <button
            type="button"
            data-disco-control="settings.sandbox-test"
            onClick={onTest}
            disabled={testPending || savePending}
            className="rounded-control border border-hairline px-body py-hair font-ui text-[0.82rem] text-text transition-colors hover:border-hairline-strong disabled:opacity-50"
          >
            {testPending ? "Testing…" : "Test connection"}
          </button>
        )}
        {saveError && (
          <span
            role="alert"
            className="font-ui text-[0.78rem] text-unsupported"
          >
            {saveError.message}
          </span>
        )}
      </div>
      {/* the typed reachability verdict from the preflight probe (W-48) */}
      {probe && (
        <span
          role="status"
          data-sandbox-probe={probe.ok ? "ok" : probe.status}
          className={cn(
            "font-ui text-[0.78rem]",
            probe.ok ? "text-supported" : "text-unsupported",
          )}
        >
          {probe.detail}
        </span>
      )}
    </div>
  );
}
