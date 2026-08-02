/**
 * Settings → Audio overview (RP-09). Controls the two-voice TTS that turns a
 * finished research report into a short podcast-style MP3.
 *
 * The universal THREE-tier provider pattern (same as Search / Extraction), each a
 * single honest, wired choice that persists to the app-server (the agent-server
 * honors it on the NEXT overview — no restart):
 *  - Off: disabled AND the agent-server unloads the Kokoro model to free RAM. The
 *    audio_overview tool fails soft with a "disabled" message.
 *  - Bundled (in-process): Kokoro ONNX/CPU, lazy-loaded on first use (~0.5 GB).
 *    Self-contained — no TTS server. The keyless default.
 *  - Self-hosted (Speaches): an OpenAI-compatible /v1/audio/speech endpoint you run
 *    (Speaches / Kokoro-FastAPI), keyless. Set its base URL.
 *  - Paid API (OpenAI-compatible): a vendor like OpenAI tts-1. Set the base URL, the
 *    stored key name, and the model id.
 *
 * Voices (Host A / Host B) are editable when on; empty falls back to af_heart /
 * af_bella (the Kokoro defaults — set vendor voices, e.g. "alloy", for a paid API).
 */

import { useEffect, useState } from "react";
import { agentIsLive } from "@/api/liveness";
import { useTtsConfig, useUpdateTtsConfig } from "@/hooks/useModels";
import { ContextualFieldsPanel } from "./audioSectionParts/ContextualFieldsPanel";
import { computeAudioFieldsDirty } from "./audioSectionParts/fieldsDirty";
import { type Mode, modeOf, type Provider } from "./audioSectionParts/modeOptions";
import { ModeOptionList } from "./audioSectionParts/ModeOptionList";

export function AudioSection() {
  const { data, isLoading } = useTtsConfig();
  const save = useUpdateTtsConfig();

  // Local draft of the editable fields (controlled inputs), synced from config.
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKeyEnv, setApiKeyEnv] = useState("");
  const [model, setModel] = useState("");
  const [voiceA, setVoiceA] = useState("");
  const [voiceB, setVoiceB] = useState("");
  useEffect(() => {
    if (data) {
      setBaseUrl(data.base_url ?? "");
      setApiKeyEnv(data.api_key_env ?? "");
      setModel(data.model ?? "");
      setVoiceA(data.voice_a ?? "");
      setVoiceB(data.voice_b ?? "");
    }
  }, [data]);

  const active: Mode | null = data ? modeOf(data.enabled, data.provider) : null;

  // The provider chosen when a mode is selected (off keeps the prior provider so
  // re-enabling lands back where it was; bundled/speaches/openai set it).
  const providerFor = (mode: Mode): Provider =>
    mode === "off" ? (data?.provider ?? "bundled") : mode;

  // Switching mode preserves the current field drafts so the user doesn't lose edits.
  const selectMode = (mode: Mode) => {
    if (!data || mode === active) return;
    save.mutate({
      enabled: mode !== "off",
      provider: providerFor(mode),
      base_url: baseUrl,
      api_key_env: apiKeyEnv,
      model,
      voice_a: voiceA,
      voice_b: voiceB,
    });
  };

  const fieldsDirty = computeAudioFieldsDirty(
    data,
    baseUrl,
    apiKeyEnv,
    model,
    voiceA,
    voiceB,
  );

  const saveFields = () => {
    if (!data) return;
    save.mutate({
      enabled: data.enabled,
      provider: data.provider,
      base_url: baseUrl,
      api_key_env: apiKeyEnv,
      model,
      voice_a: voiceA,
      voice_b: voiceB,
    });
  };

  const showUrl = data?.provider === "speaches" || data?.provider === "openai";
  const showPaid = data?.provider === "openai";

  return (
    <section className="flex flex-col gap-inline">
      <header>
        <h3 className="font-ui text-[0.95rem] font-semibold text-text">
          Audio overview
        </h3>
        <p className="mt-hair font-ui text-[0.86rem] text-text-muted">
          The two-voice TTS that turns a finished research report into a short
          podcast-style MP3. Bundled in-process by default — no separate TTS
          server. Turn it off to free its RAM.
        </p>
      </header>

      {isLoading || !data ? (
        <p className="font-ui text-[0.86rem] text-text-faint">Loading…</p>
      ) : (
        <div className="flex flex-col gap-inline">
          <ModeOptionList
            active={active}
            pending={save.isPending}
            onSelect={selectMode}
          />

          <ContextualFieldsPanel
            enabled={data.enabled}
            showUrl={showUrl}
            showPaid={showPaid}
            baseUrl={baseUrl}
            setBaseUrl={setBaseUrl}
            apiKeyEnv={apiKeyEnv}
            setApiKeyEnv={setApiKeyEnv}
            model={model}
            setModel={setModel}
            voiceA={voiceA}
            setVoiceA={setVoiceA}
            voiceB={voiceB}
            setVoiceB={setVoiceB}
            fieldsDirty={fieldsDirty}
            savePending={save.isPending}
            onSave={saveFields}
            agentIsLive={agentIsLive()}
          />
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
