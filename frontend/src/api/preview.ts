/**
 * Preview data-access seam for the Build canvas (Amendment A3). Components under
 * `components/` must not import `@/api/client` directly — this module wraps the
 * canonical preview-bootstrap + artifact-inline calls and the restore-failure
 * copy so `PreviewPane` and its parts only ever reach into `@/api/preview` and
 * `@/api/agent`.
 */

import {
  agentHttpBase,
  canonicalPreviewBootstrapUrl,
  ApiError,
  previewHostUrl as clientPreviewHostUrl,
  type PreviewLaunch,
} from "@/api/client";

/** Mints (or remints) the canonical Build Preview capability. `versionSeq` is
 * passed through to `canonicalPreviewBootstrapUrl` only when non-null, so the
 * two existing call shapes (current vs. an exact immutable version) are
 * preserved exactly — callers/tests keying on argument count still match. */
export async function previewBootstrapUrl(
  cid: string,
  target: string,
  versionSeq?: number | null,
): Promise<PreviewLaunch | null> {
  return versionSeq == null
    ? canonicalPreviewBootstrapUrl(cid, target)
    : canonicalPreviewBootstrapUrl(cid, target, versionSeq);
}

/** The inline-view URL for a static (non-runtime) HTML artifact. */
export function artifactInlineUrl(cid: string, path: string): string {
  return `${agentHttpBase()}/conversations/${encodeURIComponent(cid)}/artifacts/${path}?inline=true`;
}

/** Pull the operator-facing reason out of a backend error body: a JSON `detail`
 * string, a JSON `{ detail: { reason } }` shape, or the raw trimmed message for
 * plain-text server errors. */
export function previewErrorReason(message: string): string | null {
  try {
    const body = JSON.parse(message) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
    if (
      body.detail &&
      typeof body.detail === "object" &&
      "reason" in body.detail &&
      typeof body.detail.reason === "string"
    ) {
      return body.detail.reason;
    }
  } catch {
    // Plain-text server errors are already suitable for the status surface.
  }
  return message.trim() || null;
}

/** User-facing copy for a failed workspace-version restore. */
export function restoreFailureCopy(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 409) return "build is running — pause or wait";
    return previewErrorReason(error.message) ?? `restore failed (${error.status})`;
  }
  return error instanceof Error ? error.message : "restore failed";
}

export type { PreviewLaunch };

/** The legacy host-transport URL builder (`api/client.ts`'s `previewHostUrl`) —
 * still live production code (the offline fallback inside `previewBootstrapUrl`
 * above), and directly contract-tested by
 * `ExecutionCanvas.preview.test.tsx`'s "previewHostUrl legacy helper" suite.
 * Declared (not re-exported) so that spec can drop its `@/api/client` import
 * per Amendment A3 while still exercising the real implementation — `base`
 * forwards only when explicitly supplied so the default-base call shape stays
 * intact for any caller that omits it. */
export function previewHostUrl(cid: string, port: number, base?: string): string | null {
  return base === undefined
    ? clientPreviewHostUrl(cid, port)
    : clientPreviewHostUrl(cid, port, base);
}
