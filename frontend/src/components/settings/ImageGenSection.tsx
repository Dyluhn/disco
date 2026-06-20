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
import {
  useImageGenConfig,
  useOpenRouterModels,
  useUpdateImageGenConfig,
} from "@/hooks/useModels";

/** Honest price label for an OpenRouter IMAGE model, from real OpenRouter data:
 *  - image_price_per_m (the /endpoints `image_output` rate ×1e6) is the actual
 *    image-generation cost → "$X /M img-tok". Shown first for every image model,
 *    incl. the dedicated generators (FLUX/Recraft/Seedream) the catalogue zeroes out.
 *  - else the text token price (some dual chat+image models) → "$in/$out /Mtok".
 *  - else (genuinely no price reported) "pricing on openrouter.ai" — never "Free",
 *    which would be a false tag on a paid model. (Per-IMAGE $ isn't shown: OpenRouter
 *    bills per image-token and tokens-per-image varies, so a per-image figure would be
 *    an invented number.) */
const orPrice = (m: {
  price_in_per_m: number;
  price_out_per_m: number;
  image_price_per_m?: number;
}): string => {
  if (m.image_price_per_m && m.image_price_per_m > 0)
    return `$${m.image_price_per_m.toFixed(2)} /M img-tok`;
  if (m.price_in_per_m > 0 || m.price_out_per_m > 0)
    return `$${m.price_in_per_m.toFixed(2)} in / $${m.price_out_per_m.toFixed(2)} out /Mtok`;
  return "pricing on openrouter.ai";
};

type Provider = "procedural" | "comfyui" | "openai" | "openrouter";

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
    help: "Your ComfyUI graph API. Keyless, but the base URL is REQUIRED (without it, image-gen falls back to procedural). Ships a built-in SDXL/SD workflow — set Checkpoint to a model file that exists on your ComfyUI. For FLUX / SD3 / custom graphs, paste a 'Save (API Format)' workflow below.",
  },
  {
    provider: "openai",
    Icon: Cloud,
    label: "Paid API (OpenAI-compatible)",
    help: "A vendor like OpenAI gpt-image-1 / DALL·E via /v1/images/generations (or ImageRouter's full /v1/openai/... URL). Set the base URL and the secret/env-var name holding your key (never the key here).",
  },
  {
    provider: "openrouter",
    Icon: Cloud,
    label: "OpenRouter (image models)",
    help: "Image models via OpenRouter chat-completions (modalities:[image,text]) using your existing OpenRouter key from Provider API keys. PAID — every OpenRouter image model costs credits (no free tier). Set Model to an image model id; cheapest is google/gemini-2.5-flash-image.",
  },
];

