/**
 * TieredSourcePanel — Perplexity-style three-tier source distinction at the
 * scale of hundreds. Cited (earned a citation) / Reviewed (read, not cited) /
 * Discovered (found, not read). Same fixed-height + scrolling pattern as the
 * existing SourcePanel; grouped + scrollable, never a flat wall.
 *
 * Reuses the existing source row primitives (monogram, cleanDomain,
 * STATUS_LABEL) — pure data-driven render.
 */

import * as Tabs from "@radix-ui/react-tabs";
import { Ban, FileX2, Lock, type LucideIcon } from "lucide-react";
import { cn } from "@/lib/cn";
import { STATUS_LABEL, cleanDomain, monogram } from "@/lib/sources";
import type { SourceTiers } from "@/lib/deepResearchTrace";
import type { ExtractStatus } from "@/types/grounded";

const FAIL_ICON: Partial<Record<ExtractStatus, LucideIcon>> = {
  paywalled: Lock,
  blocked: Ban,
  not_found: FileX2,
  error: FileX2,
};

interface Props {
  tiers: SourceTiers;
}

interface Row {
  url: string;
  title: string;
  status?: ExtractStatus | string;
}

/** Normalize a Cited (Passage) row vs Reviewed/Discovered (SearchHit) row
 * into a single shape — both carry url + title, the tier itself determines
 * the chrome (numbered chip vs status badge). */
function toRow(raw: Record<string, unknown>, isPassage: boolean): Row {
  if (isPassage) {
    return {
      url: String(raw.source_url ?? ""),
      title: String(raw.source_title ?? "") || cleanDomain(String(raw.source_url ?? "")),
    };
  }
  return {
    url: String(raw.url ?? ""),
    title: String(raw.title ?? "") || cleanDomain(String(raw.url ?? "")),
    status: raw.status as ExtractStatus | undefined,
  };
}

function CitedRow({ idx, row }: { idx: number; row: Row }) {
  return (
    <li className="flex items-start gap-inline border-b border-hairline py-inline last:border-0">
      <span className="mt-px shrink-0 font-ui text-[0.72rem] text-accent">
        [{idx + 1}]
      </span>
      <div className="min-w-0 flex-1">
        <a
          href={row.url}
          target="_blank"
          rel="noreferrer"
          className="block truncate font-ui text-[0.82rem] text-text hover:text-link"
        >
          {row.title}
        </a>
        <span className="font-ui text-[0.72rem] text-text-faint">
          {cleanDomain(row.url)}
        </span>
      </div>
    </li>
  );
}

function HitRow({ row }: { row: Row }) {
  const status = row.status as ExtractStatus | undefined;
  const Icon = status ? FAIL_ICON[status] : undefined;
  const failed = Boolean(Icon);
  return (
    <li className="flex items-start gap-inline border-b border-hairline py-inline last:border-0">
      <span
        aria-hidden
        className={cn(
          "mt-px grid size-5 shrink-0 place-items-center rounded-control font-ui text-[0.68rem]",
          failed
            ? "bg-surface-2 text-text-faint"
            : "bg-surface-2 text-text-muted",
        )}
      >
        {monogram(row.url)}
      </span>
      <div className="min-w-0 flex-1">
        <a
          href={row.url}
          target="_blank"
          rel="noreferrer"
          className={cn(
            "block truncate font-ui text-[0.82rem] hover:text-link",
            failed
              ? "text-text-muted line-through decoration-text-faint/50"
              : "text-text",
          )}
        >
          {row.title}
        </a>
        <span className="font-ui text-[0.72rem] text-text-faint">
          {cleanDomain(row.url)}
        </span>
      </div>
      {failed && Icon && (
        <span className="ml-auto flex shrink-0 items-center gap-hair font-ui text-[0.68rem] text-warn">
          <Icon className="size-3" aria-hidden />
          {status ? STATUS_LABEL[status] : ""}
        </span>
      )}
    </li>
  );
}

export function TieredSourcePanel({ tiers }: Props) {
  const cited = tiers.cited.map((p) => toRow(p, true));
  const reviewed = tiers.reviewed.map((h) => toRow(h, false));
  const discovered = tiers.discovered.map((h) => toRow(h, false));

  const tabs: Array<{
    id: string;
    label: string;
    note: string;
    count: number;
    render: () => React.ReactNode;
  }> = [
    {
      id: "cited",
      label: "Cited",
      note: "Earned a citation",
      count: cited.length,
      render: () => (
        <ul className="px-body">
          {cited.map((row, i) => (
            <CitedRow key={row.url} idx={i} row={row} />
          ))}
          {cited.length === 0 && (
            <li className="py-body text-center font-ui text-[0.82rem] text-text-faint">
              No citations yet.
            </li>
          )}
        </ul>
      ),
    },
    {
      id: "reviewed",
      label: "Reviewed",
      note: "Read, not cited",
      count: reviewed.length,
      render: () => (
        <ul className="px-body">
          {reviewed.map((row) => (
            <HitRow key={row.url} row={row} />
          ))}
          {reviewed.length === 0 && (
            <li className="py-body text-center font-ui text-[0.82rem] text-text-faint">
              No reviewed-but-uncited sources.
            </li>
          )}
        </ul>
      ),
    },
    {
      id: "discovered",
      label: "Discovered",
      note: "Found, not read",
      count: discovered.length,
      render: () => (
        <ul className="px-body">
          {discovered.map((row) => (
            <HitRow key={row.url} row={row} />
          ))}
          {discovered.length === 0 && (
            <li className="py-body text-center font-ui text-[0.82rem] text-text-faint">
              No discovered-but-unread sources.
            </li>
          )}
        </ul>
      ),
    },
  ];

  return (
    <section className="flex h-72 flex-col rounded-card border border-hairline bg-bg">
      <Tabs.Root defaultValue="cited" className="flex min-h-0 flex-1 flex-col">
        <Tabs.List className="flex gap-section border-b border-hairline px-body">
          {tabs.map((t) => (
            <Tabs.Trigger
              key={t.id}
              value={t.id}
              title={t.note}
              className="-mb-px border-b-2 border-transparent py-inline font-ui text-[0.82rem] text-text-muted transition-colors data-[state=active]:border-accent data-[state=active]:text-text"
            >
              {t.label}
              <span className="ml-hair text-text-faint">{t.count}</span>
            </Tabs.Trigger>
          ))}
        </Tabs.List>
        {tabs.map((t) => (
          <Tabs.Content
            key={t.id}
            value={t.id}
            className="min-h-0 flex-1 overflow-y-auto"
          >
            {t.render()}
          </Tabs.Content>
        ))}
      </Tabs.Root>
    </section>
  );
}
