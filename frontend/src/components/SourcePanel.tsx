import * as Tabs from "@radix-ui/react-tabs";
import { Ban, CheckCircle2, FileX2, Filter, Lock, MinusCircle, RefreshCw, XCircle } from "lucide-react";
import { useMemo, useState } from "react";
import { cn } from "@/lib/cn";
import { STATUS_LABEL, cleanDomain, isFailedStatus, monogram } from "@/lib/sources";
import type { ExtractStatus, GroundedAnswer, SearchHit, Verdict } from "@/types/grounded";

const FAIL_ICON: Partial<Record<ExtractStatus, typeof Lock>> = {
  paywalled: Lock,
  blocked: Ban,
  not_found: FileX2,
  error: FileX2,
};

function HitRow({ hit }: { hit: SearchHit }) {
  const failed = isFailedStatus(hit.status);
  const Icon = hit.status ? FAIL_ICON[hit.status] : undefined;
  return (
    <li className="flex items-start gap-inline border-b border-hairline py-inline last:border-0">
      <span
        aria-hidden
        className={cn(
          "mt-px grid size-5 shrink-0 place-items-center rounded-control font-ui text-[0.68rem]",
          failed ? "bg-surface-2 text-text-faint" : "bg-surface-2 text-text-muted",
        )}
      >
        {monogram(hit.url)}
      </span>
      <div className="min-w-0 flex-1">
        <a
          href={hit.url}
          target="_blank"
          rel="noreferrer"
          className={cn(
            "block truncate font-ui text-[0.82rem] hover:text-link",
            failed ? "text-text-muted line-through decoration-text-faint/50" : "text-text",
          )}
        >
          {hit.title}
        </a>
        <span className="font-ui text-[0.72rem] text-text-faint">{cleanDomain(hit.url)}</span>
      </div>
      {failed && Icon && (
        // explicit failure — visible, never silently dropped (BoD §13.3)
        <span className="ml-auto flex shrink-0 items-center gap-hair font-ui text-[0.68rem] text-warn">
          <Icon className="size-3" aria-hidden />
          {hit.status ? STATUS_LABEL[hit.status] : ""}
        </span>
      )}
    </li>
  );
}

interface Props {
  answer: GroundedAnswer | null;
  onReScope: (partial: { domains_deny?: string[]; drop_weak?: boolean }) => void;
}

/** Fixed-height dual-tab source panel: "All Searched" (full discovery set,
 * incl. failures) vs "Cited" (passages used). Re-scope controls re-issue
 * retrieval. The fixed height is a CLS guard — it never shoves the answer. */
