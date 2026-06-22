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
 *    secret/env-var NAME holding the key (never the key itself), and the model id.
 *
 * Voices (Host A / Host B) are editable when on; empty falls back to af_heart /
 * af_bella (the Kokoro defaults — set vendor voices, e.g. "alloy", for a paid API).
 */

import { useEffect, useState } from "react";
import { Check, Cloud, Cpu, Loader2, Server, VolumeX } from "lucide-react";
import { cn } from "@/lib/cn";
import { agentLive } from "@/api/client";
import { testTts } from "@/api/models";
import { useTtsConfig, useUpdateTtsConfig } from "@/hooks/useModels";
import { ProbeButton } from "./ProbeButton";

type Provider = "bundled" | "speaches" | "openai";
type Mode = "off" | Provider;

const OPTIONS: { mode: Mode; Icon: typeof Cpu; label: string; help: string }[] = [
  {
    mode: "off",
    Icon: VolumeX,
    label: "Off",
    help: "Audio overviews are disabled, and the agent-server unloads the voice model to free RAM. The tool fails soft with a clear message if invoked.",
  },
  {
    mode: "bundled",
    Icon: Cpu,
    label: "Bundled (in-process)",
    help: "Kokoro ONNX/CPU, self-contained — no TTS server, no key. Lazy-loaded, so enabled-but-unused costs nothing; loads ~0.5 GB on first use. The default.",
  },
  {
    mode: "speaches",
    Icon: Server,
    label: "Self-hosted endpoint",
    help: "An OpenAI-compatible /v1/audio/speech endpoint you run (Speaches / Kokoro-FastAPI). Keyless. Offloads the model from this host; leave the URL blank to use the server default.",
  },
  {
    mode: "openai",
    Icon: Cloud,
    label: "Paid API (OpenAI-compatible)",
    help: "A vendor like OpenAI tts-1. Set the base URL, the secret/env-var name holding your key (never the key here), and the model id. Use vendor voice names (e.g. alloy).",
  },
];

