/**
 * The finished-artifact HANDOFF panel. When the agent declares a deliverable via
 * the `serve` tool (a DeliverableEvent → `deriveDeliverable`), this panel turns
 * "the run ended" into "here is your thing": a named result with one real action
 * — open the live app, or download the files.
 *
 * No false affordance (project rule): it renders ONLY when a deliverable exists,
 * and its single button does exactly one wired thing. `kind` drives the
 * affordance — "app" opens the live preview, "files" downloads. Presentational:
 * the parent wires `onOpen`/`onDownload` to the real preview/download machinery.
 */

import { Download, ExternalLink, PackageCheck } from "lucide-react";
import type { DeliverableView } from "@/lib/buildTrace";

export function DeliverablePanel({
  deliverable,
  onOpen,
  onDownload,
}: {
  deliverable: DeliverableView | null;
  onOpen?: () => void;
  onDownload?: () => void;
}) {
  if (deliverable === null) return null; // nothing handed off yet → no panel

  const isApp = deliverable.kind === "app";
  const action = isApp ? onOpen : onDownload;
  const ActionIcon = isApp ? ExternalLink : Download;
  const actionLabel = isApp ? "Open" : "Download";

  return (
    <div className="flex items-center gap-inline rounded-control border border-accent/30 bg-accent/5 px-inline py-hair">
      <PackageCheck className="size-4 shrink-0 text-accent" aria-hidden />
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="truncate font-ui text-[0.86rem] font-medium text-text">
          {deliverable.title}
        </span>
        <span className="truncate font-mono text-[0.72rem] text-text-faint">
          {isApp ? "live app" : "files"} · {deliverable.path}
        </span>
      </div>
      <button
        type="button"
        onClick={action}
        disabled={!action}
        aria-label={`${actionLabel} the deliverable: ${deliverable.title}`}
        title={`${actionLabel} ${deliverable.title}`}
        className="flex shrink-0 items-center gap-hair rounded-control bg-accent px-inline py-hair font-ui text-[0.8rem] font-medium text-surface-0 transition hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
      >
        <ActionIcon className="size-3.5" aria-hidden />
        {actionLabel}
      </button>
    </div>
  );
}
