import type { MutableRefObject } from "react";
import type { WorkspaceVersion } from "@/api/agent";
import type { PreviewLaunch } from "@/api/preview";
import type { AgentEvent, ConversationStatus, PreviewInfo } from "@/types/agent";
import type { WorkspaceFile } from "@/lib/buildTrace";

export function formatRelativeTime(ts: string): string {
  const time = new Date(ts).getTime();
  if (!Number.isFinite(time)) return "unknown time";
  const diffSeconds = Math.round((Date.now() - time) / 1000);
  const future = diffSeconds < 0;
  const absolute = Math.abs(diffSeconds);
  const units: Array<[number, string]> = [
    [60 * 60 * 24 * 30, "month"],
    [60 * 60 * 24, "day"],
    [60 * 60, "hour"],
    [60, "minute"],
  ];
  if (absolute < 45) return "just now";
  for (const [seconds, unit] of units) {
    if (absolute >= seconds) {
      const count = Math.max(1, Math.round(absolute / seconds));
      return future
        ? `in ${count} ${unit}${count === 1 ? "" : "s"}`
        : `${count} ${unit}${count === 1 ? "" : "s"} ago`;
    }
  }
  return future ? "in 1 minute" : "1 minute ago";
}

export function versionLabel(version: WorkspaceVersion): string {
  const label = version.label?.trim() || version.trigger;
  return `v${version.seq} · ${formatRelativeTime(version.ts)} · ${label}`;
}

export function triggerBadgeClass(trigger: string): string {
  if (trigger === "turn") return "border-accent/30 bg-accent/10 text-accent";
  if (trigger === "finish") return "border-supported/30 bg-supported/10 text-supported";
  if (trigger === "restore") return "border-warn/30 bg-warn/10 text-warn";
  return "border-hairline bg-surface-1 text-text-faint";
}

export function originOf(url: string | null): string | null {
  if (!url) return null;
  try {
    return new URL(url).origin;
  } catch {
    return null;
  }
}

export type OwnedPreviewMint = { key: string; promise: Promise<PreviewLaunch | null> };

export function mintOnceForNavigation(
  ref: MutableRefObject<OwnedPreviewMint | null>,
  key: string,
  mint: () => Promise<PreviewLaunch | null>,
): Promise<PreviewLaunch | null> {
  if (ref.current?.key === key) return ref.current.promise;
  const promise = mint();
  ref.current = { key, promise };
  return promise;
}

export function latestAppDeliverable(events: AgentEvent[]) {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event.kind === "deliverable" && event.artifact_kind === "app") return event;
  }
  return null;
}

export function refreshTarget(path: string, nonce: number): string {
  const safePath = path.startsWith("/") ? path : "/";
  try {
    const url = new URL(safePath, "http://preview.invalid");
    url.searchParams.set("_disco_refresh", String(nonce));
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return `/?_disco_refresh=${nonce}`;
  }
}

/**
 * Derived-state helpers below. Each owns exactly one of PreviewPane's small
 * boolean/string decisions so the coordinator's own callable stays a handful
 * of zero-branch call expressions instead of a wall of `&&`/`?:`/`??` chains —
 * that's what actually moves cyclomatic weight off `PreviewPane` (relocating
 * the WHOLE expression into one function wouldn't; giving each decision its
 * own named function does).
 */

export function previewActiveStatus(status: ConversationStatus): boolean {
  return status === "RUNNING" || status === "WAITING_FOR_CONFIRMATION";
}

export function previewWebSignal(
  appDeliverable: AgentEvent | null,
  artifactSrcDoc: string | null,
  dataAvailable: boolean | undefined,
): boolean {
  return Boolean(appDeliverable || artifactSrcDoc || dataAvailable);
}

export function previewOwnsCanonicalPreview(
  cid: string | null,
  untrusted: boolean,
  webSignal: boolean,
): boolean {
  return Boolean(cid && !untrusted && webSignal);
}

export function previewHasFinishedCommittedApp(
  status: ConversationStatus,
  appDeliverable: AgentEvent | null,
  hasWorkspaceVersion: boolean,
): boolean {
  return status === "FINISHED" && Boolean(appDeliverable && hasWorkspaceVersion);
}

