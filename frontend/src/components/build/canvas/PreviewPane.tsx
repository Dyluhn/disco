import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type MutableRefObject,
} from "react";
import * as Dropdown from "@radix-ui/react-dropdown-menu";
import { useQueryClient } from "@tanstack/react-query";
import {
  Check,
  ChevronDown,
  ExternalLink,
  History,
  MonitorPlay,
  MousePointer2,
  Pencil,
  RotateCw,
  Undo2,
} from "lucide-react";
import { canonicalPreviewBootstrapUrl, agentHttpBase, ApiError, type PreviewLaunch } from "@/api/client";
import { restartPreview, restoreWorkspaceVersion, type WorkspaceVersion } from "@/api/agent";
import { PreviewLaunchFrame } from "@/components/PreviewLaunchFrame";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { EditAffordance } from "@/components/build/canvas/EditAffordance";
import { SelectionOverlay } from "@/components/build/canvas/SelectionOverlay";
import { useToast } from "@/components/toastApi";
import { useBuildPreview } from "@/hooks/useBuildPreview";
import { useElementMention } from "@/hooks/useElementMention";
import { useElementSelect } from "@/hooks/useElementSelect";
import { useWorkspaceVersions } from "@/hooks/useWorkspaceVersions";
import { deriveFiles, deriveSrcDoc } from "@/lib/buildTrace";
import { cn } from "@/lib/cn";
import type { ElementMentionPayload } from "@/lib/elementMention";
import { openFreshPreview } from "@/lib/previewLaunch";
import { formatSelectionContext } from "@/lib/resolvers/appResolver";
import type { SelectionRef } from "@/lib/selectionBridge";
import type { AgentEvent, ConversationStatus } from "@/types/agent";

function formatRelativeTime(ts: string): string {
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

function versionLabel(version: WorkspaceVersion): string {
  const label = version.label?.trim() || version.trigger;
  return `v${version.seq} · ${formatRelativeTime(version.ts)} · ${label}`;
}

function triggerBadgeClass(trigger: string): string {
  if (trigger === "turn") return "border-accent/30 bg-accent/10 text-accent";
  if (trigger === "finish") return "border-supported/30 bg-supported/10 text-supported";
  if (trigger === "restore") return "border-warn/30 bg-warn/10 text-warn";
  return "border-hairline bg-surface-1 text-text-faint";
}

function originOf(url: string | null): string | null {
  if (!url) return null;
  try {
    return new URL(url).origin;
  } catch {
    return null;
  }
}

type OwnedPreviewMint = { key: string; promise: Promise<PreviewLaunch | null> };

function mintOnceForNavigation(
  ref: MutableRefObject<OwnedPreviewMint | null>,
  key: string,
  mint: () => Promise<PreviewLaunch | null>,
): Promise<PreviewLaunch | null> {
  if (ref.current?.key === key) return ref.current.promise;
  const promise = mint();
  ref.current = { key, promise };
  return promise;
}

function latestAppDeliverable(events: AgentEvent[]) {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event.kind === "deliverable" && event.artifact_kind === "app") return event;
  }
  return null;
}

function refreshTarget(path: string, nonce: number): string {
  const safePath = path.startsWith("/") ? path : "/";
  try {
    const url = new URL(safePath, "http://preview.invalid");
    url.searchParams.set("_disco_refresh", String(nonce));
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return `/?_disco_refresh=${nonce}`;
  }
}

function parseErrorReason(message: string): string | null {
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

function restoreErrorCopy(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 409) return "build is running — pause or wait";
    return parseErrorReason(error.message) ?? `restore failed (${error.status})`;
  }
  return error instanceof Error ? error.message : "restore failed";
}

