import { useCallback, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { previewBootstrapUrl } from "@/api/preview";
import { restartPreview } from "@/api/agent";
import { useToast } from "@/components/toastApi";
import { useBuildPreview } from "@/hooks/useBuildPreview";
import { useDownloadProject } from "@/hooks/useProjects";
import { useElementMention } from "@/hooks/useElementMention";
import { useElementSelect } from "@/hooks/useElementSelect";
import { useWorkspaceVersions } from "@/hooks/useWorkspaceVersions";
import { deriveFiles, deriveSrcDoc } from "@/lib/buildTrace";
import { cn } from "@/lib/cn";
import type { ElementMentionPayload } from "@/lib/elementMention";
import { openFreshPreview } from "@/lib/previewLaunch";
import type { SelectionRef } from "@/lib/selectionBridge";
import type { AgentEvent, ConversationStatus } from "@/types/agent";
import {
  committedWorkspaceKey,
  computeCanEdit,
  computeMentionEnabled,
  computeRequestedLaunchKey,
  computeRuntimeUnavailable,
  computeSelectedTarget,
  computeShowPointButton,
  computeVisibleFailure,
  latestAppDeliverable,
  previewGeneration,
  previewHasFinishedCommittedApp,
  previewOrigin,
  previewOwnsCanonicalPreview,
  previewWebSignal,
  previewActiveStatus,
  shouldShowStaticArtifactViewer,
  shouldShowUpdateErrorBanner,
} from "./previewPaneParts/helpers";
import { PreviewStage } from "./previewPaneParts/PreviewStage";
import { PreviewToolbar } from "./previewPaneParts/PreviewToolbar";
import { PreviewUnavailable } from "./previewPaneParts/PreviewUnavailable";
import { StaticArtifactViewer } from "./previewPaneParts/StaticArtifactViewer";
import { usePreviewAutoRefresh } from "./previewPaneParts/usePreviewAutoRefresh";
import { usePreviewLaunch } from "./previewPaneParts/usePreviewLaunch";
import { usePreviewRouteBridge } from "./previewPaneParts/usePreviewRouteBridge";
import { useWorkspaceRollback } from "./previewPaneParts/useWorkspaceRollback";
import { VersionBanner } from "./previewPaneParts/VersionBanner";

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
  downloadTitle = null,
}: {
  status: ConversationStatus;
  cid: string | null;
  events: AgentEvent[];
  untrusted?: boolean;
  /** Names the SAVED FILE for the failure card's Download source action, the
   * same way the header's download does (UI-40). Presentation only. */
  downloadTitle?: string | null;
  onSteer?: (text: string) => void;
  onSelectionEdit?: (ref: SelectionRef, instruction: string, humanLabel?: string) => void;
  onElementMention?: (payload: ElementMentionPayload) => void;
}) {
  const active = previewActiveStatus(status);
  const { data } = useBuildPreview(cid, active);
  const queryClient = useQueryClient();
  const toast = useToast();
  const download = useDownloadProject();
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
  const hasFinishedCommittedApp = previewHasFinishedCommittedApp(status, appDeliverable, hasWorkspaceVersion);
  const webSignal = previewWebSignal(appDeliverable, artifactSrcDoc, data?.available);
  const ownsCanonicalPreview = previewOwnsCanonicalPreview(cid, untrusted, webSignal);

  const [selectedVersionSeq, setSelectedVersionSeq] = useState<number | null>(null);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [restarting, setRestarting] = useState(false);
  const [displayedVersionSeq, setDisplayedVersionSeq] = useState<number | null>(null);
  const frameRef = useRef<HTMLIFrameElement | null>(null);
  const currentRouteRef = useRef("/");

  const generation = previewGeneration(data);
  const selectedTarget = computeSelectedTarget(selectedVersionSeq, currentRouteRef.current, refreshNonce);
  const requestedLaunchKey = computeRequestedLaunchKey(
    cid,
    generation,
    selectedVersionSeq,
    refreshNonce,
    committedWorkspaceKey(events),
  );

  const {
    launch,
    launchKey,
    launchVersionSeq,
    minting,
    launchFailure,
    bootstrapFailure,
    onBootstrapReady,
    frameReady,
    setFrameReady,
  } = usePreviewLaunch(
    cid,
    ownsCanonicalPreview,
    requestedLaunchKey,
    selectedTarget,
    selectedVersionSeq,
  );

  usePreviewRouteBridge(launch, frameRef, currentRouteRef);
  usePreviewAutoRefresh(files, status, data?.reload_strategy, setRefreshNonce);

  const { restoreNotice, restoringSeq, selectVersion, rollBackToVersion } = useWorkspaceRollback(
    cid,
    refetchVersions,
    setSelectedVersionSeq,
    currentRouteRef,
    setRefreshNonce,
  );

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
      if (
        !hasFinishedCommittedApp &&
        (!data?.available || data.status === "crashed" || data.status === "stopped")
      ) {
        await restartPreview(cid);
      }
      await queryClient.invalidateQueries({ queryKey: ["build-preview", cid] });
      setRefreshNonce((value) => value + 1);
    } finally {
      setRestarting(false);
    }
  }, [cid, data?.available, data?.status, hasFinishedCommittedApp, queryClient]);

  const mentionOrigin = previewOrigin(launch);
  const allowedOrigin = mentionOrigin ?? "null";
  const selection = useElementSelect(frameRef, allowedOrigin);
  const handleElementMention = useCallback(
    (payload: ElementMentionPayload) => onElementMention?.(payload),
    [onElementMention],
  );
  const mention = useElementMention(
    frameRef,
    mentionOrigin,
    handleElementMention,
    computeMentionEnabled(Boolean(onElementMention), launch, untrusted, displayedVersionSeq),
  );
  const [editMode, setEditMode] = useState(false);
  const canEdit = computeCanEdit(Boolean(onSelectionEdit), launch, untrusted, displayedVersionSeq);

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

  function handleArmMention() {
    if (editMode) toggleEdit();
    mention.arm();
  }

  function handleDisarmEdit() {
    selection.disarm();
    setEditMode(false);
  }

  function handleOpenPreview() {
    if (!cid) return;
    openFreshPreview(
      () =>
        displayedVersionSeq === null
          ? previewBootstrapUrl(cid, currentRouteRef.current)
          : previewBootstrapUrl(cid, "/", displayedVersionSeq),
      previewOpenError,
    );
  }

  // Imported/shared HTML and non-runtime HTML artifacts keep a deliberately
  // hardened viewer. It is visibly not the Build Preview and never mints a
  // runtime capability or supplies verification evidence.
  if (shouldShowStaticArtifactViewer(ownsCanonicalPreview, artifactSrcDoc, inlineHtmlArtifact, cid)) {
    return (
      <StaticArtifactViewer cid={cid} artifactSrcDoc={artifactSrcDoc} inlineHtmlArtifact={inlineHtmlArtifact} />
    );
  }

  if (!ownsCanonicalPreview) {
    return <PreviewUnavailable reason={data?.reason} />;
  }

  const runtimeUnavailable = computeRuntimeUnavailable(selectedVersionSeq, hasFinishedCommittedApp, data);
  const visibleFailure = computeVisibleFailure(
    launchFailure,
    launchKey,
    requestedLaunchKey,
    bootstrapFailure,
  );

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PreviewToolbar
        versions={versions}
        selectedVersionSeq={selectedVersionSeq}
        onSelectVersion={selectVersion}
        onRefresh={() => void refresh()}
        restarting={restarting}
        cid={cid}
        showPointButton={computeShowPointButton(Boolean(onElementMention), displayedVersionSeq)}
        mentionArmed={mention.armed}
        onArmMention={handleArmMention}
        onDisarmMention={mention.disarm}
        canEdit={canEdit}
        editMode={editMode}
        onToggleEdit={toggleEdit}
        onOpenPreview={handleOpenPreview}
      />
      {restoreNotice && (
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
      )}
      {shouldShowUpdateErrorBanner(selectedVersionSeq, data?.update_error) && (
        <div
          role="alert"
          className="shrink-0 border-b border-warn/30 bg-warn/10 px-body py-hair font-ui text-[0.72rem] text-warn"
        >
          Preview is showing the last healthy frame while the platform retries: {data?.update_error}
        </div>
      )}
      <VersionBanner
        displayedVersionSeq={displayedVersionSeq}
        restoringSeq={restoringSeq}
        onBackToCurrent={() => selectVersion(null)}
        onRollback={rollBackToVersion}
      />
      {editMode && (
        <div className="shrink-0 border-b border-hairline bg-surface-1 px-body py-hair font-ui text-[0.72rem] text-text-faint">
          Edit mode: click the real running page, then describe the targeted change.
        </div>
      )}
      <PreviewStage
        frameRef={frameRef}
        launch={launch}
        onFrameLoad={() => {
          setDisplayedVersionSeq(launchVersionSeq);
          setFrameReady(true);
        }}
        onBootstrapReady={onBootstrapReady}
        visibleFailure={visibleFailure}
        dataReason={data?.reason}
        minting={minting}
        runtimeUnavailable={runtimeUnavailable}
        frameReady={frameReady}
        editMode={editMode}
        untrusted={untrusted}
        selection={selection}
        onSteer={onSteer}
        onDisarmEdit={handleDisarmEdit}
        onApplyEdit={applyEdit}
        onRefresh={() => void refresh()}
        onDownloadSource={
          cid ? () => download.mutate({ id: cid, binding: null, title: downloadTitle }) : null
        }
        downloadPending={download.isPending}
      />
    </div>
  );
}
