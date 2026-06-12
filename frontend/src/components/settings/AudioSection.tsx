/**
 * Settings → Audio overview (RP-09). Controls the two-voice TTS that turns a
 * finished research report into a short podcast-style MP3.
 *
 * Three honest, wired options — each sets the (enabled, remote) pair:
 *  - Off: the feature is disabled AND the agent-server unloads the Kokoro model
 *    to free RAM. The audio_overview tool fails soft with a "disabled" message.
 *  - Bundled (in-process): Kokoro ONNX/CPU, lazy-loaded on first use (~0.5 GB).
 *    Self-contained — no TTS server. The default.
 *  - Remote (Speaches): an external Speaches /v1/audio/speech endpoint.
 *
 * Voices (Host A / Host B) are editable when the feature is on; an empty field
 * falls back to the ratified af_heart / af_bella. The toggle persists to the
 * app-server and the agent-server honors it on the next overview (no restart).
 */

import { useEffect, useState } from "react";
import { Check, Cpu, Loader2, Server, VolumeX } from "lucide-react";
import { cn } from "@/lib/cn";
import { useTtsConfig, useUpdateTtsConfig } from "@/hooks/useModels";

type Mode = "off" | "bundled" | "remote";

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
    help: "Kokoro ONNX/CPU, self-contained — no TTS server. Lazy-loaded, so enabled-but-unused costs nothing; loads ~0.5 GB on first use.",
  },
  {
    mode: "remote",
    Icon: Server,
    label: "Remote (Speaches)",
    help: "An external Speaches /v1/audio/speech endpoint. Offloads the model from this host; leave the URL blank to use the server's default.",
  },
];

function modeOf(enabled: boolean, remote: boolean): Mode {
  if (!enabled) return "off";
  return remote ? "remote" : "bundled";
}

export function AudioSection() {
  const { data, isLoading } = useTtsConfig();
  const save = useUpdateTtsConfig();

  // Local draft of the editable fields (controlled inputs), synced from config.
  const [url, setUrl] = useState("");
  const [voiceA, setVoiceA] = useState("");
  const [voiceB, setVoiceB] = useState("");
  useEffect(() => {
    if (data) {
      setUrl(data.speaches_url ?? "");
      setVoiceA(data.voice_a ?? "");
      setVoiceB(data.voice_b ?? "");
    }
  }, [data]);

  const active: Mode | null = data ? modeOf(data.enabled, data.remote) : null;

  // Switching mode preserves the current voice/url draft so the user doesn't lose edits.
  const selectMode = (mode: Mode) => {
    if (!data || mode === active) return;
    save.mutate({
      enabled: mode !== "off",
      remote: mode === "remote",
      speaches_url: url,
      voice_a: voiceA,
      voice_b: voiceB,
    });
  };

  const fieldsDirty =
    !!data &&
    (url !== (data.speaches_url ?? "") ||
      voiceA !== (data.voice_a ?? "") ||
      voiceB !== (data.voice_b ?? ""));

  const saveFields = () => {
    if (!data) return;
    save.mutate({
      enabled: data.enabled,
      remote: data.remote,
      speaches_url: url,
      voice_a: voiceA,
      voice_b: voiceB,
    });
  };

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

          {/* Contextual fields — voices (any on-mode) + endpoint (remote only). */}
          {data.enabled && (
            <div className="mt-hair flex flex-col gap-inline rounded-card border border-hairline bg-surface-1/40 px-body py-inline">
              {data.remote && (
                <label className="flex flex-col gap-hair">
                  <span className="flex items-baseline gap-hair font-ui text-[0.8rem] text-text">
                    Speaches endpoint
                    <span className="font-ui text-[0.72rem] text-text-faint">
                      · OpenAI-shape /v1/audio/speech
                    </span>
                  </span>
                  <input
                    type="url"
                    inputMode="url"
                    spellCheck={false}
                    value={url}
                    onChange={(e) => setUrl(e.target.value)}
                    placeholder="http://host:port  (empty = server default)"
                    className="rounded-control border border-hairline bg-bg px-inline py-hair font-mono text-[0.78rem] text-text outline-none transition-colors placeholder:text-text-faint focus:border-accent/60"
                  />
                </label>
              )}
              <div className="grid grid-cols-2 gap-inline">
                <label className="flex flex-col gap-hair">
                  <span className="font-ui text-[0.8rem] text-text">Host A voice</span>
                  <input
                    spellCheck={false}
                    value={voiceA}
                    onChange={(e) => setVoiceA(e.target.value)}
                    placeholder="af_heart"
                    className="rounded-control border border-hairline bg-bg px-inline py-hair font-mono text-[0.78rem] text-text outline-none transition-colors placeholder:text-text-faint focus:border-accent/60"
                  />
                </label>
                <label className="flex flex-col gap-hair">
                  <span className="font-ui text-[0.8rem] text-text">Host B voice</span>
                  <input
                    spellCheck={false}
                    value={voiceB}
                    onChange={(e) => setVoiceB(e.target.value)}
                    placeholder="af_bella"
                    className="rounded-control border border-hairline bg-bg px-inline py-hair font-mono text-[0.78rem] text-text outline-none transition-colors placeholder:text-text-faint focus:border-accent/60"
                  />
                </label>
              </div>
              <div className="flex items-center gap-inline">
                <button
                  type="button"
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