export function previewGeneration(data: PreviewInfo | undefined): string {
  return data?.generation ?? `${data?.port ?? 0}:${data?.status ?? "unknown"}`;
}

export function computeSelectedTarget(
  selectedVersionSeq: number | null,
  currentRoute: string,
  refreshNonce: number,
): string {
  return selectedVersionSeq === null
    ? refreshTarget(currentRoute, refreshNonce)
    : refreshTarget("/", refreshNonce);
}

/** Identity of the run's committed workspace, or "" while none is sealed yet.
 *
 * A finished run becomes previewable only once its FINAL SEAL is durable, and
 * that event lands one to four seconds AFTER the run reports FINISHED. A
 * capability minted inside that window is correctly refused
 * (`preview_unavailable`) — but nothing else changes the launch key afterwards,
 * so the pane latched on that refusal until someone pressed Refresh. Keying the
 * launch on the seal makes its arrival re-mint on its own. Only the sealed
 * version counts: per-turn version markers must not churn the live preview. */
export function committedWorkspaceKey(events: AgentEvent[]): string {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event.kind === "workspace_version" && event.final_seal) {
      return `sealed:${event.version_seq}`;
    }
  }
  return "";
}

export function computeRequestedLaunchKey(
  cid: string | null,
  generation: string,
  selectedVersionSeq: number | null,
  refreshNonce: number,
  committedKey: string,
): string {
  return cid
    ? `${cid}:${generation}:${selectedVersionSeq ?? "current"}:${refreshNonce}:${committedKey}`
    : "";
}

/** The preview iframe's origin, or null when no launch is minted yet. Distinct
 * from `computeAllowedOrigin` (used by `useElementSelect`, which wants the
 * literal string `"null"` rather than the value `null` as its no-origin case). */
export function previewOrigin(launch: PreviewLaunch | null): string | null {
  return originOf(launch?.url ?? null);
}

export function computeRuntimeUnavailable(
  selectedVersionSeq: number | null,
  committedStatic: boolean,
  data: PreviewInfo | undefined,
): boolean {
  return selectedVersionSeq === null && !committedStatic && data != null && !data.available;
}

/** The failure the pane should actually SAY something about.
 *
 * `launchFailure` only counts while the minted launch is stale (`launchKey !==
 * requestedLaunchKey`) — a failure attached to the launch already on screen is
 * old news the user has moved past.
 *
 * `bootstrapFailure` (UI-44) short-circuits that test, because it belongs to
 * the CURRENT launch by construction: the mint succeeded, so the two keys are
 * equal, and routing it through `launchFailure` would silently show nothing. */
export function computeVisibleFailure(
  launchFailure: string | null,
  launchKey: string,
  requestedLaunchKey: string,
  bootstrapFailure: string | null = null,
): string | null {
  if (bootstrapFailure) return bootstrapFailure;
  return launchFailure && launchKey !== requestedLaunchKey ? launchFailure : null;
}

export function computeMentionEnabled(
  hasOnElementMention: boolean,
  launch: PreviewLaunch | null,
  untrusted: boolean,
  displayedVersionSeq: number | null,
): boolean {
  return Boolean(hasOnElementMention && launch && !untrusted && displayedVersionSeq === null);
}

export function computeShowPointButton(
  hasOnElementMention: boolean,
  displayedVersionSeq: number | null,
): boolean {
  return Boolean(hasOnElementMention && displayedVersionSeq === null);
}

export function computeCanEdit(
  hasOnSelectionEdit: boolean,
  launch: PreviewLaunch | null,
  untrusted: boolean,
  displayedVersionSeq: number | null,
): boolean {
  return Boolean(hasOnSelectionEdit && launch && !untrusted && displayedVersionSeq === null);
}

export function shouldShowStaticArtifactViewer(
  ownsPreview: boolean,
  artifactSrcDoc: string | null,
  inlineHtmlArtifact: WorkspaceFile | null,
  cid: string | null,
): boolean {
  return !ownsPreview && (artifactSrcDoc !== null || Boolean(inlineHtmlArtifact && cid));
}

