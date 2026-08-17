/**
 * Workspace-file and declared-artifact URL builders for the Build Activity Feed
 * (Amendment A3). `ActivityFeed.tsx` built these URLs directly off
 * `agentHttpBase()` (a `@/api/client` transport primitive); that put the ONE
 * transport module in the import graph of the view layer for what is really
 * just a string-building concern. Declared here instead, byte-identical to the
 * URLs the component used to build inline.
 */

import { agentHttpBase } from "@/api/client";

/** A workspace file under a conversation's sandbox (e.g. a BP-15 screenshot).
 *  `path` is interpolated as-is — callers that need escaping do it themselves,
 *  matching the inline URL this replaces. */
export function workspaceFileUrl(conversationId: string, path: string): string {
  return `${agentHttpBase()}/conversations/${conversationId}/workspace/${path}`;
}

/** A declared conversation artifact (the `/artifacts/{filename}` route).
 *  `encodeURI` (not `encodeURIComponent`) preserves any subdir slashes in
 *  `filename`, matching the inline URLs this replaces exactly. */
export function artifactUrl(conversationId: string, filename: string): string {
  return `${agentHttpBase()}/conversations/${conversationId}/artifacts/${encodeURI(filename)}`;
}
