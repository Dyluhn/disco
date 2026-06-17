/**
 * Settings → Data sources. The two web-data providers — SEARCH (discovery) and
 * EXTRACTION (URL → clean content) — each with the three universal tiers:
 *   • Bundled  — keyless, in-process, ships with the app (the first-run default)
 *   • Self-host — your own container (a base URL)
 *   • Paid     — a hosted API (a BYO key, referenced by env-var NAME, never typed)
 *
 * This is the "download + it just works" surface: a fresh install runs on the
 * bundled tiers with zero config; this is where you upgrade. Persisted in the
 * shared config (not ephemeral) and honored on the next research run.
 */

import { useEffect, useState } from "react";
import { Check, Container, Globe, KeyRound, Loader2, Package } from "lucide-react";
import { cn } from "@/lib/cn";
import { useDataSourcesConfig, useUpdateDataSourcesConfig } from "@/hooks/useModels";
import type { DataSourcesConfig, ExtractionProvider, SearchProvider } from "@/types/models";

type Tier = "bundled" | "selfhost" | "paid";

interface Opt<P extends string> {
  id: P;
  label: string;
  tier: Tier;
  help: string;
}

const SEARCH_OPTS: Opt<SearchProvider>[] = [
  { id: "ddgs", tier: "bundled", label: "Bundled — ddgs", help: "Keyless, in-process. No setup. Moderate rate limits — fine for personal use. The default." },
  { id: "searxng", tier: "selfhost", label: "Self-hosted SearXNG", help: "Your own SearXNG instance — set its URL below." },
  { id: "tavily", tier: "paid", label: "Tavily (paid)", help: "Hosted search API. Set the env-var name that holds your key." },
];

const EXTRACT_OPTS: Opt<ExtractionProvider>[] = [
  { id: "local", tier: "bundled", label: "Bundled — in-process", help: "Fetch + readability→markdown, no service. Works offline-of-services. The default." },
  { id: "crawl4ai", tier: "selfhost", label: "Self-hosted Crawl4AI", help: "Your own Crawl4AI instance — set its URL below." },
  { id: "firecrawl", tier: "paid", label: "Firecrawl (paid)", help: "Hosted scrape API → clean markdown. Set the env-var name that holds your key." },
];

const TIER_ICON: Record<Tier, typeof Package> = {
  bundled: Package,
  selfhost: Container,
  paid: KeyRound,
};

function ProviderGroup<P extends string>({
  title,
  Icon,
  opts,
  value,
  baseUrl,
  apiKeyEnv,
  onPick,
  onBaseUrl,
  onApiKeyEnv,
}: {
  title: string;
  Icon: typeof Globe;
  opts: Opt<P>[];
  value: P;
  baseUrl: string;
  apiKeyEnv: string;
  onPick: (id: P) => void;
  onBaseUrl: (v: string) => void;
  onApiKeyEnv: (v: string) => void;
}) {
  const active = opts.find((o) => o.id === value);
  return (
    <div className="flex flex-col gap-inline">
      <h3 className="flex items-center gap-hair font-ui text-[0.95rem] font-medium text-text">
        <Icon className="size-4 text-text-faint" aria-hidden />
        {title}
      </h3>
      <div className="grid gap-hair sm:grid-cols-3">
        {opts.map((o) => {
          const TierIcon = TIER_ICON[o.tier];
          const on = o.id === value;
          return (
            <button
              key={o.id}
              type="button"
              onClick={() => onPick(o.id)}
              aria-pressed={on}
              className={cn(
                "flex flex-col gap-hair rounded-control border px-inline py-inline text-left transition-colors",
                on ? "border-accent/60 bg-accent/5" : "border-hairline hover:border-hairline-strong",
              )}
            >
              <span className="flex items-center gap-hair font-ui text-[0.82rem] font-medium text-text">
                <TierIcon className={cn("size-3.5", on ? "text-accent" : "text-text-faint")} aria-hidden />
                {o.label}
              </span>
              <span className="font-ui text-[0.74rem] leading-snug text-text-faint">{o.help}</span>
            </button>
          );
        })}
      </div>
      {/* contextual field for the active tier */}
      {active?.tier === "selfhost" && (
        <label className="flex flex-col gap-hair">
          <span className="font-ui text-[0.78rem] text-text-muted">Service URL</span>
          <input
            type="url"
            spellCheck={false}
            value={baseUrl}
            onChange={(e) => onBaseUrl(e.target.value)}
            placeholder="http://host:port  (empty = server default)"
            className="rounded-control border border-hairline bg-bg px-inline py-hair font-mono text-[0.78rem] text-text outline-none transition-colors placeholder:text-text-faint focus:border-accent/60"
          />
        </label>
      )}
      {active?.tier === "paid" && (
        <label className="flex flex-col gap-hair">
          <span className="font-ui text-[0.78rem] text-text-muted">
            API-key env var <span className="text-text-faint">(the NAME, not the key)</span>
          </span>
          <input
            spellCheck={false}
            value={apiKeyEnv}
            onChange={(e) => onApiKeyEnv(e.target.value)}
            placeholder="e.g. TAVILY_API_KEY"
            className="rounded-control border border-hairline bg-bg px-inline py-hair font-mono text-[0.78rem] text-text outline-none transition-colors placeholder:text-text-faint focus:border-accent/60"
          />
        </label>
      )}
    </div>
  );
}

