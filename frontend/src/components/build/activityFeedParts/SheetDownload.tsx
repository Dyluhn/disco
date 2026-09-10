/**
 * ActivityFeed — the generated-spreadsheet download card, extracted verbatim
 * from ActivityFeed.tsx so the feed module stays inside the logical-line
 * budget. Same component, same route, same markup: not a fork.
 */

import { Download, FileSpreadsheet } from "lucide-react";
import { artifactUrl } from "@/api/artifacts";
import type { ActivityItem } from "@/lib/buildTrace";

/** A generated spreadsheet — a real download via the declared-artifact route
 * (encodeURI preserves any subdir slashes). Honest: only renders when there's a
 * conversation id to fetch against; the file's live formulas compute on open.
 *
 * Exported (D12) so SheetBlock (AnswerDocument path) can reuse the EXACT same
 * download affordance when a cid is threaded down — no fork, no divergence. */
export function SheetDownload({
  sheet,
  conversationId,
}: {
  sheet: NonNullable<ActivityItem["expandable"]>["sheet"];
  conversationId: string;
}) {
  if (!sheet) return null;
  const href = artifactUrl(conversationId, sheet.filename);
  const n = sheet.sheet_names?.length ?? 0;
  return (
    <a
      href={href}
      download
      data-disco-control="build.activity-download"
      data-download-kind="sheet"
      className="mt-hair flex items-center gap-inline rounded-card border border-hairline bg-surface-0 px-inline py-hair transition-colors hover:border-hairline-strong"
    >
      <FileSpreadsheet className="size-4 shrink-0 text-accent" aria-hidden />
      <span className="min-w-0 flex-1">
        <span className="block truncate font-ui text-[0.82rem] text-text">
          {sheet.title || sheet.filename}
        </span>
        <span className="block truncate font-mono text-[0.7rem] text-text-faint">
          {sheet.filename}
          {n > 0 ? ` · ${n} sheet${n !== 1 ? "s" : ""}` : ""}
        </span>
      </span>
      <Download className="size-3.5 shrink-0 text-text-faint" aria-hidden />
    </a>
  );
}
