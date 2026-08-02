import { Plus } from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/cn";
import { useCreateProvider, useProviderPresets } from "@/hooks/useModels";
import type { ProviderMutationResult, ProviderPreset } from "@/types/models";
import { errorText, hostLabel } from "./helpers";
import { FIELD } from "./styles";

export function AddProviderForm({
  onCreated,
}: {
  onCreated: (result: ProviderMutationResult) => void;
}) {
  const { data: presets } = useProviderPresets();
  const create = useCreateProvider();
  const [presetId, setPresetId] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [customBaseUrl, setCustomBaseUrl] = useState("");

  const selected: ProviderPreset | undefined =
    presets?.find((p) => p.id === (presetId || presets[0]?.id)) ?? presets?.[0];
  const baseUrl = selected?.requires_base_url
    ? customBaseUrl.trim()
    : (selected?.base_url ?? "");
  const label = selected?.requires_base_url
    ? hostLabel(customBaseUrl.trim()) || "Custom provider"
    : (selected?.label ?? "");
  const canSave = !!selected && !!apiKey.trim() && !!baseUrl.trim();

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (!selected || !canSave) return;
        create.mutate(
          {
            label,
            base_url: baseUrl,
            kind: selected.kind,
            api_key: apiKey.trim(),
          },
          {
            onSuccess: (result) => {
              setApiKey("");
              setCustomBaseUrl("");
              onCreated(result);
            },
          },
        );
      }}
      className="flex flex-col gap-hair rounded-card border border-hairline bg-surface-1/50 p-body"
    >
      <div className="grid gap-inline md:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)_auto]">
        <select
          value={selected?.id ?? ""}
          onChange={(e) => setPresetId(e.target.value)}
          aria-label="Provider preset"
          className={FIELD}
        >
          {(presets ?? []).map((preset) => (
            <option key={preset.id} value={preset.id}>
              {preset.label}
            </option>
          ))}
        </select>
        <input
          type="password"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder="API key"
          aria-label="Provider API key"
          className={FIELD}
        />
        <button
          type="submit"
          data-disco-control="settings.provider-add"
          disabled={!canSave || create.isPending}
          className="flex shrink-0 items-center justify-center gap-hair rounded-control bg-accent px-body py-hair font-ui text-[0.82rem] font-medium text-bg disabled:opacity-60"
        >
          <Plus className="size-3.5" aria-hidden />
          {create.isPending ? "Adding..." : "Add provider"}
        </button>
      </div>
      {selected?.requires_base_url && (
        <input
          value={customBaseUrl}
          onChange={(e) => setCustomBaseUrl(e.target.value)}
          placeholder="https://provider.example/v1"
          aria-label="Custom provider base URL"
          className={cn(FIELD, "font-mono")}
        />
      )}
      {create.error && (
        <p role="alert" className="font-ui text-[0.78rem] text-unsupported">
          {errorText(create.error)}
        </p>
      )}
    </form>
  );
}
