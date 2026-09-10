/**
 * The "remote endpoints" card for EncoderSection — the three endpoint inputs
 * (disabled + flagged while Bundled is active), the inactive note, the Save
 * endpoints action, and the trailing save error. Pulled out of the parent
 * purely to shed cyclomatic complexity (TS-0029); the parent still owns the
 * `urls` draft state and the save mutation.
 */

import { Check, Loader2 } from "lucide-react";
import { cn } from "@/lib/cn";
import type { EncoderUrls } from "./deriveEncoderState";

const ENDPOINTS = [
  {
    key: "embedder_url",
    label: "Embeddings endpoint",
    hint: "OpenAI-shape /v1",
  },
  { key: "reranker_url", label: "Reranker endpoint", hint: "TEI rerank" },
  { key: "nli_url", label: "NLI endpoint", hint: "entailment sidecar" },
] as const;

export function EncoderEndpointsPanel({
  remote,
  urls,
  onUrlChange,
  dirty,
  pending,
  onSaveEndpoints,
  error,
}: {
  remote: boolean;
  urls: EncoderUrls;
  onUrlChange: (key: keyof EncoderUrls, value: string) => void;
  dirty: boolean;
  pending: boolean;
  onSaveEndpoints: () => void;
  error: Error | null;
}) {
  return (
    <>
      <div className="mt-hair flex flex-col gap-inline rounded-card border border-hairline bg-surface-1/40 px-body py-inline">
        <p className="font-ui text-[0.78rem] text-text-muted">
          Remote endpoints — leave blank to use the agent-server's
          configured default.
        </p>
        {!remote && (
          <p
            role="note"
            data-disco-flag="encoder-endpoints-inactive"
            className="font-ui text-[0.76rem] text-text-faint"
          >
            Inactive while Bundled is selected — these endpoints apply only
            when Remote is the active mode. Switch to Remote endpoints above
            to edit them.
          </p>
        )}
        {ENDPOINTS.map((ep) => (
          <label key={ep.key} className="flex flex-col gap-hair">
            <span className="flex items-baseline gap-hair font-ui text-[0.8rem] text-text">
              {ep.label}
              <span className="font-ui text-[0.72rem] text-text-faint">
                · {ep.hint}
              </span>
            </span>
            <input
              type="url"
              inputMode="url"
              spellCheck={false}
              data-disco-control="settings.encoder-endpoint"
              data-endpoint={ep.key}
              disabled={!remote || pending}
              value={urls[ep.key]}
              onChange={(e) => onUrlChange(ep.key, e.target.value)}
              placeholder="http://host:port  (empty = server default)"
              className="min-h-11 rounded-control border border-hairline bg-bg px-inline py-hair font-mono text-[0.78rem] text-text outline-none transition-colors placeholder:text-text-faint focus:border-accent/60 disabled:opacity-50 lg:min-h-0"
            />
          </label>
        ))}
        {remote && (
          <div className="flex items-center gap-inline">
            <button
              type="button"
              data-disco-control="settings.encoder-save"
              disabled={!dirty || pending}
              onClick={onSaveEndpoints}
              className={cn(
                "flex min-h-11 items-center gap-hair self-start rounded-control border px-inline py-hair font-ui text-[0.8rem] transition-colors lg:min-h-0",
                dirty && !pending
                  ? "border-accent/50 bg-accent/10 text-text hover:bg-accent/20"
                  : "border-hairline text-text-faint",
              )}
            >
              {pending ? (
                <Loader2 className="size-3 animate-spin" aria-hidden />
              ) : (
                <Check className="size-3" aria-hidden />
              )}
              Save endpoints
            </button>
            {!dirty && !pending && (
              <span className="font-ui text-[0.76rem] text-text-faint">
                Saved
              </span>
            )}
          </div>
        )}
      </div>
      {error && (
        <p className="font-ui text-[0.8rem] text-warn">
          Couldn't save: {error.message}
        </p>
      )}
    </>
  );
}