function VersionPicker({
  versions,
  selectedSeq,
  onSelect,
}: {
  versions: WorkspaceVersion[];
  selectedSeq: number | null;
  onSelect: (seq: number | null) => void;
}) {
  if (versions.length === 0) return null;
  const selected = selectedSeq == null ? null : versions.find((version) => version.seq === selectedSeq);
  return (
    <Dropdown.Root>
      <Dropdown.Trigger
        aria-label="Version history"
        className="flex max-w-[18rem] items-center gap-hair rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.74rem] text-text-muted transition-colors hover:border-hairline-strong hover:text-text"
      >
        <History className="size-3.5 shrink-0" aria-hidden />
        <span className="truncate">{selected ? `v${selected.seq}` : "Current"}</span>
        <ChevronDown className="size-3 shrink-0 opacity-60" aria-hidden />
      </Dropdown.Trigger>
      <Dropdown.Portal>
        <Dropdown.Content
          align="end"
          sideOffset={6}
          className="z-50 min-w-[18rem] max-w-[min(28rem,92vw)] rounded-card border border-hairline bg-bg p-px shadow-none"
        >
          <Dropdown.Item
            onSelect={() => onSelect(null)}
            className="flex cursor-pointer items-center gap-inline rounded-control px-inline py-hair font-ui text-[0.78rem] text-text outline-none data-[highlighted]:bg-surface-2"
          >
            <Check className={cn("size-3.5 shrink-0", selectedSeq === null ? "text-accent" : "opacity-0")} aria-hidden />
            <span className="min-w-0 flex-1 truncate">Current</span>
          </Dropdown.Item>
          <Dropdown.Separator className="my-px h-px bg-hairline" />
          {versions.map((version) => (
            <Dropdown.Item
              key={version.seq}
              onSelect={() => onSelect(version.seq)}
              className="flex cursor-pointer items-center gap-inline rounded-control px-inline py-hair font-ui text-[0.78rem] text-text outline-none data-[highlighted]:bg-surface-2"
            >
              <Check className={cn("size-3.5 shrink-0", selectedSeq === version.seq ? "text-accent" : "opacity-0")} aria-hidden />
              <span className="min-w-0 flex-1 truncate">{versionLabel(version)}</span>
              <span className={cn("shrink-0 rounded-full border px-hair py-px font-ui text-[0.62rem] uppercase tracking-wide", triggerBadgeClass(version.trigger))}>
                {version.trigger}
              </span>
            </Dropdown.Item>
          ))}
        </Dropdown.Content>
      </Dropdown.Portal>
    </Dropdown.Root>
  );
}

function PointButton({
  armed,
  onArm,
  onDisarm,
}: {
  armed: boolean;
  onArm: () => void;
  onDisarm: () => void;
}) {
  return (
    <button
      type="button"
      onClick={armed ? onDisarm : onArm}
      aria-pressed={armed}
      aria-label={armed ? "Cancel element mention" : "Point at element"}
      data-disco-control="build.element-mention"
      className={cn(
        "flex items-center gap-hair font-ui text-[0.74rem] transition-colors",
        armed ? "text-accent" : "text-text-muted hover:text-text",
      )}
    >
      <MousePointer2 className="size-3" aria-hidden />
      {armed ? "Cancel" : "Point"}
    </button>
  );
}

/**
 * One canonical Build Preview. Owned web builds always use the platform-managed
 * capability proxy; event-derived srcdoc is reserved for the hardened imported /
 * artifact viewer and is never presented as proof that an owned application runs.
 */
