import { ArtifactPreview } from "./ArtifactPreview";
import { useMemo, useRef, useState, useEffect } from "react";
import { deriveDeliverable, deriveFiles, deriveSrcDoc } from "@/lib/buildTrace";
import { mintPathPreviewLaunch, type PreviewLaunch } from "@/api/canvas";
import { SELECTION_AGENT_SCRIPT } from "@/lib/selectionAgent";
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
  const reloadOwner = useRef(0);
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
      reloadOwner.current += 1;
    };
  }, [cid, committedGeneration, committedStaticPreview, srcDoc]);

  async function reloadPreview() {
    const owned = staticMintRef.current;
    if (!cid || !owned) return;
    const owner = ++reloadOwner.current;
    const launch = await mintPathPreviewLaunch(cid, "/");
    if (reloadOwner.current !== owner || staticMintRef.current !== owned) return;
    if (!launch) throw new Error("Preview access unavailable");
    setStaticPreviewSrc(launch);
  }

  if (files.length === 0)
    return <Empty>Outputs the agent produces — files, spreadsheets, pages — show up here.</Empty>;
  return (
    <div className="flex h-full min-h-0 flex-col">
      {srcDoc != null && <ArtifactPreview srcDoc={srcDoc} staticPreviewSrc={staticPreviewSrc}
        untrusted={untrusted} onSteer={onSteer} reloadPreview={reloadPreview} />}
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
