/**
 * Deliverable-handoff download URLs (Amendment A3). `DeliverablePanel` no
 * longer imports the transport seam (`@/api/client`) directly — it calls these
 * named URL builders instead. Pure string construction, no fetch; kept as
 * thin wrappers around `agentHttpBase()` matching the exact hrefs the panel
 * built inline before this split.
 */
import { agentHttpBase } from "@/api/client";

/** The whole-project source zip route (no per-file selection) — the "Download
 * source" affordance on a served `app` deliverable. */
export function projectSourceDownloadUrl(cid: string): string {
  return `${agentHttpBase()}/api/projects/${cid}/download`;
}

/** The per-file artifact download route for a single deliverable path — used
 * when the deliverable is a concrete file (not a directory) and a cid is
 * known. `path` is URI-encoded; `cid` is not (matches prior inline behavior). */
export function deliverableArtifactUrl(cid: string | null | undefined, path: string): string {
  return `${agentHttpBase()}/conversations/${cid}/artifacts/${encodeURI(path)}`;
}
