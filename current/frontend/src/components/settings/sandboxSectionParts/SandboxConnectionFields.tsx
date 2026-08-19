/**
 * W-49 progressive disclosure: ONE varying field up front, the rest (runtime is
 * dropped from the form; image/workspace_root + any non-primary connection field)
 * folded under Advanced with their saved defaults. Pulled out of SandboxSection
 * purely to shed cyclomatic complexity (TS-0035); the parent still owns the draft
 * state and passes field changes back up.
 */

import {
  fieldLabel,
  type BackendMeta,
  type SandboxConfig,
  type SandboxField as SandboxFieldId,
} from "@/types/sandbox";
import { SandboxField } from "./SandboxField";

export function SandboxConnectionFields({
  meta,
  draft,
  onFieldChange,
}: {
  meta: BackendMeta | undefined;
  draft: SandboxConfig;
  onFieldChange: (field: SandboxFieldId, value: string) => void;
}) {
  if (!meta) return null;

  const advanced = meta.fields.filter(
    (f) => f !== "runtime" && f !== meta.primaryField,
  );

  return (
    <div className="flex flex-col gap-inline">
      {/* the "what you provide" subline — sets expectations before any input */}
      <p className="font-ui text-[0.78rem] text-text-faint">
        {meta.provides}
      </p>

      {/* the ONE connection field that varies per backend (null → none shown) */}
      {meta.primaryField && (
        <div className="flex flex-col gap-hair">
          <SandboxField
            field={meta.primaryField}
            label={meta.primaryLabel ?? fieldLabel(meta.primaryField)}
            value={draft[meta.primaryField]}
            disabled={meta.stub}
            onChange={(v) => onFieldChange(meta.primaryField!, v)}
          />
          {/* W-48(b): inline "use the tailnet IP, not the LAN IP" guidance */}
          {meta.primaryHint && (
            <p
              data-sandbox-hint={meta.primaryField}
              className="font-ui text-[0.72rem] text-text-faint"
            >
              {meta.primaryHint}
            </p>
          )}
        </div>
      )}

      {/* Advanced — everything else, collapsed by default. runtime is NOT in the form
          (it rides along in the saved payload via the per-backend default). */}
      {advanced.length > 0 && (
        <details className="rounded-control border border-hairline px-body py-inline">
          <summary
            data-disco-control="settings.sandbox-advanced"
            className="cursor-pointer py-3 font-ui text-[0.78rem] text-text-muted lg:py-0"
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
                onChange={(v) => onFieldChange(f, v)}
              />
            ))}
          </div>
        </details>
      )}

      {meta.stub && (
        <p
          data-disco-flag="sandbox-stub"
          className="font-ui text-[0.76rem] text-weak"
        >
          Podman is a stub in this environment — it configures but doesn’t
          run here; completed at deployment.
        </p>
      )}
    </div>
  );
}
