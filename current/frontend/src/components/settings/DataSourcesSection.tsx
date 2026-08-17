/**
 * Settings → Data sources. The two web-data providers — SEARCH (discovery) and
 * EXTRACTION (URL → clean content) — each with the three universal tiers:
 *   • Bundled  — keyless, in-process, ships with the app (the first-run default)
 *   • Self-host — your own container (a base URL)
 *   • Paid     — a hosted API (a BYO key stored in Providers)
 *
 * This is the "download + it just works" surface: a fresh install runs on the
 * bundled tiers with zero config; this is where you upgrade. Persisted in the
 * shared config (not ephemeral) and honored on the next research run.
 */

import { useEffect, useState } from "react";
import { Container, Globe, KeyRound, Package } from "lucide-react";
import { cn } from "@/lib/cn";
import { apiIsLive } from "@/api/liveness";
import { testDataSource } from "@/api/models";
import {
  useDataSourcesConfig,
  useUpdateDataSourcesConfig,
} from "@/hooks/useModels";
import type {
  DataSourcesConfig,
  ExtractionProvider,
  SearchProvider,
} from "@/types/models";
import { ProbeButton } from "./ProbeButton";
import { StoredCredentialField } from "./StoredCredentialField";
import { DataSourcesSaveRow } from "./dataSourcesSectionParts/DataSourcesSaveRow";

type Tier = "bundled" | "selfhost" | "paid";

interface Opt<P extends string> {
  id: P;
  label: string;
  tier: Tier;
  help: string;
  baseUrlLabel?: string;
  baseUrlPlaceholder?: string;
  baseUrlType?: "text" | "url";
  showApiKeyEnv?: boolean;
  apiKeyPlaceholder?: string;
}

const SEARCH_OPTS: Opt<SearchProvider>[] = [
  {
    id: "ddgs",
    tier: "bundled",
    label: "Bundled — ddgs",
    help: "Keyless, in-process. No setup. Moderate rate limits — fine for personal use. The default.",
  },
  {
    id: "searxng",
    tier: "selfhost",
    label: "Self-hosted SearXNG",
    help: "Your own SearXNG instance — set its URL below.",
  },
  {
    id: "news",
    tier: "bundled",
    label: "News",
    help: "Recent news headlines via Google News. No key required.",
  },
  {
    id: "arxiv",
    tier: "bundled",
    label: "arXiv",
    help: "Academic preprints (physics, CS, math). No key required.",
  },
  {
    id: "semantic_scholar",
    tier: "bundled",
    label: "Semantic Scholar",
    help: "Academic papers + abstracts. Optional API key raises rate limits.",
    showApiKeyEnv: true,
    apiKeyPlaceholder: "e.g. SEMANTIC_SCHOLAR_API_KEY",
  },
  {
    id: "site_scoped",
    tier: "bundled",
    label: "Site-scoped",
    help: "Restrict web search to specific domains.",
    baseUrlLabel: "Domains (comma-separated)",
    baseUrlPlaceholder: "example.com, docs.example.org",
    baseUrlType: "text",
  },
  {
    id: "tavily",
    tier: "paid",
    label: "Tavily (paid)",
    help: "Hosted search API. Store its key in Providers.",
  },
  {
    id: "brave",
    tier: "paid",
    label: "Brave Search (paid)",
    help: "Brave's hosted Search API. Store its key in Providers.",
    apiKeyPlaceholder: "e.g. BRAVE_SEARCH_API_KEY",
  },
];

