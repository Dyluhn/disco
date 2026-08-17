import { Plus } from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/cn";
import { useCreateProvider, useProviderPresets } from "@/hooks/useModels";
import type { ProviderMutationResult, ProviderPreset } from "@/types/models";
import { errorText, hostLabel } from "./helpers";
import { FIELD } from "./styles";

function formValues(
  presets: ProviderPreset[],
  presetId: string,
  customBaseUrl: string,
  apiKey: string,
) {
  const selectedId = presetId || presets[0]?.id;
  const selected = presets.find((preset) => preset.id === selectedId) ?? presets[0];
  if (!selected) {
    return { selected: undefined, baseUrl: "", label: "", requiresKey: true, canSave: false };
  }
  const custom = customBaseUrl.trim();
  const baseUrl = selected.requires_base_url ? custom || selected.base_url : selected.base_url;
  const customLabel = hostLabel(custom) || "Custom provider";
  const label = selected.id === "ollama"
    ? selected.label
    : selected.requires_base_url
      ? customLabel
      : selected.label;
  const requiresKey = selected.requires_api_key !== false;
  const canSave = !!baseUrl.trim() && (!requiresKey || !!apiKey.trim());
  return { selected, baseUrl, label, requiresKey, canSave };
}

function OllamaHint({ selected }: { selected: ProviderPreset | undefined }) {
  if (selected?.id !== "ollama") return null;
  return (
    <p className="font-ui text-[0.76rem] leading-snug text-text-faint">
      No API key is needed. Docker Compose uses
      <span className="font-mono"> host.docker.internal:11434/v1</span>;
      native installs can use
      <span className="font-mono"> localhost:11434/v1</span>.
    </p>
  );
}

function ProviderFields({
  presets,
  selected,
  requiresKey,
  canSave,
  pending,
  apiKey,
  customBaseUrl,
  onPresetChange,
  onApiKeyChange,
  onBaseUrlChange,
}: {
  presets: ProviderPreset[];
  selected: ProviderPreset | undefined;
  requiresKey: boolean;
  canSave: boolean;
  pending: boolean;
  apiKey: string;
  customBaseUrl: string;
  onPresetChange: (id: string, preset: ProviderPreset | undefined) => void;
  onApiKeyChange: (value: string) => void;
  onBaseUrlChange: (value: string) => void;
}) {
  return (
    <>
      <div
        className={cn(
          "grid gap-inline",
          requiresKey
            ? "md:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)_auto]"
            : "md:grid-cols-[minmax(0,1fr)_auto]",
        )}
      >
        <select
          value={selected?.id ?? ""}
          onChange={(event) => {
            const id = event.target.value;
            onPresetChange(id, presets.find((preset) => preset.id === id));
          }}
          aria-label="Provider preset"
          className={FIELD}
        >
          {presets.map((preset) => (
            <option key={preset.id} value={preset.id}>
              {preset.label}
            </option>
          ))}
        </select>
        {requiresKey && (
          <input
            type="password"
            value={apiKey}
            onChange={(event) => onApiKeyChange(event.target.value)}
            placeholder="API key"
            aria-label="Provider API key"
            className={FIELD}
          />
        )}
        <button
          type="submit"
          data-disco-control="settings.provider-add"
          disabled={!canSave || pending}
          className="flex shrink-0 items-center justify-center gap-hair rounded-control bg-accent px-body py-hair font-ui text-[0.82rem] font-medium text-bg disabled:opacity-60"
        >
          <Plus className="size-3.5" aria-hidden />
          {pending ? "Adding..." : "Add provider"}
        </button>
      </div>
      {selected?.requires_base_url && (
        <input
          value={customBaseUrl}
          onChange={(event) => onBaseUrlChange(event.target.value)}
          placeholder={selected.base_url || "https://provider.example/v1"}
          aria-label="Custom provider base URL"
          className={cn(FIELD, "font-mono")}
        />
      )}
      <OllamaHint selected={selected} />
    </>
  );
}

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

  // OpenRouter has a first-class key + catalogue flow directly below this form.
  // Leaving its generic preset here created two legitimate-looking ways to add
  // the same provider, backed by different secret storage.
  const availablePresets = (presets ?? []).filter(
    (preset) => preset.id !== "openrouter",
  );
  const { selected, baseUrl, label, requiresKey, canSave } = formValues(
    availablePresets,
    presetId,
    customBaseUrl,
    apiKey,
  );

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
            requires_api_key: requiresKey,
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
      <ProviderFields
        presets={availablePresets}
        selected={selected}
        requiresKey={requiresKey}
        canSave={canSave}
        pending={create.isPending}
        apiKey={apiKey}
        customBaseUrl={customBaseUrl}
        onPresetChange={(id, preset) => {
          setPresetId(id);
          setCustomBaseUrl(preset?.requires_base_url ? preset.base_url : "");
        }}
        onApiKeyChange={setApiKey}
        onBaseUrlChange={setCustomBaseUrl}
      />
      {create.error && (
        <p role="alert" className="font-ui text-[0.78rem] text-unsupported">
          {errorText(create.error)}
        </p>
      )}
    </form>
  );
}
