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
 *
 * F1: for `kind === "files"`, the primary Download button now links directly to
 * the per-file artifact route (`/conversations/{cid}/artifacts/{path}`) — not the
 * whole-project ZIP. For directories (no file extension), it falls back to the
 * `onDownload` callback (whole-project zip). `cid` is required for the per-file route.
 */

import { Download, ExternalLink, FileJson, Globe, PackageCheck } from "lucide-react";
import { agentHttpBase } from "@/api/client";
import type { DeliverableView } from "@/lib/buildTrace";

export function DeliverablePanel({
  deliverable,
  cid,
  onOpen,
  onDownload,
  onExportManifest,
}: {
  deliverable: DeliverableView | null;
  /** Conversation id — required for the per-file artifact download route. */
  cid?: string | null;
  onOpen?: () => void;
  onDownload?: () => void;
  /** Export the project manifest JSON (files + deliverable metadata). */
  onExportManifest?: () => void;
}) {
  if (deliverable === null) return null; // nothing handed off yet → no panel

  const isApp = deliverable.kind === "app";
  const deployUrl = deliverable.deploymentUrl;

  // F1: Detect directory vs file. A directory has no file extension (no '.' in the name).
  // Files get the per-file artifact route; directories fall back to onDownload (zip).
  const hasExtension = deliverable.path.includes(".");
  const canDirectDownload = cid && hasExtension && !isApp;

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
      {/* Manifest export — a real, wired secondary action (the JSON description). */}
      {onExportManifest && (
        <button
          type="button"
          onClick={onExportManifest}
          aria-label="Export the project manifest (JSON)"
          title="Export manifest (files + deliverable metadata)"
          data-disco-control="export-manifest"
          className="flex shrink-0 items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:border-accent hover:text-text"
        >
          <FileJson className="size-3.5" aria-hidden />
          Manifest
        </button>
      )}
      {/* When the agent served to a canonical URL, surface it as a real link. */}
      {deployUrl && (
        <a
          href={deployUrl}
          target="_blank"
          rel="noopener noreferrer"
          aria-label={`Open the deployed app at ${deployUrl}`}
          title={deployUrl}
          className="flex shrink-0 items-center gap-hair rounded-control border border-accent/40 px-inline py-hair font-ui text-[0.78rem] text-accent transition-colors hover:bg-accent/10"
        >
          <Globe className="size-3.5" aria-hidden />
          Deployed
        </a>
      )}
      {/* F1: For files (not app), use the per-file artifact route when we have a cid
          and it's a file (not a directory). Otherwise fall back to onDownload (zip). */}
      {canDirectDownload ? (
        <a
          href={`${agentHttpBase()}/conversations/${cid}/artifacts/${encodeURI(deliverable.path)}`}
          download
          aria-label={`Download the deliverable: ${deliverable.title}`}
          title={`Download ${deliverable.title}`}
          data-disco-control="download-artifact"
          className="flex shrink-0 items-center gap-hair rounded-control bg-accent px-inline py-hair font-ui text-[0.8rem] font-medium text-surface-0 transition hover:bg-accent/90"
        >
          <Download className="size-3.5" aria-hidden />
          Download
        </a>
      ) : (
        <button
          type="button"
          onClick={isApp ? onOpen : onDownload}
          disabled={isApp ? !onOpen : !onDownload}
          aria-label={`${isApp ? "Open" : "Download"} the deliverable: ${deliverable.title}`}
          title={`${isApp ? "Open" : "Download"} ${deliverable.title}`}
          data-disco-control={isApp ? "open-app" : "download-artifact"}
          className="flex shrink-0 items-center gap-hair rounded-control bg-accent px-inline py-hair font-ui text-[0.8rem] font-medium text-surface-0 transition hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {isApp ? <ExternalLink className="size-3.5" aria-hidden /> : <Download className="size-3.5" aria-hidden />}
          {isApp ? "Open" : "Download"}
        </button>
      )}
    </div>
  );
}
