/**
 * Settings → Encoders. Where the non-generative models — embeddings, reranking,
 * and citation-entailment (NLI) — run. These are NOT LLM-router roles (they don't
 * follow the model assignments), so they live here, not in the model matrix.
 *
 * Two honest, wired options:
 *  - Bundled (local): in-process ONNX/CPU (fastembed) — self-contained, no encoder
 *    server, faster than a network hop. The default.
 *  - Remote: the external LAN endpoints (TEI rerank / OpenAI-shape embeddings /
 *    NLI sidecar), configured via the PMX_*_URL env on the agent-server.
 *
 * The toggle persists to the app-server and the agent-server honors it on the next
 * research run (no restart) — a real control, not a label.
 */

import { useEffect, useState } from "react";
import { Check, Cpu, Loader2, Server } from "lucide-react";
import { cn } from "@/lib/cn";
import { useEncodersConfig, useUpdateEncodersConfig } from "@/hooks/useModels";

const ENDPOINTS = [
  { key: "embedder_url", label: "Embeddings endpoint", hint: "OpenAI-shape /v1" },
  { key: "reranker_url", label: "Reranker endpoint", hint: "TEI rerank" },
  { key: "nli_url", label: "NLI endpoint", hint: "entailment sidecar" },
] as const;

const OPTIONS = [
  {
    remote: false,
    Icon: Cpu,
    label: "Bundled (local)",
    help: "In-process ONNX/CPU (multilingual-e5-large + jina-reranker-v2). Self-contained — no encoder server, faster than a network hop. Loads ~1–2 GB on first use.",
  },
  {
    remote: true,
    Icon: Server,
    label: "Remote endpoints",
    help: "External LAN services (TEI rerank / embeddings / NLI sidecar), configured via the PMX_*_URL env on the agent-server.",
  },
] as const;

export function EncoderSection() {
  const { data, isLoading } = useEncodersConfig();
  const save = useUpdateEncodersConfig();

  // Local draft of the three endpoint URLs (controlled inputs), synced from the
  // persisted config. Saved as a unit alongside remote=true.
  const [urls, setUrls] = useState({ embedder_url: "", reranker_url: "", nli_url: "" });
  useEffect(() => {
    if (data)
      setUrls({
        embedder_url: data.embedder_url ?? "",
        reranker_url: data.reranker_url ?? "",
        nli_url: data.nli_url ?? "",
      });
  }, [data]);

  const dirty =
    !!data &&
    (urls.embedder_url !== (data.embedder_url ?? "") ||
      urls.reranker_url !== (data.reranker_url ?? "") ||
      urls.nli_url !== (data.nli_url ?? ""));

  return (
    <section className="flex flex-col gap-section border-t border-hairline pt-section">
      <header>
        <h2 className="font-display text-[1.3rem] tracking-tight text-text">Encoders</h2>
        <p className="mt-hair font-ui text-[0.86rem] text-text-muted">
          Where embeddings, reranking, and citation verification (NLI) run. Bundled in-process by
          default — no separate encoder server. (Not an LLM model assignment.)
        </p>
      </header>

      {isLoading || !data ? (
        <p className="font-ui text-[0.86rem] text-text-faint">Loading…</p>
      ) : (
        <div className="flex flex-col gap-inline">
          {OPTIONS.map((opt) => {
            const active = data.remote === opt.remote;
            return (
              <button
                key={String(opt.remote)}
                type="button"
                data-disco-control="settings.encoder-mode"
                data-remote={opt.remote}
                onClick={() => !active && save.mutate({ remote: opt.remote, ...urls })}
                disabled={save.isPending}
                aria-pressed={active}
                className={cn(
                  "flex items-start gap-inline rounded-card border px-body py-inline text-left transition-colors",
                  active
                    ? "border-accent/50 bg-accent/5"
                    : "border-hairline hover:border-hairline-strong",
                  save.isPending && "opacity-60",
                )}
              >
                <opt.Icon
                  className={cn("mt-px size-4 shrink-0", active ? "text-accent" : "text-text-faint")}
                  aria-hidden
                />
                <span className="flex min-w-0 flex-col gap-hair">
                  <span className="flex items-center gap-hair font-ui text-[0.9rem] font-medium text-text">
                    {opt.label}
                    {active && save.isPending && (
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

          {/* Endpoint fields. Rendered always (so the config is legible), but the
              inputs are honestly DISABLED + flagged when Bundled is active — the
              agent-server ignores these URLs unless Remote is the selected mode, so
              an editable-looking-but-ignored field would be a false affordance. */}
          <div className="mt-hair flex flex-col gap-inline rounded-card border border-hairline bg-surface-1/40 px-body py-inline">
            <p className="font-ui text-[0.78rem] text-text-muted">
              Remote endpoints — leave blank to use the agent-server's configured default.
            </p>
            {!data.remote && (
              <p
                role="note"
                data-disco-flag="encoder-endpoints-inactive"
                className="font-ui text-[0.76rem] text-text-faint"
              >
                Inactive while Bundled is selected — these endpoints apply only when Remote is the
                active mode. Switch to Remote endpoints above to edit them.
              </p>
            )}
            {ENDPOINTS.map((ep) => (
              <label key={ep.key} className="flex flex-col gap-hair">
                <span className="flex items-baseline gap-hair font-ui text-[0.8rem] text-text">
                  {ep.label}
                  <span className="font-ui text-[0.72rem] text-text-faint">· {ep.hint}</span>
                </span>
                <input
                  type="url"
                  inputMode="url"
                  spellCheck={false}
                  data-disco-control="settings.encoder-endpoint"
                  data-endpoint={ep.key}
                  disabled={!data.remote || save.isPending}
                  value={urls[ep.key]}
                  onChange={(e) => setUrls((u) => ({ ...u, [ep.key]: e.target.value }))}
                  placeholder="http://host:port  (empty = server default)"
                  className="rounded-control border border-hairline bg-bg px-inline py-hair font-mono text-[0.78rem] text-text outline-none transition-colors placeholder:text-text-faint focus:border-accent/60 disabled:opacity-50"
                />
              </label>
            ))}
            {data.remote && (
              <div className="flex items-center gap-inline">
                <button
                  type="button"
                  data-disco-control="settings.encoder-save"
                  disabled={!dirty || save.isPending}
                  onClick={() => save.mutate({ remote: true, ...urls })}
                  className={cn(
                    "flex items-center gap-hair self-start rounded-control border px-inline py-hair font-ui text-[0.8rem] transition-colors",
                    dirty && !save.isPending
                      ? "border-accent/50 bg-accent/10 text-text hover:bg-accent/20"
                      : "border-hairline text-text-faint",
                  )}
                >
                  {save.isPending ? (
                    <Loader2 className="size-3 animate-spin" aria-hidden />
                  ) : (
                    <Check className="size-3" aria-hidden />
                  )}
                  Save endpoints
                </button>
                {!dirty && !save.isPending && (
                  <span className="font-ui text-[0.76rem] text-text-faint">Saved</span>
                )}
              </div>
            )}
          </div>
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
