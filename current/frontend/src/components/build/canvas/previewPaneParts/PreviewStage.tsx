import type { MutableRefObject } from "react";
import type { PreviewLaunch } from "@/api/preview";
import { PreviewLaunchFrame } from "@/components/PreviewLaunchFrame";
import { EditAffordance } from "@/components/build/canvas/EditAffordance";
import { SelectionOverlay } from "@/components/build/canvas/SelectionOverlay";
import type { ElementSelectState } from "@/hooks/useElementSelect";
import { formatSelectionContext } from "@/lib/resolvers/appResolver";
import {
  isPreviewReasonCode,
  previewStagePlaceholderMessage,
  shouldShowStatusOverlay,
  statusOverlayMessage,
  statusOverlayRole,
} from "./helpers";

export function PreviewStage({
  frameRef,
  launch,
  onFrameLoad,
  visibleFailure,
  dataReason,
  minting,
  runtimeUnavailable,
  frameReady,
  editMode,
  untrusted,
  selection,
  onSteer,
  onDisarmEdit,
  onApplyEdit,
}: {
  frameRef: MutableRefObject<HTMLIFrameElement | null>;
  launch: PreviewLaunch | null;
  onFrameLoad: () => void;
  visibleFailure: string | null;
  dataReason: string | undefined;
  minting: boolean;
  runtimeUnavailable: boolean;
  frameReady: boolean;
  editMode: boolean;
  untrusted: boolean;
  selection: ElementSelectState;
  onSteer?: (text: string) => void;
  onDisarmEdit: () => void;
  onApplyEdit: (instruction: string) => void;
}) {
  return (
    <div className="relative min-h-0 flex-1 bg-white">
      {launch ? (
        <PreviewLaunchFrame
          ref={frameRef}
          title="Preview"
          launch={launch}
          onLoad={onFrameLoad}
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
            {previewStagePlaceholderMessage(visibleFailure, dataReason)}
          </p>
          {visibleFailure && isPreviewReasonCode(visibleFailure) && (
            <p className="font-mono text-[0.7rem] text-text-faint">Reason code: {visibleFailure}</p>
          )}
        </div>
      )}
      {shouldShowStatusOverlay(launch, minting, runtimeUnavailable, visibleFailure, frameReady) && (
        <div
          role={statusOverlayRole(visibleFailure, runtimeUnavailable)}
          className="pointer-events-none absolute inset-x-body top-body z-20 rounded-control border border-hairline bg-bg/95 px-body py-hair font-ui text-[0.76rem] text-text-muted shadow-sm backdrop-blur"
        >
          {statusOverlayMessage(visibleFailure, runtimeUnavailable, dataReason)}
        </div>
      )}
      <SelectionOverlay
        armed={editMode && selection.armed}
        untrusted={untrusted}
        selection={selection.selection}
        onArm={selection.arm}
        onDisarm={onDisarmEdit}
        onWalkUp={selection.walkUp}
        onDiscuss={
          onSteer
            ? (selected) => {
                onSteer(formatSelectionContext(selected));
                selection.resetSelection();
                onDisarmEdit();
              }
            : undefined
        }
      />
      {editMode && selection.selection !== null && (
        <EditAffordance
          targetLabel={selection.selection.human_label}
          rect={selection.selection.rect}
          onApply={onApplyEdit}
          onCancel={selection.resetSelection}
        />
      )}
    </div>
  );
}
