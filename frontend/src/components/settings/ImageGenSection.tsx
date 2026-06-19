/**
 * Settings → Image generation. Controls the provider the `image_generate` tool uses
 * when an agent or build task produces an image (slide art, website backgrounds, etc.).
 *
 * The universal THREE-tier provider pattern (same as Audio / Search / Extraction), each
 * a single honest, wired choice that persists to the app-server (the agent-server honors
 * it on the NEXT image-gen call — no restart). There is no "off": image-gen always works
 * because `procedural` is a keyless in-process fallback.
 *  - Bundled (procedural): in-process Pillow procedural patterns. Keyless, no network, no
 *    model download. Deterministic per (prompt, seed). The first-run default.
 *  - Self-hosted (ComfyUI): your ComfyUI graph API (`base_url`; empty → default), keyless.
 *    Posts a workflow to /prompt and polls /history.
 *  - Paid API (OpenAI-compatible): a vendor like OpenAI gpt-image-1 / DALL·E via
 *    /v1/images/generations. Set the base URL and the secret/env-var NAME holding the key
 *    (never the key itself).
 */

import { useEffect, useState } from "react";
import { Check, Cloud, Cpu, Loader2, Server } from "lucide-react";
import { cn } from "@/lib/cn";
import { useImageGenConfig, useUpdateImageGenConfig } from "@/hooks/useModels";

type Provider = "procedural" | "comfyui" | "openai";

const OPTIONS: { provider: Provider; Icon: typeof Cpu; label: string; help: string }[] = [
  {
    provider: "procedural",
    Icon: Cpu,
    label: "Bundled (procedural)",
    help: "In-process Pillow procedural patterns. Keyless, no network, no model download — image-gen works out of the box. Deterministic per (prompt, seed). The default.",
  },
  {
    provider: "comfyui",
    Icon: Server,
    label: "Self-hosted (ComfyUI)",
    help: "Your ComfyUI graph API. Keyless; set its base URL (empty → server default). Posts a workflow to /prompt and polls /history for the result.",
  },
  {
    provider: "openai",
    Icon: Cloud,
    label: "Paid API (OpenAI-compatible)",
    help: "A vendor like OpenAI gpt-image-1 / DALL·E via /v1/images/generations. Set the base URL and the secret/env-var name holding your key (never the key here).",
  },
];

export function ImageGenSection() {
  const { data, isLoading } = useImageGenConfig();
  const save = useUpdateImageGenConfig();

  const [baseUrl, setBaseUrl] = useState("");
  const [apiKeyEnv, setApiKeyEnv] = useState("");
  useEffect(() => {
    if (data) {
      setBaseUrl(data.base_url ?? "");
      setApiKeyEnv(data.api_key_env ?? "");
    }
  }, [data]);

  const active = data?.provider ?? null;

  // Switching provider preserves the current field drafts so edits aren't lost.
  const selectProvider = (provider: Provider) => {
    if (!data || provider === active) return;
    save.mutate({ provider, base_url: baseUrl, api_key_env: apiKeyEnv });
  };

  const fieldsDirty =
    !!data &&
    (baseUrl !== (data.base_url ?? "") || apiKeyEnv !== (data.api_key_env ?? ""));

  const saveFields = () => {
    if (!data) return;
    save.mutate({ provider: data.provider, base_url: baseUrl, api_key_env: apiKeyEnv });
  };

  const showUrl = data?.provider === "comfyui" || data?.provider === "openai";
  const showPaid = data?.provider === "openai";

  const fieldClass =
    "rounded-control border border-hairline bg-bg px-inline py-hair font-mono text-[0.78rem] text-text outline-none transition-colors placeholder:text-text-faint focus:border-accent/60";

  return (
    <section className="flex flex-col gap-section border-t border-hairline pt-section">
      <header>
        <h2 className="font-display text-[1.3rem] tracking-tight text-text">Image generation</h2>
        <p className="mt-hair font-ui text-[0.86rem] text-text-muted">
          The provider the <code className="font-mono text-[0.8rem]">image_generate</code> tool uses
          for slide art, website backgrounds, and other generated images. Bundled procedural by
          default — keyless and offline; point it at ComfyUI or a paid API for real diffusion.
        </p>
      </header>

      {isLoading || !data ? (
        <p className="font-ui text-[0.86rem] text-text-faint">Loading…</p>
      ) : (
        <div className="flex flex-col gap-inline">
          {OPTIONS.map((opt) => {
            const isActive = active === opt.provider;
            return (
              <button
                key={opt.provider}
                type="button"
                onClick={() => selectProvider(opt.provider)}
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
                  className={cn("mt-px size-4 shrink-0", isActive ? "text-accent" : "text-text-faint")}
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

          {/* Contextual fields — endpoint (self-host/paid) + key env (paid only). */}
          {showUrl && (
            <div className="mt-hair flex flex-col gap-inline rounded-card border border-hairline bg-surface-1/40 px-body py-inline">
              <label className="flex flex-col gap-hair">
                <span className="flex items-baseline gap-hair font-ui text-[0.8rem] text-text">
                  Endpoint base URL
                  <span className="font-ui text-[0.72rem] text-text-faint">
                    {showPaid ? "· /v1/images/generations" : "· ComfyUI host"}
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
                      ? "https://api.openai.com/v1  (empty = OpenAI default)"
                      : "http://host:8188  (empty = server default)"
                  }
                  className={fieldClass}
                />
              </label>
              {showPaid && (
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
              )}
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
