import { useRef } from "react";
import type { PreviewLaunch } from "@/api/canvas";
import { usePreviewSelectionAccess } from "@/hooks/usePreviewSelectionAccess";
import { useElementSelect } from "@/hooks/useElementSelect";
import { SelectionOverlay, SelectionToolbar } from "@/components/build/canvas/SelectionOverlay";
import { formatSelectionContext } from "@/lib/resolvers/appResolver";
import { PreviewLaunchFrame } from "@/components/PreviewLaunchFrame";

export function ArtifactPreview({ srcDoc, staticPreviewSrc, untrusted, onSteer, reloadPreview }: {
  srcDoc: string;
  staticPreviewSrc: PreviewLaunch | null;
  untrusted: boolean;
  onSteer?: (text: string) => void;
  reloadPreview: () => Promise<void>;
}) {
  // §4.1 C-EDIT-1: ref + selection state for the artifact preview iframe.
  const artifactIframeRef = useRef<HTMLIFrameElement | null>(null);
  const {
    armed: artifactArmed,
    arm: artifactArm,
    disarm: artifactDisarm,
    selection: artifactSelection,
    walkUp: artifactWalkUp,
    resetSelection: artifactResetSelection,
  } = useElementSelect(
    artifactIframeRef,
    staticPreviewSrc ? new URL(staticPreviewSrc.url).origin : "null",
  );

  const { expired: accessExpired, onExpiredChange } = usePreviewSelectionAccess(() => { artifactDisarm(); artifactResetSelection(); });

  return (
    <div className="flex min-h-0 flex-[2] flex-col border-b border-hairline">
      <div className="flex shrink-0 items-center justify-between border-b border-hairline px-body py-hair font-mono text-[0.74rem] text-text-faint">
        <span>preview</span>
        {untrusted && (
          <span className="font-ui text-text-faint">scripts disabled (untrusted)</span>
        )}
      </div>
      {!accessExpired && <SelectionToolbar
        armed={artifactArmed}
        untrusted={untrusted}
        onArm={artifactArm}
        onDisarm={artifactDisarm}
      />}
      {/* §4.1 C-EDIT-1: relative wrapper for SelectionOverlay positioning. */}
      <div className="relative min-h-0 flex-1">
        {staticPreviewSrc ? (
          <PreviewLaunchFrame
            ref={artifactIframeRef}
            title="Artifact preview"
            launch={staticPreviewSrc}
            onReload={reloadPreview}
            onExpiredChange={onExpiredChange}
            sandbox="allow-scripts allow-same-origin"
            className="h-full w-full border-0 bg-white"
          />
        ) : (
          <iframe
            ref={artifactIframeRef}
            title="Artifact preview"
            srcDoc={srcDoc}
            // untrusted (shared/imported run) → empty sandbox, no scripts: a script
            // here could reach this instance's open-CORS APIs.
            sandbox={untrusted ? "" : "allow-scripts"}
            className="h-full w-full border-0 bg-white"
          />
        )}
        <SelectionOverlay
          showToolbar={false}
          armed={!accessExpired && artifactArmed}
          untrusted={untrusted}
          selection={accessExpired ? null : artifactSelection}
          onArm={artifactArm}
          onDisarm={artifactDisarm}
          onWalkUp={artifactWalkUp}
          // W-26 — Discuss is offered whenever steering is available (onSteer
          // defined). Delivers the named-element context to the agent, then clears.
          onDiscuss={
            onSteer
              ? (sel) => {
                  onSteer(formatSelectionContext(sel));
                  artifactResetSelection();
                  artifactDisarm();
                }
              : undefined
          }
        />
      </div>
    </div>
  );
}