export function DataSourcesSection() {
  const { data, isLoading } = useDataSourcesConfig();
  const save = useUpdateDataSourcesConfig();
  const [draft, setDraft] = useState<DataSourcesConfig | null>(null);

  useEffect(() => {
    if (data) setDraft(data);
  }, [data]);

  const dirty = !!data && !!draft && JSON.stringify(draft) !== JSON.stringify(data);
  const set = (patch: Partial<DataSourcesConfig>) =>
    setDraft((d) => (d ? { ...d, ...patch } : d));

  return (
    <section className="flex flex-col gap-section border-t border-hairline pt-section">
      <header>
        <h2 className="font-display text-[1.3rem] tracking-tight text-text">Data sources</h2>
        <p className="mt-hair font-ui text-[0.86rem] text-text-muted">
          Where web search and content extraction run. Bundled, keyless options by default — the
          app works the moment it's installed. Upgrade to self-hosted or paid here.
        </p>
      </header>

      {isLoading || !draft ? (
        <p className="font-ui text-[0.86rem] text-text-faint">Loading…</p>
      ) : (
        <div className="flex flex-col gap-section">
          <ProviderGroup
            title="Search"
            Icon={Globe}
            opts={SEARCH_OPTS}
            value={draft.search_provider}
            baseUrl={draft.search_base_url}
            apiKeyEnv={draft.search_api_key_env}
            onPick={(id) => set({ search_provider: id })}
            onBaseUrl={(v) => set({ search_base_url: v })}
            onApiKeyEnv={(v) => set({ search_api_key_env: v })}
          />
          <ProviderGroup
            title="Extraction"
            Icon={Globe}
            opts={EXTRACT_OPTS}
            value={draft.extraction_provider}
            baseUrl={draft.extraction_base_url}
            apiKeyEnv={draft.extraction_api_key_env}
            onPick={(id) => set({ extraction_provider: id })}
            onBaseUrl={(v) => set({ extraction_base_url: v })}
            onApiKeyEnv={(v) => set({ extraction_api_key_env: v })}
          />

          <div className="flex items-center gap-inline">
            <button
              type="button"
              disabled={!dirty || save.isPending}
              onClick={() => draft && save.mutate(draft)}
              className={cn(
                "flex items-center gap-hair self-start rounded-control border px-inline py-hair font-ui text-[0.82rem] transition-colors",
                dirty && !save.isPending
                  ? "border-accent/50 bg-accent/10 text-text hover:bg-accent/20"
                  : "border-hairline text-text-faint",
              )}
            >
              {save.isPending ? (
                <Loader2 className="size-3.5 animate-spin" aria-hidden />
              ) : (
                <Check className="size-3.5" aria-hidden />
              )}
              Save data sources
            </button>
            {!dirty && !save.isPending && (
              <span className="font-ui text-[0.76rem] text-text-faint">Saved</span>
            )}
            {save.error && (
              <span className="font-ui text-[0.78rem] text-warn">
                Couldn't save: {(save.error as Error).message}
              </span>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