export function shouldShowUpdateErrorBanner(
  selectedVersionSeq: number | null,
  updateError: string | null | undefined,
): boolean {
  return selectedVersionSeq === null && Boolean(updateError);
}

/** PreviewStage's own small decisions, split out the same way — one function
 * per branch-bearing question, so the JSX stays a flat list of calls. */

export function shouldShowStatusOverlay(
  launch: PreviewLaunch | null,
  minting: boolean,
  runtimeUnavailable: boolean,
  visibleFailure: string | null,
  frameReady: boolean,
): boolean {
  return Boolean(launch) && (minting || runtimeUnavailable || Boolean(visibleFailure) || !frameReady);
}

/** Whether the preview iframe is still worth showing.
 *
 * A frame that NEVER loaded (`frameReady` false) while a failure is visible is
 * displaying whatever the proxy answered with — a raw nginx 403 or an expired
 * origin page — so the plain-language failure card replaces it (UI-44).
 *
 * `frameReady` is the whole distinction. A previously healthy frame keeps it
 * true through a later failed update, and THAT frame deliberately stays on
 * screen behind the "showing the last healthy frame" banner. */
export function shouldRenderPreviewFrame(
  launch: PreviewLaunch | null,
  visibleFailure: string | null,
  frameReady: boolean,
): boolean {
  return Boolean(launch) && (frameReady || !visibleFailure);
}

export function statusOverlayRole(
  visibleFailure: string | null,
  runtimeUnavailable: boolean,
): "alert" | "status" {
  return visibleFailure || runtimeUnavailable ? "alert" : "status";
}

/** True for a bare backend reason code (`preview_unavailable`) as opposed to a
 * message already written for a person ("capability unavailable"). Codes are the
 * only thing that needs translating, and the only thing worth repeating as a
 * secondary line. */
export function isPreviewReasonCode(reason: string): boolean {
  return /^[a-z][a-z0-9_]*$/.test(reason);
}

/** Plain-words copy for a backend preview reason code: what happened, and what
 * the operator can do about it. The raw code is never the whole message — the
 * surfaces below render it as a separate secondary line so it stays greppable
 * without being the only thing a user is told. An unrecognized code still gets
 * an honest sentence rather than being presented as progress, and a message that
 * was already human passes through unchanged. */
export function previewFailureExplanation(reason: string): string {
  if (!isPreviewReasonCode(reason)) return reason;
  switch (reason) {
    case "preview_unavailable":
      return "Preview is not available for this run: its app server is not running and the finished build has no committed app to serve. Use Download source, or ask the agent to serve the site again.";
    case "preview_authority_unavailable":
      return "Preview could not be opened because the platform could not establish an isolated preview origin. Try again in a moment.";
    case "local_preview_origin_pool_exhausted":
      return "Every isolated preview slot is in use. Close another preview tab, then try again.";
    case "preview_bootstrap_timeout":
      return "The preview server never answered, so nothing was loaded — whatever the frame was showing came from the proxy, not from your app. Press Refresh to try again, or use Download source to open the project on your own machine.";
    case "invalid_preview_path":
    case "reserved_preview_path":
    case "preview_target_too_long":
      return "Preview could not open that address. Reopen Preview from the toolbar to go back to the site root.";
    default:
      return "Preview could not be opened for this run. Try Refresh, or ask the agent to serve the site again.";
  }
}

export function statusOverlayMessage(
  visibleFailure: string | null,
  runtimeUnavailable: boolean,
  dataReason: string | undefined,
): string {
  if (visibleFailure) {
    return `Preview update unavailable: ${previewFailureExplanation(visibleFailure)}`;
  }
  if (runtimeUnavailable) {
    return `Preview runtime is recovering: ${dataReason ?? "server unavailable"}`;
  }
  return "Preparing Preview update…";
}

export function previewStagePlaceholderMessage(
  visibleFailure: string | null,
  dataReason: string | undefined,
): string {
  if (visibleFailure) return previewFailureExplanation(visibleFailure);
  return dataReason ?? "Starting the platform-managed application runtime…";
}
