/**
 * Settings → Encoders. Where the non-generative models — embeddings, reranking,
 * and citation-entailment (NLI) — run. These are NOT LLM-router roles (they don't
 * follow the model assignments), so they live here, not in the model matrix.
 *
 * Two honest, wired options:
 *  - Bundled (local): in-process ONNX/CPU (fastembed) — self-contained, no encoder
 *    server, faster than a network hop. The default.
 *  - Remote: external LAN endpoints (TEI rerank / OpenAI-shape embeddings /
 *    NLI sidecar), configured below.
 *
 * The toggle persists to the app-server and the agent-server honors it on the next
 * research run (no restart) — a real control, not a label.
 */

import { useEffect, useState } from "react";
import { Cpu, Loader2, Server } from "lucide-react";
import { cn } from "@/lib/cn";
import { useEncodersConfig, useUpdateEncodersConfig } from "@/hooks/useModels";
import { isEncoderUrlsDirty } from "./encoderSectionParts/deriveEncoderState";
import { EncoderEndpointsPanel } from "./encoderSectionParts/EncoderEndpointsPanel";

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
    help: "External LAN services for reranking, embeddings, and NLI. Enter endpoint URLs below.",
  },
] as const;

export function EncoderSection() {
  const { data, isLoading } = useEncodersConfig();
  const save = useUpdateEncodersConfig();

  // Local draft of the three endpoint URLs (controlled inputs), synced from the
  // persisted config. Saved as a unit alongside remote=true.
  const [urls, setUrls] = useState({
    embedder_url: "",
    reranker_url: "",
    nli_url: "",
  });
  useEffect(() => {
    if (data)
      setUrls({
        embedder_url: data.embedder_url ?? "",
        reranker_url: data.reranker_url ?? "",
        nli_url: data.nli_url ?? "",
      });
  }, [data]);

  const dirty = isEncoderUrlsDirty(urls, data);
  const activeOption = data
    ? OPTIONS.find((option) => option.remote === data.remote)
    : undefined;

  return (
    <section className="flex flex-col gap-inline">
      <header>
        <h3 className="font-ui text-[0.95rem] font-semibold text-text">
          Encoders
        </h3>
        <p className="mt-hair font-ui text-[0.86rem] text-text-muted">
          Where embeddings, reranking, and citation verification (NLI) run.
          Bundled in-process by default — no separate encoder server. (Not an
          LLM model assignment.)
        </p>
      </header>

      {isLoading || !data ? (
        <p className="font-ui text-[0.86rem] text-text-faint">Loading…</p>
      ) : (
        <div className="flex flex-col gap-inline">
          <div
            data-encoder-current={data.remote ? "remote" : "bundled"}
            className="flex items-start gap-inline rounded-control border border-hairline bg-surface-1/40 px-body py-inline"
          >
            {activeOption && (
              <activeOption.Icon
                className="mt-px size-4 shrink-0 text-accent"
                aria-hidden
              />
            )}
            <span className="flex min-w-0 flex-col gap-hair">
              <span className="font-ui text-[0.86rem] font-medium text-text">
                Current: {activeOption?.label}
              </span>
              <span className="font-ui text-[0.78rem] leading-snug text-text-faint">
                {activeOption?.help}
              </span>
            </span>
          </div>

          <details className="rounded-control border border-hairline px-body py-inline">
            <summary className="cursor-pointer py-3 font-ui text-[0.84rem] font-medium text-text lg:py-0">
              Configure encoders
            </summary>
            <div className="mt-inline flex flex-col gap-inline">
              {OPTIONS.map((opt) => {
                const active = data.remote === opt.remote;
                return (
                  <button
                    key={String(opt.remote)}
                    type="button"
                    data-disco-control="settings.encoder-mode"
                    data-remote={opt.remote}
                    onClick={() =>
                      !active && save.mutate({ remote: opt.remote, ...urls })
                    }
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
                      className={cn(
                        "mt-px size-4 shrink-0",
                        active ? "text-accent" : "text-text-faint",
                      )}
                      aria-hidden
                    />
                    <span className="flex min-w-0 flex-col gap-hair">
                      <span className="flex items-center gap-hair font-ui text-[0.9rem] font-medium text-text">
                        {opt.label}
                        {active && save.isPending && (
                          <Loader2
                            className="size-3 animate-spin text-accent"
                            aria-hidden
                          />
                        )}
                      </span>
                      <span className="font-ui text-[0.8rem] leading-relaxed text-text-faint">
                        {opt.help}
                      </span>
                    </span>
                  </button>
                );
              })}

              <EncoderEndpointsPanel
                remote={data.remote}
                urls={urls}
                onUrlChange={(key, value) =>
                  setUrls((current) => ({ ...current, [key]: value }))
                }
                dirty={dirty}
                pending={save.isPending}
                onSaveEndpoints={() => save.mutate({ remote: true, ...urls })}
                error={save.error}
              />
            </div>
          </details>
        </div>
      )}
    </section>
  );
}
