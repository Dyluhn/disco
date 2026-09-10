/**
 * The Agent Canvas (Build inspector) data-access layer.
 *
 * Amendment A3: components under `components/build/agentCanvas/` may not import
 * `@/api/client` directly — only `AgentCanvas.tsx` itself sits on the eslint
 * grandfather allowlist, and that entry is scheduled to be removed at Epic 12
 * close. This module is the one place the Agent surface crosses that seam for
 * its live-browser (noVNC) session calls and its artifact/workspace URLs.
 */

import {
  agentGet,
  agentSend,
  agentHttpBase,
  previewBootstrapUrl,
  pathPreviewBootstrapUrl,
  type PreviewLaunch,
} from "@/api/client";

export type { PreviewLaunch };

export interface LiveBrowserReadiness {
  ready: boolean;
  reason: string;
}

export interface LiveBrowserStartResult {
  ready: boolean;
  novnc_path: string;
  port: number;
  reason?: string;
}

/** The side-effect-free /browser/live-ready probe: true only when this backend
 * can actually run + stream the noVNC stack AND a sandbox + healthy browser
 * daemon are up. Drives both auto-start and auto-stop in useLiveBrowserSession. */
export async function getLiveBrowserReadiness(cid: string): Promise<LiveBrowserReadiness> {
  return agentGet<LiveBrowserReadiness>(
    `/conversations/${encodeURIComponent(cid)}/browser/live-ready`,
  );
}

/** Ask the sandbox to start (or report the status of) the noVNC live-browser
 * stack for this conversation. */
export async function startLiveBrowserSession(cid: string): Promise<LiveBrowserStartResult> {
  return agentSend<LiveBrowserStartResult>(
    "POST",
    `/conversations/${encodeURIComponent(cid)}/browser/live-url`,
  );
}

/** Tell the sandbox to tear the live-view stack down. Best-effort +
 * fire-and-forget: teardown should never block on (or error from) the call. */
export function stopLiveBrowserSession(cid: string): void {
  void agentSend("POST", `/conversations/${encodeURIComponent(cid)}/browser/live-stop`).catch(
    () => {},
  );
}

/** Heartbeat while a live view is open, so an ACTIVELY-watched session's idle
 * watchdog never reaps it out from under the user. Fire-and-forget. */
export function touchLiveBrowserSession(cid: string): void {
  void agentSend("POST", `/conversations/${encodeURIComponent(cid)}/browser/live-touch`).catch(
    () => {},
  );
}

/** SECURITY: redeem a server-minted noVNC launch at the isolated preview origin
 * (localhost by default, or the operator's separate wildcard site). The server
 * intentionally never returns a raw sandbox host:port — that would bypass the
 * auth/cid-scoping proxy. */
export async function mintLiveBrowserLaunch(
  cid: string,
  port: number,
  novncPath: string,
): Promise<PreviewLaunch | null> {
  return previewBootstrapUrl(cid, port, novncPath);
}

/** Capability-gated preview-app URL for a committed static artifact (the
 * "path" transport) — used for the Artifacts pane's app deliverable preview. */
export async function mintPathPreviewLaunch(
  cid: string,
  targetPath: string,
): Promise<PreviewLaunch | null> {
  return pathPreviewBootstrapUrl(cid, targetPath);
}

/** The URL the browser loads directly for a captured workspace screenshot. */
export function workspaceFileUrl(cid: string, path: string): string {
  return `${agentHttpBase()}/conversations/${cid}/workspace/${path}`;
}

/** The declared-artifact download URL (encodeURI preserves any subdir slashes). */
export function artifactDownloadUrl(cid: string, path: string): string {
  return `${agentHttpBase()}/conversations/${cid}/artifacts/${encodeURI(path)}`;
}