function modeOf(enabled: boolean, provider: Provider): Mode {
  return enabled ? provider : "off";
}

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

  const fieldsDirty =
    !!data &&
    (baseUrl !== (data.base_url ?? "") ||
      apiKeyEnv !== (data.api_key_env ?? "") ||
      model !== (data.model ?? "") ||
      voiceA !== (data.voice_a ?? "") ||
      voiceB !== (data.voice_b ?? ""));

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

  const fieldClass =
    "rounded-control border border-hairline bg-bg px-inline py-hair font-mono text-[0.78rem] text-text outline-none transition-colors placeholder:text-text-faint focus:border-accent/60";

  return (
    <section className="flex flex-col gap-section border-t border-hairline pt-section">
      <header>
        <h2 className="font-display text-[1.3rem] tracking-tight text-text">Audio overview</h2>
        <p className="mt-hair font-ui text-[0.86rem] text-text-muted">
          The two-voice TTS that turns a finished research report into a short podcast-style MP3.
          Bundled in-process by default — no separate TTS server. Turn it off to free its RAM.
        </p>
      </header>

      {isLoading || !data ? (
        <p className="font-ui text-[0.86rem] text-text-faint">Loading…</p>
      ) : (
        <div className="flex flex-col gap-inline">
          {OPTIONS.map((opt) => {
            const isActive = active === opt.mode;
            return (
              <button
                key={opt.mode}
                type="button"
                data-disco-control="settings.audio-mode"
                data-mode={opt.mode}
                onClick={() => selectMode(opt.mode)}
                disabled={save.isPending}
                aria-pressed={isActive}
                className={cn(
                  "flex items-start gap-inline rounded-card border px-body py-inline text-left transition-colors",
                  isActive
                    ? "border-accent/50 bg-accent/5"
                    : "border-hairline hover:border-hairline-strong",
                  save.isPending && "opacity-60",
                )}
              >
                <opt.Icon
                  className={cn(
                    "mt-px size-4 shrink-0",
                    isActive ? "text-accent" : "text-text-faint",
                  )}
                  aria-hidden
                />
                <span className="flex min-w-0 flex-col gap-hair">
                  <span className="flex items-center gap-hair font-ui text-[0.9rem] font-medium text-text">
                    {opt.label}
                    {isActive && save.isPending && (
                      <Loader2 className="size-3 animate-spin text-accent" aria-hidden />
                    )}
                  </span>
                  <span className="font-ui text-[0.8rem] leading-relaxed text-text-faint">
                    {opt.help}
                  </span>
                </span>
              </button>
            );
          })}

          {/* Contextual fields — voices (any on-mode) + endpoint (self-host/paid) + key+model (paid). */}
          {data.enabled && (
            <div className="mt-hair flex flex-col gap-inline rounded-card border border-hairline bg-surface-1/40 px-body py-inline">
              {showUrl && (
                <label className="flex flex-col gap-hair">
                  <span className="flex items-baseline gap-hair font-ui text-[0.8rem] text-text">
                    Endpoint base URL
                    <span className="font-ui text-[0.72rem] text-text-faint">
                      · OpenAI-shape /v1/audio/speech
                    </span>
                  </span>
                  <input
                    type="url"
                    inputMode="url"
                    spellCheck={false}
                    value={baseUrl}
                    onChange={(e) => setBaseUrl(e.target.value)}
                    placeholder={
                      showPaid
                        ? "https://api.openai.com  (empty = OpenAI default)"
                        : "http://host:port  (empty = server default)"
                    }
                    className={fieldClass}
                  />
                </label>
              )}
              {showPaid && (
                <div className="grid grid-cols-2 gap-inline">
                  <label className="flex flex-col gap-hair">
                    <span className="flex items-baseline gap-hair font-ui text-[0.8rem] text-text">
                      API key env var
                      <span className="font-ui text-[0.72rem] text-text-faint">· name, not the key</span>
                    </span>
                    <input
                      spellCheck={false}
                      value={apiKeyEnv}
                      onChange={(e) => setApiKeyEnv(e.target.value)}
                      placeholder="OPENAI_API_KEY"
                      className={fieldClass}
                    />
                  </label>
                  <label className="flex flex-col gap-hair">
                    <span className="font-ui text-[0.8rem] text-text">Model</span>
                    <input
                      spellCheck={false}
                      value={model}
                      onChange={(e) => setModel(e.target.value)}
                      placeholder="tts-1"
                      className={fieldClass}
                    />
                  </label>
                </div>
              )}
              <div className="grid grid-cols-2 gap-inline">
                <label className="flex flex-col gap-hair">
                  <span className="flex items-baseline gap-hair font-ui text-[0.8rem] text-text">
                    Host A voice
                    <span className="font-ui text-[0.72rem] text-text-faint">
                      · single-speaker default
                    </span>
                  </span>
                  <input
                    spellCheck={false}
                    value={voiceA}
                    onChange={(e) => setVoiceA(e.target.value)}
                    placeholder="af_heart"
                    className={fieldClass}
                  />
                </label>
                <label className="flex flex-col gap-hair">
                  <span className="font-ui text-[0.8rem] text-text">Host B voice</span>
                  <input
                    spellCheck={false}
                    value={voiceB}
                    onChange={(e) => setVoiceB(e.target.value)}
                    placeholder="af_bella"
                    className={fieldClass}
                  />
                </label>
              </div>
              <div className="flex items-center gap-inline">
                <button
                  type="button"
                  data-disco-control="settings.audio-save"
                  disabled={!fieldsDirty || save.isPending}
                  onClick={saveFields}
                  className={cn(
                    "flex items-center gap-hair self-start rounded-control border px-inline py-hair font-ui text-[0.8rem] transition-colors",
                    fieldsDirty && !save.isPending
                      ? "border-accent/50 bg-accent/10 text-text hover:bg-accent/20"
                      : "border-hairline text-text-faint",
                  )}
                >
                  {save.isPending ? (
                    <Loader2 className="size-3 animate-spin" aria-hidden />
                  ) : (
                    <Check className="size-3" aria-hidden />
                  )}
                  Save
                </button>
                {!fieldsDirty && !save.isPending && (
                  <span className="font-ui text-[0.76rem] text-text-faint">Saved</span>
                )}
              </div>
              {/* T4.3 live probe: synthesize the single word "Disco" via the
                  SAVED TTS tier. Non-empty audio = ok; a disabled/unreachable/
                  bad-key tier fails honestly. Disabled until field edits are saved
                  and the agent-server is connected. */}
              <ProbeButton
                control="settings.audio-test-tts"
                idleLabel="Test TTS"
                run={testTts}
                disabled={!agentLive() || fieldsDirty}
                disabledHint={fieldsDirty ? "save changes to test" : "connect the agent server to test"}
              />
            </div>
          )}
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
