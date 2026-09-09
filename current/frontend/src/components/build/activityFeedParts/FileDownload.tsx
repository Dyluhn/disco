/**
 * ActivityFeed — the agent-emitted file download card, extracted verbatim from
 * ActivityFeed.tsx so the feed module stays inside the logical-line budget.
 * Same component, same route, same markup: not a fork.
 */

import { Download, File } from "lucide-react";
import { artifactUrl } from "@/api/artifacts";
import type { ActivityItem } from "@/lib/buildTrace";

/** F2: An agent-emitted file (via serve(kind="files")) — a real download via the
 * declared-artifact route. Renders as a first-class download card in the conversation
 * feed. Only renders when there's a conversation id (no false affordance). */
export function FileDownload({
  file,
  conversationId,
}: {
  file: NonNullable<ActivityItem["expandable"]>["file"];
  conversationId: string;
}) {
  if (!file) return null;
  const href = artifactUrl(conversationId, file.filename);
  return (
    <a
      href={href}
      download
      data-disco-control="build.activity-download"
      data-download-kind="file"
      className="mt-hair flex items-center gap-inline rounded-card border border-hairline bg-surface-0 px-inline py-hair transition-colors hover:border-hairline-strong"
    >
      <File className="size-4 shrink-0 text-accent" aria-hidden />
      <span className="min-w-0 flex-1">
        <span className="block truncate font-ui text-[0.82rem] text-text">
          {file.title || file.filename}
        </span>
        <span className="block truncate font-mono text-[0.7rem] text-text-faint">
          {file.filename}
        </span>
      </span>
      <Download className="size-3.5 shrink-0 text-text-faint" aria-hidden />
    </a>
  );
}