export function PreviewPane({
  status,
  cid,
  events,
  untrusted = false,
  onSteer,
  onSelectionEdit,
  onElementMention,
}: {
  status: ConversationStatus;
  cid: string | null;
  events: AgentEvent[];
  untrusted?: boolean;
  onSteer?: (text: string) => void;
  onSelectionEdit?: (ref: SelectionRef, instruction: string, humanLabel?: string) => void;
  onElementMention?: (payload: ElementMentionPayload) => void;
}) {
  const active =
    status === "RUNNING" ||
    status === "WAITING_FOR_CONFIRMATION" ||
    status === "FINISHED" ||
    status === "STUCK";
  const { data } = useBuildPreview(cid, active);
  const queryClient = useQueryClient();
  const toast = useToast();
  const { versions, refetch: refetchVersions } = useWorkspaceVersions(cid, events, status);
  const files = useMemo(() => deriveFiles(events), [events]);
  const appDeliverable = useMemo(() => latestAppDeliverable(events), [events]);
  const selectedAppPath = appDeliverable?.path;
  const artifactSrcDoc = useMemo(
    () => deriveSrcDoc(files, undefined, undefined, selectedAppPath),
    [files, selectedAppPath],
  );
  const inlineHtmlArtifact = useMemo(
    () =>
      files.find(
        (file) =>
          !file.content &&
          /\.html?$/i.test(file.path) &&
          (selectedAppPath == null || file.path === selectedAppPath),
      ) ?? null,
    [files, selectedAppPath],
  );
  const hasWorkspaceVersion = events.some((event) => event.kind === "workspace_version");
  const webSignal = Boolean(appDeliverable || artifactSrcDoc || data?.available);
  const ownsCanonicalPreview = Boolean(cid && !untrusted && webSignal);

  const [selectedVersionSeq, setSelectedVersionSeq] = useState<number | null>(null);
  const [restoringSeq, setRestoringSeq] = useState<number | null>(null);
  const [restoreNotice, setRestoreNotice] = useState<{
    tone: "success" | "error";
    text: string;
  } | null>(null);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [restarting, setRestarting] = useState(false);
  const [launch, setLaunch] = useState<PreviewLaunch | null>(null);
  const [launchKey, setLaunchKey] = useState("");
  const [launchVersionSeq, setLaunchVersionSeq] = useState<number | null>(null);
  const [displayedVersionSeq, setDisplayedVersionSeq] = useState<number | null>(null);
  const [minting, setMinting] = useState(false);
  const [launchFailure, setLaunchFailure] = useState<string | null>(null);
  const [frameReady, setFrameReady] = useState(false);
  const frameRef = useRef<HTMLIFrameElement | null>(null);
  const mintRef = useRef<OwnedPreviewMint | null>(null);
  const currentRouteRef = useRef("/");

  const generation = data?.generation ?? `${data?.port ?? 0}:${data?.status ?? "unknown"}`;
  const selectedTarget =
    selectedVersionSeq === null
      ? refreshTarget(currentRouteRef.current, refreshNonce)
      : refreshTarget("/", refreshNonce);
  const requestedLaunchKey = cid
    ? `${cid}:${generation}:${selectedVersionSeq ?? "current"}:${refreshNonce}`
    : "";

  useEffect(() => {
    let cancelled = false;
    if (!cid || !ownsCanonicalPreview) {
      mintRef.current = null;
      return;
    }
    setMinting(true);
    setLaunchFailure(null);
    void mintOnceForNavigation(mintRef, requestedLaunchKey, () =>
      selectedVersionSeq === null
        ? canonicalPreviewBootstrapUrl(cid, selectedTarget)
        : canonicalPreviewBootstrapUrl(cid, selectedTarget, selectedVersionSeq),
    )
      .then((nextLaunch) => {
        if (cancelled) return;
        if (nextLaunch === null) {
          setLaunchFailure("isolated preview access could not be established");
          return;
        }
        setLaunch(nextLaunch);
        setLaunchKey(requestedLaunchKey);
        setLaunchVersionSeq(selectedVersionSeq);
        setFrameReady(false);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        const reason = error instanceof Error ? parseErrorReason(error.message) : null;
        setLaunchFailure(reason ?? "isolated preview access could not be established");
      })
      .finally(() => {
        if (!cancelled) setMinting(false);
      });
    return () => {
      cancelled = true;
    };
  }, [cid, ownsCanonicalPreview, requestedLaunchKey, selectedTarget, selectedVersionSeq]);

  // The injected host bridge reports the page's current route. Refreshes and
  // static-server file updates then remint that route instead of jumping to `/`.
  useLayoutEffect(() => {
    const allowedOrigin = originOf(launch?.url ?? null);
    function onMessage(event: MessageEvent) {
      if (
        !allowedOrigin ||
        event.origin !== allowedOrigin ||
        event.source !== frameRef.current?.contentWindow ||
        !event.data ||
        typeof event.data !== "object"
      ) {
        return;
      }
      const message = event.data as Record<string, unknown>;
      if (
        message.channel === "disco-preview" &&
        message.type === "location" &&
        typeof message.path === "string" &&
        message.path.startsWith("/") &&
        message.path.length <= 4096
      ) {
        currentRouteRef.current = message.path;
      }
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [launch]);

  // Plain/static runtimes need a frame refresh when the workspace changes. HMR
  // runtimes own their connection and remain mounted so Vite/Next can update
  // naturally without losing component or route state.
  const fileSignature = useMemo(
    () =>
      JSON.stringify(
        files.map((file) => [file.path, file.bytes, file.content]),
      ),
    [files],
  );
  const lastFileSignature = useRef(fileSignature);
  useEffect(() => {
    if (status !== "RUNNING") {
      lastFileSignature.current = fileSignature;
      return;
    }
    if (fileSignature === lastFileSignature.current) return;
    if (data?.reload_strategy === "hmr") {
      lastFileSignature.current = fileSignature;
      return;
    }
    const timer = window.setTimeout(() => {
      lastFileSignature.current = fileSignature;
      setRefreshNonce((value) => value + 1);
    }, 600);
    return () => window.clearTimeout(timer);
  }, [data?.reload_strategy, fileSignature, status]);

  const previewOpenError = (reason: "popup_blocked" | "capability_unavailable") => {
    toast.show({
      title: reason === "popup_blocked" ? "Preview popup blocked" : "Couldn’t open Preview",
      body:
        reason === "popup_blocked"
          ? "Allow popups for this site, then try again."
          : "Isolated Preview access could not be established. Try again.",
    });
  };

  const refresh = useCallback(async () => {
    if (!cid) return;
    setRestarting(true);
    try {
      if (!data?.available || data.status === "crashed" || data.status === "stopped") {
        await restartPreview(cid);
      }
      await queryClient.invalidateQueries({ queryKey: ["build-preview", cid] });
      setRefreshNonce((value) => value + 1);
    } finally {
      setRestarting(false);
    }
  }, [cid, data?.available, data?.status, queryClient]);

  function selectVersion(seq: number | null) {
    setSelectedVersionSeq(seq);
    setRestoreNotice(null);
    currentRouteRef.current = "/";
    setRefreshNonce((value) => value + 1);
  }

  async function rollBackToVersion(seq: number) {
    if (!cid || restoringSeq !== null) return;
    setRestoringSeq(seq);
    setRestoreNotice(null);
    try {
      const result = await restoreWorkspaceVersion(cid, seq);
      await refetchVersions();
      setSelectedVersionSeq(null);
      currentRouteRef.current = "/";
      setRefreshNonce((value) => value + 1);
      const text =
        result.new_version == null
          ? `Rolled back to v${seq}.`
          : `Rolled back to v${seq}; saved as v${result.new_version}.`;
      setRestoreNotice({ tone: "success", text });
      toast.show({
        title: `Rolled back to v${seq}`,
        body: result.new_version == null ? undefined : `Saved as v${result.new_version}.`,
      });
    } catch (error) {
      setRestoreNotice({ tone: "error", text: restoreErrorCopy(error) });
    } finally {
      setRestoringSeq(null);
    }
  }

  const allowedOrigin = originOf(launch?.url ?? null) ?? "null";
  const selection = useElementSelect(frameRef, allowedOrigin);
  const handleElementMention = useCallback(
    (payload: ElementMentionPayload) => onElementMention?.(payload),
    [onElementMention],
  );
  const mention = useElementMention(
    frameRef,
    originOf(launch?.url ?? null),
    handleElementMention,
    Boolean(onElementMention && launch && !untrusted && displayedVersionSeq === null),
  );
  const [editMode, setEditMode] = useState(false);
  const canEdit = Boolean(onSelectionEdit && launch && !untrusted && displayedVersionSeq === null);

  function toggleEdit() {
    if (!canEdit) return;
    if (editMode) {
      selection.disarm();
      setEditMode(false);
    } else {
      mention.disarm();
      setEditMode(true);
      selection.arm();
    }
  }

  function applyEdit(instruction: string) {
    if (!onSelectionEdit || !selection.selection || !instruction.trim()) return;
    onSelectionEdit(
      selection.selection.selection_ref,
      instruction,
      selection.selection.human_label,
    );
    selection.resetSelection();
  }

  const RefreshButton = (
    <button
      type="button"
      onClick={() => void refresh()}
      disabled={restarting || !cid}
      aria-label="Refresh or restart Preview"
      data-disco-control="build.preview-refresh"
      className="flex items-center gap-hair font-ui text-[0.74rem] text-text-muted transition-colors hover:text-text disabled:opacity-50"
    >
      <RotateCw className={cn("size-3", restarting && "animate-spin")} aria-hidden />
      {restarting ? "Restarting…" : "Refresh"}
    </button>
  );
  const VersionControl = (
    <VersionPicker versions={versions} selectedSeq={selectedVersionSeq} onSelect={selectVersion} />
  );
  const RestoreNotice = restoreNotice ? (
    <div
      role={restoreNotice.tone === "error" ? "alert" : "status"}
      className={cn(
        "shrink-0 border-b px-body py-hair font-ui text-[0.72rem]",
        restoreNotice.tone === "error"
          ? "border-unsupported/30 bg-unsupported/10 text-unsupported"
          : "border-supported/30 bg-supported/10 text-supported",
      )}
    >
      {restoreNotice.text}
    </div>
  ) : null;

  // Imported/shared HTML and non-runtime HTML artifacts keep a deliberately
  // hardened viewer. It is visibly not the Build Preview and never mints a
  // runtime capability or supplies verification evidence.
  if (!ownsCanonicalPreview && (artifactSrcDoc !== null || (inlineHtmlArtifact && cid))) {
    const inlineSrc =
      inlineHtmlArtifact && cid
        ? `${agentHttpBase()}/conversations/${encodeURIComponent(cid)}/artifacts/${inlineHtmlArtifact.path}?inline=true`
        : null;
    return (
      <div className="flex h-full min-h-0 flex-col">
        <div className="flex shrink-0 items-center justify-between border-b border-hairline px-body py-hair">
          <span className="font-mono text-[0.74rem] text-text-faint">static artifact viewer</span>
        </div>
        <div className="shrink-0 border-b border-hairline bg-surface-1 px-body py-hair font-ui text-[0.72rem] text-text-faint">
          Hardened artifact view — not a runtime Preview and not application verification.
        </div>
        <iframe
          title="Static artifact viewer"
          {...(inlineSrc ? { src: inlineSrc } : { srcDoc: artifactSrcDoc ?? undefined })}
          sandbox=""
          className="min-h-0 flex-1 border-0 bg-white"
        />
      </div>
    );
  }

  if (!ownsCanonicalPreview) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        <div className="flex shrink-0 items-center justify-between border-b border-hairline px-body py-hair">
          <span className="font-mono text-[0.74rem] text-text-faint">Preview</span>
        </div>
        <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-hair px-body text-center">
          <p className="font-ui text-[0.9rem] text-text-muted">Preparing Preview</p>
          <p className="max-w-measure font-ui text-[0.8rem] text-text-faint">
            {data?.reason ?? "A real Preview appears here when this build starts a web runtime."}
          </p>
        </div>
      </div>
    );
  }

  const committedStatic = status === "FINISHED" && Boolean(appDeliverable && hasWorkspaceVersion);
  const runtimeUnavailable =
    selectedVersionSeq === null && !committedStatic && data != null && !data.available;
  const visibleFailure =
    launchFailure && launchKey !== requestedLaunchKey ? launchFailure : null;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center justify-between gap-inline border-b border-hairline px-body py-hair">
        <span className="truncate font-mono text-[0.74rem] text-text-faint">Preview</span>
        <div className="flex items-center gap-inline">
          {VersionControl}
          {RefreshButton}
          {onElementMention && displayedVersionSeq === null && (
            <PointButton
              armed={mention.armed}
              onArm={() => {
                if (editMode) toggleEdit();
                mention.arm();
              }}
              onDisarm={mention.disarm}
            />
          )}
          {canEdit && (
            <button
              type="button"
              onClick={toggleEdit}
              aria-pressed={editMode}
              aria-label="Toggle click-to-edit mode on Preview"
              data-disco-control="build.edit-toggle"
              className={cn(
                "flex items-center gap-hair font-ui text-[0.74rem] transition-colors",
                editMode ? "text-accent" : "text-text-muted hover:text-text",
              )}
            >
              <Pencil className="size-3" aria-hidden /> Edit
            </button>
          )}
          {cid && (
            <button
              type="button"
              onClick={() =>
                openFreshPreview(
                  () =>
                    displayedVersionSeq === null
                      ? canonicalPreviewBootstrapUrl(cid, currentRouteRef.current)
                      : canonicalPreviewBootstrapUrl(cid, "/", displayedVersionSeq),
                  previewOpenError,
                )
              }
              aria-label="Open Preview in new tab"
              data-disco-control="build.preview-open"
              className="flex items-center gap-hair font-ui text-[0.74rem] text-text-muted transition-colors hover:text-text"
            >
              <ExternalLink className="size-3" aria-hidden /> Open
            </button>
          )}
        </div>
      </div>
      {RestoreNotice}
      {displayedVersionSeq !== null && (
        <div className="flex shrink-0 items-center justify-between gap-inline border-b border-warn/30 bg-warn/10 px-body py-hair">
          <span className="font-ui text-[0.78rem] text-warn">
            Viewing immutable v{displayedVersionSeq} (read-only)
          </span>
          <div className="flex items-center gap-inline">
            <button
              type="button"
              onClick={() => selectVersion(null)}
              className="flex items-center gap-hair font-ui text-[0.74rem] text-text-muted hover:text-text"
            >
              <MonitorPlay className="size-3" aria-hidden /> Back to current
            </button>
            <ConfirmDialog
              title={`Roll back to v${displayedVersionSeq}?`}
              description="Restores the workspace to this exact version. The rollback is saved as a new version."
              confirmLabel="Roll back"
              confirmContext="workspace-rollback"
              onConfirm={() => void rollBackToVersion(displayedVersionSeq)}
              trigger={
                <button
                  type="button"
                  disabled={restoringSeq !== null}
                  className="flex items-center gap-hair rounded-control border border-warn/40 px-inline py-hair font-ui text-[0.74rem] text-warn disabled:opacity-50"
                >
                  <Undo2 className={cn("size-3", restoringSeq !== null && "animate-spin")} aria-hidden />
                  {restoringSeq !== null ? "Rolling back…" : "Roll back"}
                </button>
              }
            />
          </div>
        </div>
      )}
      {editMode && (
        <div className="shrink-0 border-b border-hairline bg-surface-1 px-body py-hair font-ui text-[0.72rem] text-text-faint">
          Edit mode: click the real running page, then describe the targeted change.
        </div>
      )}
      <div className="relative min-h-0 flex-1 bg-white">
        {launch ? (
          <PreviewLaunchFrame
            ref={frameRef}
            title="Preview"
            launch={launch}
            onLoad={() => {
              setDisplayedVersionSeq(launchVersionSeq);
              setFrameReady(true);
            }}
            sandbox="allow-scripts allow-forms allow-same-origin allow-popups allow-downloads"
            className="h-full w-full border-0 bg-white"
          />
        ) : (
          <div
            role={visibleFailure ? "alert" : "status"}
            className="flex h-full flex-col items-center justify-center gap-hair px-body text-center font-ui"
          >
            <p className="text-[0.9rem] text-text-muted">
              {visibleFailure ? "Preview unavailable" : "Preparing Preview"}
            </p>
            <p className="max-w-measure text-[0.8rem] text-text-faint">
              {visibleFailure ?? data?.reason ?? "Starting the platform-managed application runtime…"}
            </p>
          </div>
        )}
        {launch && (minting || runtimeUnavailable || visibleFailure || !frameReady) && (
          <div
            role={visibleFailure || runtimeUnavailable ? "alert" : "status"}
            className="pointer-events-none absolute inset-x-body top-body z-20 rounded-control border border-hairline bg-bg/95 px-body py-hair font-ui text-[0.76rem] text-text-muted shadow-sm backdrop-blur"
          >
            {visibleFailure
              ? `Preview update unavailable: ${visibleFailure}`
              : runtimeUnavailable
                ? `Preview runtime is recovering: ${data?.reason ?? "server unavailable"}`
                : "Preparing Preview update…"}
          </div>
        )}
        <SelectionOverlay
          armed={editMode && selection.armed}
          untrusted={untrusted}
          selection={selection.selection}
          onArm={selection.arm}
          onDisarm={() => {
            selection.disarm();
            setEditMode(false);
          }}
          onWalkUp={selection.walkUp}
          onDiscuss={
            onSteer
              ? (selected) => {
                  onSteer(formatSelectionContext(selected));
                  selection.resetSelection();
                  selection.disarm();
                  setEditMode(false);
                }
              : undefined
          }
        />
        {editMode && selection.selection !== null && (
          <EditAffordance
            targetLabel={selection.selection.human_label}
            rect={selection.selection.rect}
            onApply={applyEdit}
            onCancel={selection.resetSelection}
          />
        )}
      </div>
    </div>
  );
}