export function SourcePanel({ answer, onReScope }: Props) {
  const [denied, setDenied] = useState<string[]>([]);
  const allHits = answer?.all_hits ?? [];
  const cited = answer?.passages ?? [];
  const hasWeak = (answer?.unsupported_count ?? 0) > 0 || (answer?.claims.some((c) => c.verdict !== "supported") ?? false);

  /** Per-source verdict rollup: for each cited passage, collect the worst
   *  verdict among claims that cite it so the source list can annotate which
   *  sources back weak/unsupported claims. */
  const passageVerdict = useMemo(() => {
    const map = new Map<string, Verdict>();
    if (!answer) return map;
    for (const c of answer.claims) {
      for (const pid of c.claim.cited_passage_ids) {
        const cur = map.get(pid);
        if (cur === "unsupported") continue;
        if (cur === "weak" && c.verdict === "supported") continue;
        map.set(pid, c.verdict);
      }
    }
    return map;
  }, [answer]);

  /** Sort cited passages: those backing unsupported/weak claims first. */
  const sortedCited = useMemo(() => {
    const order: Record<Verdict, number> = { unsupported: 0, weak: 1, supported: 2 };
    const cite = [...(answer?.passages ?? [])];
    cite.sort((a, b) => {
      const va = passageVerdict.get(a.id);
      const vb = passageVerdict.get(b.id);
      return (va ? order[va] : 3) - (vb ? order[vb] : 3);
    });
    return cite;
  }, [answer, passageVerdict]);

  const verdictIcon = (v: Verdict | undefined) => {
    if (v === "supported") return <CheckCircle2 className="mt-px size-3 shrink-0 text-supported" aria-label="Supported" />;
    if (v === "weak") return <MinusCircle className="mt-px size-3 shrink-0 text-weak" aria-label="Weak" />;
    if (v === "unsupported") return <XCircle className="mt-px size-3 shrink-0 text-unsupported" aria-label="Unsupported" />;
    return null;
  };

  const toggleDeny = (domain: string) => {
    const next = denied.includes(domain) ? denied.filter((d) => d !== domain) : [...denied, domain];
    setDenied(next);
    onReScope({ domains_deny: next });
  };

  return (
    <section className="flex h-72 flex-col rounded-card border border-hairline bg-bg">
      <Tabs.Root defaultValue="all" className="flex min-h-0 flex-1 flex-col">
        <div className="flex items-center justify-between border-b border-hairline px-body">
          <Tabs.List className="flex gap-section">
            {[
              ["all", `All Searched`, allHits.length],
              ["cited", `Cited`, cited.length],
            ].map(([v, label, count]) => (
              <Tabs.Trigger
                key={v as string}
                value={v as string}
                className="-mb-px border-b-2 border-transparent py-inline font-ui text-[0.82rem] text-text-muted transition-colors data-[state=active]:border-accent data-[state=active]:text-text"
              >
                {label as string}
                <span className="ml-hair text-text-faint">{count as number}</span>
              </Tabs.Trigger>
            ))}
          </Tabs.List>
          <div className="flex items-center gap-inline">
            <button
              type="button"
              onClick={() => onReScope({ drop_weak: true })}
              disabled={!hasWeak}
              className="flex items-center gap-hair rounded-control px-inline py-hair font-ui text-[0.74rem] text-text-muted transition-colors hover:text-text disabled:opacity-40"
            >
              <Filter className="size-3" aria-hidden />
              Drop weak
            </button>
            <button
              type="button"
              onClick={() => onReScope({})}
              className="flex items-center gap-hair rounded-control px-inline py-hair font-ui text-[0.74rem] text-text-muted transition-colors hover:text-text"
            >
              <RefreshCw className="size-3" aria-hidden />
              Re-synthesize
            </button>
          </div>
        </div>

        <Tabs.Content value="all" className="min-h-0 flex-1 overflow-y-auto px-body">
          <ul className="flex flex-col">
            {allHits.map((hit) => (
              <div key={hit.url} className="group flex items-center">
                <div className="flex-1">
                  <HitRow hit={hit} />
                </div>
                <button
                  type="button"
                  aria-label={`Filter out ${cleanDomain(hit.url)}`}
                  onClick={() => toggleDeny(cleanDomain(hit.url))}
                  className={cn(
                    "ml-inline shrink-0 rounded-control p-hair font-ui text-[0.68rem] transition-colors",
                    denied.includes(cleanDomain(hit.url))
                      ? "text-warn"
                      : "text-text-faint opacity-0 hover:text-text group-hover:opacity-100",
                  )}
                >
                  filter
                </button>
              </div>
            ))}
            {allHits.length === 0 && (
              <li className="py-section font-ui text-[0.82rem] text-text-faint">No sources yet.</li>
            )}
          </ul>
        </Tabs.Content>

        <Tabs.Content value="cited" className="min-h-0 flex-1 overflow-y-auto px-body">
          <ul className="flex flex-col">
            {sortedCited.map((p, i) => {
              const v = passageVerdict.get(p.id);
              return (
                <li
                  key={p.id}
                  className="flex items-start gap-inline border-b border-hairline py-inline last:border-0"
                >
                  <span className="mt-px font-ui text-[0.72rem] text-accent">[{i + 1}]</span>
                  {verdictIcon(v)}
                  <div className="min-w-0">
                    <a
                      href={p.source_url}
                      target="_blank"
                      rel="noreferrer"
                      className="block truncate font-ui text-[0.82rem] text-text hover:text-link"
                    >
                      {p.source_title}
                    </a>
                    <span className="font-ui text-[0.72rem] text-text-faint">
                      {cleanDomain(p.source_url)}
                    </span>
                  </div>
                </li>
              );
            })}
            {cited.length === 0 && (
              <li className="py-section font-ui text-[0.82rem] text-text-faint">
                Nothing cited yet.
              </li>
            )}
          </ul>
        </Tabs.Content>
      </Tabs.Root>
    </section>
  );
}