export function ImageGenSection() {
  const { data, isLoading } = useImageGenConfig();
  const save = useUpdateImageGenConfig();
  // Live OpenRouter catalogue, fetched only while the OpenRouter tier is active —
  // filtered to image-OUTPUT models so the model field becomes a priced picker.
  const orModels = useOpenRouterModels(data?.provider === "openrouter", { allModalities: true });
  const imageModels = (orModels.data ?? []).filter((m) => m.image_output);

  const [baseUrl, setBaseUrl] = useState("");
  const [apiKeyEnv, setApiKeyEnv] = useState("");
  const [model, setModel] = useState("");
  const [workflowJson, setWorkflowJson] = useState("");
  useEffect(() => {
    if (data) {
      setBaseUrl(data.base_url ?? "");
      setApiKeyEnv(data.api_key_env ?? "");
      setModel(data.model ?? "");
      setWorkflowJson(data.workflow_json ?? "");
    }
  }, [data]);

  const active = data?.provider ?? null;

  // Switching provider preserves the current field drafts so edits aren't lost.
  const selectProvider = (provider: Provider) => {
    if (!data || provider === active) return;
    save.mutate({
      provider,
      base_url: baseUrl,
      api_key_env: apiKeyEnv,
      model,
      workflow_json: workflowJson,
    });
  };

  const fieldsDirty =
    !!data &&
    (baseUrl !== (data.base_url ?? "") ||
      apiKeyEnv !== (data.api_key_env ?? "") ||
      model !== (data.model ?? "") ||
      workflowJson !== (data.workflow_json ?? ""));

  const saveFields = () => {
    if (!data) return;
    save.mutate({
      provider: data.provider,
      base_url: baseUrl,
      api_key_env: apiKeyEnv,
      model,
      workflow_json: workflowJson,
    });
  };

  const showUrl = data?.provider === "comfyui" || data?.provider === "openai";
  const showPaid = data?.provider === "openai";
  const showComfy = data?.provider === "comfyui";
  const showOpenRouter = data?.provider === "openrouter";
  // The contextual fields panel renders for any remote tier (comfyui/openai/openrouter).
  const showFields = showUrl || showOpenRouter;

  // A local "is the pasted workflow even valid JSON?" check so the user gets an
  // honest hint BEFORE the agent's next image-gen fails server-side. Empty = use
  // the built-in default (not an error). Tokens are substituted at gen time, so a
  // template with %seed% etc. won't parse here — only flag clearly-broken braces.
  const workflowJsonError = (() => {
    const t = workflowJson.trim();
    if (!t) return null;
    // Numeric tokens are substituted as BARE numbers, so they must NOT sit inside
    // quotes — `"seed": "%seed%"` would send `"123"` (a string) and ComfyUI rejects it.
    // Catch that here since the parse-probe below would otherwise mask it.
    if (/"\s*%(seed|width|height)%\s*"/.test(t))
      return "Numeric tokens (%seed% %width% %height%) must be UNQUOTED, e.g. \"seed\": %seed% — not \"%seed%\".";
    // Strip the substitution tokens to a parseable stand-in before validating shape.
    const probe = t
      .replace(/%seed%|%width%|%height%/g, "0")
      .replace(/%prompt%|%negative%|%ckpt%/g, "x");
    try {
      const parsed = JSON.parse(probe);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed))
        return "Workflow must be a JSON object of nodes (ComfyUI 'Save (API Format)').";
      return null;
    } catch {
      return "Not valid JSON yet — paste a ComfyUI 'Save (API Format)' export.";
    }
  })();

  // Honesty: select_image_backend() silently falls back to procedural when a remote tier
  // is selected without its required config (comfyui w/o base_url, openai w/o a stored key).
  // Warn so the UI never claims a remote provider is active when procedural is what runs.
  const fallbackWarning =
    data?.provider === "comfyui" && !(data.base_url ?? "").trim()
      ? "Set a base URL below — until then, image generation silently falls back to procedural."
      : data?.provider === "openai" && !(data.api_key_env ?? "").trim()
        ? "Set the API key env var below and store that key in Provider API keys — until then, image generation falls back to procedural."
        : null;

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

          {fallbackWarning && (
            <p className="font-ui text-[0.8rem] text-warn" role="status">
              ⚠ {fallbackWarning}
            </p>
          )}

          {/* Contextual fields — endpoint (self-host/paid) + model + key env (paid). */}
          {showFields && (
            <div className="mt-hair flex flex-col gap-inline rounded-card border border-hairline bg-surface-1/40 px-body py-inline">
              {showOpenRouter && (
                <p className="font-ui text-[0.78rem] leading-relaxed text-text-faint">
                  Uses your <strong className="text-text">OpenRouter key</strong> from{" "}
                  <em>Provider API keys</em> — store it there if you haven't, or image
                  generation falls back to the procedural placeholder. Every OpenRouter image
                  model is <strong className="text-text">paid</strong>; add credits at
                  openrouter.ai/settings/credits.
                </p>
              )}
              {showUrl && (
                <label className="flex flex-col gap-hair">
                  <span className="flex items-baseline gap-hair font-ui text-[0.8rem] text-text">
                    Endpoint base URL
                    <span className="font-ui text-[0.72rem] text-text-faint">
                      {showPaid
                        ? "· origin (we append /v1/images/generations) OR a full endpoint URL"
                        : "· ComfyUI host"}
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
                        ? "https://api.openai.com  — or ImageRouter's full URL https://api.imagerouter.io/v1/openai/images/generations"
                        : "http://host:8188  (required for ComfyUI)"
                    }
                    className={fieldClass}
                  />
                </label>
              )}
              <label className="flex flex-col gap-hair">
                <span className="flex items-baseline gap-hair font-ui text-[0.8rem] text-text">
                  {showComfy ? "Checkpoint" : "Model"}
                  <span className="font-ui text-[0.72rem] text-text-faint">
                    {showComfy
                      ? "· required; must exist on your ComfyUI"
                      : showOpenRouter
                        ? "· $/M image-tokens (the real image-gen cost, from OpenRouter)"
                        : "· empty = provider default"}
                  </span>
                </span>
                {showOpenRouter && imageModels.length > 0 ? (
                  // Priced picker — filtered to OpenRouter image-output models, like the
                  // LLM model browser. Falls back to the text input below if the live
                  // catalogue is unavailable (offline / 502), so config still works.
                  <select
                    value={model}
                    onChange={(e) => setModel(e.target.value)}
                    className={fieldClass}
                  >
                    <option value="">Select an image model…</option>
                    {/* Keep a currently-saved id selectable even if it's not in the live
                        catalogue (a custom/renamed/removed model) — never silently hide it. */}
                    {model && !imageModels.some((m) => m.id === model) && (
                      <option value={model}>{model} · (current)</option>
                    )}
                    {imageModels.map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.id} · {orPrice(m)}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    spellCheck={false}
                    value={model}
                    onChange={(e) => setModel(e.target.value)}
                    placeholder={
                      showComfy
                        ? "sd_xl_base_1.0.safetensors"
                        : showOpenRouter
                          ? "google/gemini-2.5-flash-image"
                          : "gpt-image-1"
                    }
                    className={fieldClass}
                  />
                )}
                {showOpenRouter && orModels.isLoading && (
                  <span className="font-ui text-[0.72rem] text-text-faint">
                    Loading the OpenRouter image catalogue…
                  </span>
                )}
                {showOpenRouter && !orModels.isLoading && imageModels.length === 0 && (
                  <span className="font-ui text-[0.72rem] text-text-faint">
                    Couldn't load the live catalogue — type an image model id (e.g.
                    google/gemini-2.5-flash-image).
                  </span>
                )}
              </label>
              {showComfy && (
                <label className="flex flex-col gap-hair">
                  <span className="flex items-baseline gap-hair font-ui text-[0.8rem] text-text">
                    Custom workflow
                    <span className="font-ui text-[0.72rem] text-text-faint">
                      · optional — empty = built-in SDXL graph
                    </span>
                  </span>
                  <textarea
                    spellCheck={false}
                    rows={6}
                    value={workflowJson}
                    onChange={(e) => setWorkflowJson(e.target.value)}
                    placeholder={
                      '{ "1": { "class_type": "CheckpointLoaderSimple", "inputs": { "ckpt_name": "%ckpt%" } }, ... }\n' +
                      "Paste a ComfyUI 'Save (API Format)' export. Tokens: %prompt% %negative% %seed% %width% %height% %ckpt%"
                    }
                    className={cn(fieldClass, "resize-y leading-snug")}
                  />
                  <span className="font-ui text-[0.72rem] text-text-faint">
                    Overrides the default graph so FLUX / SD3 / custom shapes work. Tokens are
                    substituted per generation: string tokens{" "}
                    <code className="font-mono">%prompt%</code> <code className="font-mono">%negative%</code>{" "}
                    <code className="font-mono">%ckpt%</code> go inside quotes; numeric tokens{" "}
                    <code className="font-mono">%seed%</code> <code className="font-mono">%width%</code>{" "}
                    <code className="font-mono">%height%</code> go UNQUOTED (e.g.{" "}
                    <code className="font-mono">"seed": %seed%</code>).
                  </span>
                  {workflowJsonError && (
                    <span className="font-ui text-[0.76rem] text-warn" role="status">
                      ⚠ {workflowJsonError}
                    </span>
                  )}
                </label>
              )}
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