const EXTRACT_OPTS: Opt<ExtractionProvider>[] = [
  {
    id: "local",
    tier: "bundled",
    label: "Bundled — in-process",
    help: "Fetch + readability→markdown, no service. Works offline-of-services. The default.",
  },
  {
    id: "crawl4ai",
    tier: "selfhost",
    label: "Self-hosted Crawl4AI",
    help: "Your own Crawl4AI instance — set its URL below.",
  },
  {
    id: "firecrawl",
    tier: "paid",
    label: "Firecrawl (paid)",
    help: "Hosted scrape API → clean markdown. Store its key in Providers.",
  },
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
  const showBaseUrl = active?.tier === "selfhost" || !!active?.baseUrlLabel;
  const showApiKeyEnv = active?.tier === "paid" || !!active?.showApiKeyEnv;
  const ActiveTierIcon = active ? TIER_ICON[active.tier] : Package;
  return (
    <div className="flex flex-col gap-inline">
      <h3 className="flex items-center gap-hair font-ui text-[0.95rem] font-medium text-text">
        <Icon className="size-4 text-text-faint" aria-hidden />
        {title}
      </h3>
      <div className="flex items-start gap-inline rounded-control border border-hairline bg-surface-1/40 px-body py-inline">
        <ActiveTierIcon className="mt-px size-4 shrink-0 text-accent" aria-hidden />
        <span className="flex min-w-0 flex-col gap-hair">
          <span className="font-ui text-[0.86rem] font-medium text-text">
            Current: {active?.label ?? value}
          </span>
          <span className="font-ui text-[0.76rem] leading-snug text-text-faint">
            {active?.help}
          </span>
        </span>
      </div>

      <details className="rounded-control border border-hairline px-body py-inline">
        <summary className="cursor-pointer font-ui text-[0.84rem] font-medium text-text">
          Configure {title.toLowerCase()}
        </summary>
        <div className="mt-inline flex flex-col gap-inline">
          <div className="grid gap-hair sm:grid-cols-3">
            {opts.map((option) => {
              const TierIcon = TIER_ICON[option.tier];
              const on = option.id === value;
              return (
                <button
                  key={option.id}
                  type="button"
                  data-disco-control="settings.datasource-pick"
                  data-provider-id={option.id}
                  onClick={() => onPick(option.id)}
                  aria-pressed={on}
                  className={cn(
                    "flex flex-col gap-hair rounded-control border px-inline py-inline text-left transition-colors",
                    on
                      ? "border-accent/60 bg-accent/5"
                      : "border-hairline hover:border-hairline-strong",
                  )}
                >
                  <span className="flex items-center gap-hair font-ui text-[0.82rem] font-medium text-text">
                    <TierIcon
                      className={cn(
                        "size-3.5",
                        on ? "text-accent" : "text-text-faint",
                      )}
                      aria-hidden
                    />
                    {option.label}
                  </span>
                  <span className="font-ui text-[0.74rem] leading-snug text-text-faint">
                    {option.help}
                  </span>
                </button>
              );
            })}
          </div>
          {showBaseUrl && (
            <label className="flex flex-col gap-hair">
              <span className="font-ui text-[0.78rem] text-text-muted">
                {active?.baseUrlLabel ?? "Service URL"}
              </span>
              <input
                type={active?.baseUrlType ?? "url"}
                spellCheck={false}
                value={baseUrl}
                onChange={(event) => onBaseUrl(event.target.value)}
                placeholder={
                  active?.baseUrlPlaceholder ??
                  "http://host:port  (empty = server default)"
                }
                className="rounded-control border border-hairline bg-bg px-inline py-hair font-mono text-[0.78rem] text-text outline-none transition-colors placeholder:text-text-faint focus:border-accent/60"
              />
            </label>
          )}
          {showApiKeyEnv && (
            <StoredCredentialField
              label={`${title} credential`}
              value={apiKeyEnv}
              onChange={onApiKeyEnv}
              placeholder={active?.apiKeyPlaceholder ?? "e.g. TAVILY_API_KEY"}
              optional={active?.tier !== "paid"}
            />
          )}
        </div>
      </details>
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

  const dirty =
    !!data && !!draft && JSON.stringify(draft) !== JSON.stringify(data);
  const set = (patch: Partial<DataSourcesConfig>) =>
    setDraft((d) => (d ? { ...d, ...patch } : d));
  const pickSearchProvider = (id: SearchProvider) => {
    const patch: Partial<DataSourcesConfig> = { search_provider: id };
    if (id !== "searxng" && id !== "site_scoped") patch.search_base_url = "";
    if (id !== "tavily" && id !== "brave" && id !== "semantic_scholar") {
      patch.search_api_key_env = "";
    }
    if (draft?.search_provider !== id) {
      if (id === "searxng" || id === "site_scoped") patch.search_base_url = "";
      if (id === "tavily" || id === "brave" || id === "semantic_scholar") {
        patch.search_api_key_env = "";
      }
    }
    set(patch);
  };

  return (
    <section className="flex flex-col gap-inline">
      <header>
        <h3 className="font-ui text-[0.95rem] font-semibold text-text">
          Data sources
        </h3>
        <p className="mt-hair font-ui text-[0.86rem] text-text-muted">
          Where web search and content extraction run. Bundled, keyless options
          by default — the app works the moment it's installed. Search uses this
          default when no per-query sources are chosen.
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
            onPick={pickSearchProvider}
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

          {/* T4.2 live probes: a real reachability check against the SAVED
              search / extraction endpoint. Bundled tiers report honestly that
              there's nothing to reach; remote tiers do a real GET. Disabled until
              changes are saved (the probe tests persisted config) and a backend
              is connected — no false affordance. */}
          <div className="flex flex-col gap-hair">
            <span className="font-ui text-[0.76rem] text-text-muted">
              Test connection
            </span>
            <ProbeButton
              control="settings.datasource-test-search"
              idleLabel="Test search"
              run={() => testDataSource("search")}
              disabled={!apiIsLive() || dirty}
              disabledHint={
                dirty ? "save changes to test" : "connect a backend to test"
              }
            />
            <ProbeButton
              control="settings.datasource-test-extraction"
              idleLabel="Test extraction"
              run={() => testDataSource("extraction")}
              disabled={!apiIsLive() || dirty}
              disabledHint={
                dirty ? "save changes to test" : "connect a backend to test"
              }
            />
          </div>

          <DataSourcesSaveRow
            dirty={dirty}
            pending={save.isPending}
            error={save.error}
            onSave={() => draft && save.mutate(draft)}
          />
        </div>
      )}
    </section>
  );
}
