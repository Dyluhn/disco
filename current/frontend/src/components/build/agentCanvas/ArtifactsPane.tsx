import { useMemo, useRef, useState, useEffect } from "react";
import { deriveDeliverable, deriveFiles, deriveSrcDoc } from "@/lib/buildTrace";
import { mintPathPreviewLaunch, type PreviewLaunch } from "@/api/canvas";
import { useElementSelect } from "@/hooks/useElementSelect";
import { SelectionOverlay } from "@/components/build/canvas/SelectionOverlay";
import { formatSelectionContext } from "@/lib/resolvers/appResolver";
import { SELECTION_AGENT_SCRIPT } from "@/lib/selectionAgent";
import { PreviewLaunchFrame } from "@/components/PreviewLaunchFrame";
import type { AgentEvent, ConversationStatus } from "@/types/agent";
import { Empty } from "./Empty";
import { FileRow } from "./FileRow";

export function ArtifactsPane({
  events,
  status,
  cid,
  untrusted,
  onSteer,
}: {
  events: AgentEvent[];
  status: ConversationStatus;
  cid: string | null;
  untrusted: boolean;
  /** W-26 — steer the agent from the artifact-preview selection ("Discuss with
   * agent"). Undefined when steering isn't available (no false affordance). */
  onSteer?: (text: string) => void;
}) {
  const files = useMemo(() => deriveFiles(events), [events]);
  const selectedDeliverable = useMemo(() => deriveDeliverable(events), [events]);
  const selectedHtmlPath =
    selectedDeliverable && /\.html?$/i.test(selectedDeliverable.path)
      ? selectedDeliverable.path
      : undefined;
  // §4.1 C-EDIT-1: inject selection agent for trusted (non-untrusted) iframes.
  const srcDoc = useMemo(
    () =>
      deriveSrcDoc(
        files,
        untrusted ? undefined : SELECTION_AGENT_SCRIPT,
        undefined,
        selectedHtmlPath,
      ),
    [files, selectedHtmlPath, untrusted],
  );
  const committedStaticPreview =
    status === "FINISHED" &&
    !untrusted &&
    cid !== null &&
    selectedDeliverable?.kind === "app" &&
    events.some((event) => event.kind === "workspace_version");
  const committedGeneration = useMemo(() => {
    let treeDigest = "unversioned";
    for (let i = events.length - 1; i >= 0; i -= 1) {
      const event = events[i];
      if (event?.kind === "workspace_version") {
        treeDigest = event.tree_digest;
        break;
      }
    }
    return `${selectedDeliverable?.id ?? "none"}:${treeDigest}`;
  }, [events, selectedDeliverable?.id]);
  const [staticPreviewSrc, setStaticPreviewSrc] = useState<PreviewLaunch | null>(null);
  const staticMintRef = useRef<{
    key: string;
    promise: Promise<PreviewLaunch | null>;
  } | null>(null);
  useEffect(() => {
    let cancelled = false;
    if (!committedStaticPreview || !cid || srcDoc === null) {
      setStaticPreviewSrc(null);
      staticMintRef.current = null;
      return;
    }
    const key = `${cid}:committed:${committedGeneration}`;
    const promise =
      staticMintRef.current?.key === key
        ? staticMintRef.current.promise
        : mintPathPreviewLaunch(cid, "/");
    staticMintRef.current = { key, promise };
    void promise
      .then((url) => {
        if (!cancelled) setStaticPreviewSrc(url);
      })
      .catch(() => {
        if (!cancelled) setStaticPreviewSrc(null);
      });
    return () => {
      cancelled = true;
    };
  }, [cid, committedGeneration, committedStaticPreview, srcDoc]);

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

  if (files.length === 0)
    return <Empty>Outputs the agent produces — files, spreadsheets, pages — show up here.</Empty>;
  return (
    <div className="flex h-full min-h-0 flex-col">
      {srcDoc != null && (
        <div className="flex min-h-0 flex-[2] flex-col border-b border-hairline">
          <div className="flex shrink-0 items-center justify-between border-b border-hairline px-body py-hair font-mono text-[0.74rem] text-text-faint">
            <span>preview</span>
            {untrusted && (
              <span className="font-ui text-text-faint">scripts disabled (untrusted)</span>
            )}
          </div>
          {/* §4.1 C-EDIT-1: relative wrapper for SelectionOverlay positioning. */}
          <div className="relative min-h-0 flex-1">
            {staticPreviewSrc ? (
              <PreviewLaunchFrame
                ref={artifactIframeRef}
                title="Artifact preview"
                launch={staticPreviewSrc}
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
              armed={artifactArmed}
              untrusted={untrusted}
              selection={artifactSelection}
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
      )}
      <ul className="flex min-h-0 flex-1 flex-col gap-hair overflow-auto p-body">
        {files.map((f) => (
          <li key={f.path}>
            <FileRow file={f} cid={cid} />
          </li>
        ))}
      </ul>
    </div>
  );
}
