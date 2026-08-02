import { Download, FileCode2, FileSpreadsheet, FileText } from "lucide-react";
import { cn } from "@/lib/cn";
import type { WorkspaceFile } from "@/lib/buildTrace";
import { artifactDownloadUrl } from "@/api/canvas";
import { fmtBytes } from "./format";

export function FileRow({ file, cid }: { file: WorkspaceFile; cid: string | null }) {
  const isSheet = /\.xlsx$/i.test(file.path);
  const Icon = isSheet ? FileSpreadsheet : /\.html?$/i.test(file.path) ? FileCode2 : FileText;
  // The declared-artifact route (encodeURI preserves any subdir slashes); honest —
  // only a real link when there's a conversation to fetch against.
  const href = cid ? artifactDownloadUrl(cid, file.path) : null;
  const inner = (
    <>
      <Icon className={cn("size-4 shrink-0", isSheet ? "text-accent" : "text-text-faint")} aria-hidden />
      <span className="min-w-0 flex-1">
        <span className="block truncate font-ui text-[0.82rem] text-text">
          {file.path.split("/").pop()}
        </span>
        <span className="block truncate font-mono text-[0.7rem] text-text-faint">
          {file.path} · {fmtBytes(file.bytes)}
        </span>
      </span>
      {href && <Download className="size-3.5 shrink-0 text-text-faint" aria-hidden />}
    </>
  );
  const base = "flex items-center gap-inline rounded-card border border-hairline bg-surface-0 px-inline py-hair";
  return href ? (
    <a href={href} download className={cn(base, "transition-colors hover:border-hairline-strong")}>
      {inner}
    </a>
  ) : (
    <div className={base}>{inner}</div>
  );
}
