import { artifactInlineUrl } from "@/api/preview";
import type { WorkspaceFile } from "@/lib/buildTrace";

/**
 * Imported/shared HTML and non-runtime HTML artifacts keep a deliberately
 * hardened viewer. It is visibly not the Build Preview and never mints a
 * runtime capability or supplies verification evidence.
 */
export function StaticArtifactViewer({
  cid,
  artifactSrcDoc,
  inlineHtmlArtifact,
}: {
  cid: string | null;
  artifactSrcDoc: string | null;
  inlineHtmlArtifact: WorkspaceFile | null;
}) {
  const inlineSrc =
    inlineHtmlArtifact && cid ? artifactInlineUrl(cid, inlineHtmlArtifact.path) : null;
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
